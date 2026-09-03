"""MCP server exposing the SoD Rules API as tools.

This is a client of the FastAPI service, not a second reader/writer of
sod_rules.json. Going through HTTP keeps a single process owning the data
file, so the API's write lock still means something -- two processes with
their own locks would happily interleave a read-modify-write cycle and lose
records. It also keeps validation and the 404/409 rules in one place.

Start the API first, then this server:

    cd ../api && python main.py
    python main.py

MCP_TRANSPORT decides how this server listens (default streamable-http on
MCP_PORT 8000 -- the same as the API's 8000, since the two are expected to
run on separate hosts):

    claude mcp add --transport http sod-rules http://127.0.0.1:8000/mcp

    # or, for a client that spawns the process itself:
    MCP_TRANSPORT=stdio
    claude mcp add sod-rules -- python /path/to/sod-test/mcp/main.py

Built on FastMCP 3.x (the standalone `fastmcp` package), not the FastMCP 1.0
bundled inside the `mcp` SDK -- hence `from fastmcp import ...`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from config import settings

API_URL = settings.sod_api_url
TIMEOUT = settings.sod_api_timeout

_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    global _client
    async with httpx.AsyncClient(base_url=API_URL, timeout=TIMEOUT) as client:
        _client = client
        try:
            yield
        finally:
            _client = None


mcp = FastMCP(
    "sod-rules",
    version="1.0.0",
    instructions=(
        "Separation-of-duties rules: pairs of entitlements that one person must "
        "not hold at the same time, because together they would let someone both "
        "initiate and approve the same action (for example creating a vendor and "
        "approving its payments). Each rule names two conflicting entitlements "
        "and a severity, and is addressed by sod_id (e.g. 'SOD001'). Conflicts "
        "are symmetric: a rule for A/B is the same rule as B/A."
    ),
    lifespan=lifespan,
)

Severity = Literal["Low", "Medium", "High", "Critical"]


def _format_detail(payload: Any) -> str:
    """Turn a FastAPI error body into one readable line."""
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, list):
        # 422 validation errors: [{"loc": [...], "msg": ...}, ...]
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                parts.append(f"{loc}: {item.get('msg', item)}" if loc else str(item.get("msg", item)))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(detail)


async def _send(method: str, path: str, **kwargs: Any) -> httpx.Response:
    if _client is None:
        raise ToolError("MCP server is not running; no HTTP client available")
    try:
        response = await _client.request(method, path, **kwargs)
    except httpx.RequestError as exc:
        raise ToolError(
            f"Cannot reach the SoD Rules API at {API_URL} ({exc.__class__.__name__}). "
            "Is it running? Start it with: cd ../api && python main.py"
        ) from exc

    if response.is_success:
        return response

    try:
        detail = _format_detail(response.json())
    except ValueError:
        detail = response.text.strip() or "no response body"
    raise ToolError(f"API returned {response.status_code}: {detail}")


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    response = await _send(method, path, **kwargs)
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


async def _list_request(
    path: str,
    params: dict[str, Any],
    *,
    requested: list[str] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Run a list call and wrap the rows in an envelope.

    One batched call has to carry the two signals a per-item fan-out gave for
    free. `missing` names the requested values that matched no record, the
    batch replacement for a 404 -- without it, values nobody found just drop
    out of a short result unnoticed. `truncated` says `limit` cut the result
    short, which a bare array cannot express and which otherwise leaves the
    caller walking offsets blind.
    """
    response = await _send("GET", path, params=params)
    records = response.json()
    total = int(response.headers.get("X-Total-Count", len(records)))

    envelope: dict[str, Any] = {
        "records": records,
        "returned": len(records),
        "total_matching": total,
        "truncated": total > len(records),
    }
    if requested is not None and key is not None:
        found = {str(r.get(key, "")).lower() for r in records}
        envelope["requested"] = requested
        envelope["missing"] = [v for v in requested if v.lower() not in found]
    return envelope


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    """Drop unset filters. An empty list is unset too, not "match nothing"."""
    return {k: v for k, v in values.items() if v is not None and v != []}


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

SodId = Annotated[str, Field(description="SoD rule identifier, e.g. 'SOD001'")]
EntitlementName = Annotated[str, Field(description="Entitlement name, e.g. 'SAP_VENDOR_CREATE'")]


@mcp.tool(
    annotations=READ_ONLY,
    title="List SoD rules",
    description=(
        "Browse the separation-of-duties rulebook. `entitlements` returns rules "
        "where ANY of the given names appears on either side, in one call -- "
        "never call this once per entitlement. "
        "To decide whether one person may hold a SET of entitlements, use "
        "check_sod_conflicts instead: this tool also returns rules whose other "
        "side is outside your set, which are not conflicts for that person. "
        "The result is an envelope: `records` holds the rows, `truncated` is "
        "true when `limit` cut the result short."
    ),
)
async def list_sod_rules(
    entitlements: Annotated[
        list[str] | None,
        Field(description="Rules touching any of these, e.g. ['SAP_VENDOR_CREATE', 'JIRA_USER']"),
    ] = None,
    severities: Annotated[
        list[Severity] | None, Field(description="e.g. ['High', 'Critical']")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 1000,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    params = _drop_none(
        {"severity": severities, "entitlement": entitlements, "limit": limit, "offset": offset}
    )
    return await _list_request("/sod-rules", params)


@mcp.tool(
    annotations=READ_ONLY,
    title="Check a set of entitlements for SoD conflicts",
    description=(
        "THE way to run a separation-of-duties check. Pass every entitlement "
        "the person would hold and get one verdict back. "
        "`conflicts` lists rules with BOTH sides inside your set -- these are "
        "real conflicts and block the request. `adjacent` lists rules with one "
        "side in the set and its counterpart outside, which are NOT conflicts "
        "for this person but show what they must not be granted next. "
        "`clear` is true when there are no conflicts, and `highest_severity` "
        "is the worst conflict found. "
        "Do not rebuild this from list_sod_rules: unioning per-entitlement "
        "lookups mixes adjacent rules in with real conflicts."
    ),
)
async def check_sod_conflicts(
    entitlements: Annotated[
        list[str],
        Field(
            min_length=1,
            description="The full set to test together, e.g. ['SAP_VENDOR_CREATE', 'JIRA_USER']",
        ),
    ],
) -> dict[str, Any]:
    return await _request("POST", "/sod-rules/check", json={"entitlements": entitlements})


@mcp.tool(
    annotations=READ_ONLY,
    title="Get a SoD rule",
    description=(
        "Fetch one SoD rule by sod_id. Errors if no such rule exists. To test "
        "whether a set of entitlements conflicts, use check_sod_conflicts."
    ),
)
async def get_sod_rule(sod_id: SodId) -> dict[str, Any]:
    return await _request("GET", f"/sod-rules/{quote(sod_id, safe='')}")


@mcp.tool(
    annotations=WRITE,
    title="Create a SoD rule",
    description=(
        "Add a separation-of-duties rule. The two entitlements must differ, and "
        "the pair must not already be covered -- conflicts are symmetric, so "
        "A/B is rejected if a rule for B/A exists."
    ),
)
async def create_sod_rule(
    sod_id: SodId,
    entitlement_1: EntitlementName,
    entitlement_2: EntitlementName,
    severity: Severity,
) -> dict[str, Any]:
    return await _request(
        "POST",
        "/sod-rules",
        json={
            "sod_id": sod_id,
            "entitlement_1": entitlement_1,
            "entitlement_2": entitlement_2,
            "severity": severity,
        },
    )


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Update a SoD rule",
    description=(
        "Partially update a SoD rule: only the fields you pass are changed. "
        "sod_id cannot be changed. Changing an entitlement fails if the "
        "resulting pair duplicates another rule. Pass at least one field."
    ),
)
async def update_sod_rule(
    sod_id: SodId,
    entitlement_1: str | None = None,
    entitlement_2: str | None = None,
    severity: Severity | None = None,
) -> dict[str, Any]:
    changes = _drop_none(
        {"entitlement_1": entitlement_1, "entitlement_2": entitlement_2, "severity": severity}
    )
    if not changes:
        raise ToolError("Pass at least one field to change")
    return await _request("PATCH", f"/sod-rules/{quote(sod_id, safe='')}", json=changes)


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Replace a SoD rule",
    description=(
        "Overwrite every field of a SoD rule. Fields you omit are NOT preserved "
        "-- prefer update_sod_rule unless you intend a full replace."
    ),
)
async def replace_sod_rule(
    sod_id: SodId,
    entitlement_1: EntitlementName,
    entitlement_2: EntitlementName,
    severity: Severity,
) -> dict[str, Any]:
    return await _request(
        "PUT",
        f"/sod-rules/{quote(sod_id, safe='')}",
        json={
            "entitlement_1": entitlement_1,
            "entitlement_2": entitlement_2,
            "severity": severity,
        },
    )


@mcp.tool(
    annotations=DESTRUCTIVE,
    title="Delete a SoD rule",
    description="Permanently remove a SoD rule. This cannot be undone.",
)
async def delete_sod_rule(sod_id: SodId) -> dict[str, Any]:
    await _request("DELETE", f"/sod-rules/{quote(sod_id, safe='')}")
    return {"deleted": sod_id}


@mcp.tool(
    annotations=READ_ONLY,
    title="API health",
    description="Check that the SoD Rules API is reachable and which data file it serves.",
)
async def api_health() -> dict[str, Any]:
    return await _request("GET", "/health")


@mcp.resource(
    "sod://all",
    name="All SoD rules",
    description="The full separation-of-duties rule set as JSON.",
    mime_type="application/json",
)
async def all_sod_rules() -> list[dict[str, Any]]:
    return await _request("GET", "/sod-rules", params={"limit": 1000})


if __name__ == "__main__":
    # FastMCP 3.x defaults run() to streamable-http on port 8000, so the
    # transport and address are always explicit here.
    if settings.mcp_transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport=settings.mcp_transport,
            host=settings.mcp_host,
            port=settings.mcp_port,
        )
