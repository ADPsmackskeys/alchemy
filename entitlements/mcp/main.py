"""MCP server exposing the Entitlements API as tools.

This is a client of the FastAPI service, not a second reader/writer of the
JSON files. Going through HTTP keeps a single process owning the data, so
the API's write lock still means something -- two processes with their own
locks would happily interleave a read-modify-write cycle and lose records.
It also keeps validation and the 404/409 rules in one place.

Start the API first, then this server:

    cd ../api && python main.py
    python main.py

MCP_TRANSPORT decides how this server listens (default streamable-http on
MCP_PORT 8000 -- the same as the API's 8000, since the two are expected to
run on separate hosts):

    claude mcp add --transport http entitlements http://127.0.0.1:8000/mcp

    # or, for a client that spawns the process itself:
    MCP_TRANSPORT=stdio
    claude mcp add entitlements -- python /path/to/entitlements/mcp/main.py

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

API_URL = settings.entitlements_api_url
TIMEOUT = settings.entitlements_api_timeout

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
    "entitlements",
    version="1.0.0",
    instructions=(
        "Access to the entitlement catalog and its risk scores. The catalog "
        "lists each entitlement with its application and owning team, addressed by "
        "entitlement_id (e.g. 'ENT001'). Risk scores rate each entitlement 0-100 "
        "with a category, addressed by entitlement_name (e.g. 'SAP_FIN_DISPLAY'). "
        "The two join on entitlement_name."
    ),
    lifespan=lifespan,
)

RiskCategory = Literal["Low", "Medium", "High", "Critical"]


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
            f"Cannot reach the Entitlements API at {API_URL} ({exc.__class__.__name__}). "
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

EntitlementId = Annotated[str, Field(description="Catalog identifier, e.g. 'ENT001'")]
EntitlementName = Annotated[str, Field(description="Entitlement name, e.g. 'SAP_FIN_DISPLAY'")]


# --------------------------------------------------------------------------
# Entitlement catalog
# --------------------------------------------------------------------------


@mcp.tool(
    annotations=READ_ONLY,
    title="List entitlements",
    description=(
        "Look up catalog entitlements -- their application and owning team. "
        "Pass every value you need in ONE call -- do not call this once per value. Filters are "
        "repeatable lists: values inside one filter are ORed, separate filters are ANDed. Omit "
        "them all to get the whole table, which is small. The result is an envelope: `records` "
        "holds the rows, `missing` names any requested value with no matching record (treat a non- "
        "empty `missing` as you would a 404), and `truncated` is true when `limit` cut the result "
        "short."
    ),
)
async def list_entitlements(
    entitlement_names: Annotated[
        list[str] | None,
        Field(description="Every name to look up at once, e.g. ['SAP_FIN_DISPLAY', 'JIRA_USER']"),
    ] = None,
    applications: Annotated[
        list[str] | None, Field(description="e.g. ['SAP ECC', 'PowerBI']")
    ] = None,
    owners: Annotated[
        list[str] | None, Field(description="Owning teams, e.g. ['Finance IT']")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 1000,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    params = _drop_none(
        {
            "application": applications,
            "owner": owners,
            "entitlement_name": entitlement_names,
            "limit": limit,
            "offset": offset,
        }
    )
    return await _list_request(
        "/entitlements", params, requested=entitlement_names, key="entitlement_name"
    )


@mcp.tool(
    annotations=READ_ONLY,
    title="Get an entitlement",
    description=(
        "Fetch one catalog entitlement by entitlement_id. Errors if it does not "
        "exist. For several entitlements use list_entitlements with a list of "
        "names -- one call, not one per entitlement."
    ),
)
async def get_entitlement(entitlement_id: EntitlementId) -> dict[str, Any]:
    return await _request("GET", f"/entitlements/{quote(entitlement_id, safe='')}")


@mcp.tool(
    annotations=WRITE,
    title="Create an entitlement",
    description=(
        "Add a catalog entitlement. Every field is required. Fails if "
        "entitlement_id is taken -- use update_entitlement to change one."
    ),
)
async def create_entitlement(
    entitlement_id: EntitlementId,
    entitlement_name: EntitlementName,
    application: str,
    owner: str,
) -> dict[str, Any]:
    return await _request(
        "POST",
        "/entitlements",
        json={
            "entitlement_id": entitlement_id,
            "entitlement_name": entitlement_name,
            "application": application,
            "owner": owner,
        },
    )


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Update an entitlement",
    description=(
        "Partially update a catalog entitlement: only the fields you pass are "
        "changed. entitlement_id cannot be changed. Pass at least one field."
    ),
)
async def update_entitlement(
    entitlement_id: EntitlementId,
    entitlement_name: str | None = None,
    application: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    changes = _drop_none(
        {"entitlement_name": entitlement_name, "application": application, "owner": owner}
    )
    if not changes:
        raise ToolError("Pass at least one field to change")
    return await _request("PATCH", f"/entitlements/{quote(entitlement_id, safe='')}", json=changes)


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Replace an entitlement",
    description=(
        "Overwrite every field of a catalog entitlement. Fields you omit are "
        "NOT preserved -- prefer update_entitlement unless you mean a full replace."
    ),
)
async def replace_entitlement(
    entitlement_id: EntitlementId,
    entitlement_name: EntitlementName,
    application: str,
    owner: str,
) -> dict[str, Any]:
    return await _request(
        "PUT",
        f"/entitlements/{quote(entitlement_id, safe='')}",
        json={
            "entitlement_name": entitlement_name,
            "application": application,
            "owner": owner,
        },
    )


@mcp.tool(
    annotations=DESTRUCTIVE,
    title="Delete an entitlement",
    description="Permanently remove a catalog entitlement. This cannot be undone.",
)
async def delete_entitlement(entitlement_id: EntitlementId) -> dict[str, Any]:
    await _request("DELETE", f"/entitlements/{quote(entitlement_id, safe='')}")
    return {"deleted": entitlement_id}


# --------------------------------------------------------------------------
# Risk scores
# --------------------------------------------------------------------------


@mcp.tool(
    annotations=READ_ONLY,
    title="List risk scores",
    description=(
        "Score entitlements 0-100 with a risk category. To rate a set of "
        "entitlements, pass them all as entitlement_names in one call rather "
        "than calling get_risk_score once per name. Pass every value you need in ONE call -- do "
        "not call this once per value. Filters are repeatable lists: values inside one filter are "
        "ORed, separate filters are ANDed. Omit them all to get the whole table, which is small. "
        "The result is an envelope: `records` holds the rows, `missing` names any requested value "
        "with no matching record (treat a non-empty `missing` as you would a 404), and `truncated` "
        "is true when `limit` cut the result short."
    ),
)
async def list_risk_scores(
    entitlement_names: Annotated[
        list[str] | None,
        Field(description="Every name to score at once, e.g. ['JIRA_USER', 'GITHUB_DEV']"),
    ] = None,
    applications: Annotated[list[str] | None, Field(description="e.g. ['SAP ECC']")] = None,
    risk_categories: Annotated[
        list[RiskCategory] | None, Field(description="e.g. ['High', 'Critical']")
    ] = None,
    min_score: Annotated[int | None, Field(ge=0, le=100)] = None,
    max_score: Annotated[int | None, Field(ge=0, le=100)] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 1000,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    params = _drop_none(
        {
            "entitlement_name": entitlement_names,
            "application": applications,
            "risk_category": risk_categories,
            "min_score": min_score,
            "max_score": max_score,
            "limit": limit,
            "offset": offset,
        }
    )
    return await _list_request(
        "/risk-scores", params, requested=entitlement_names, key="entitlement_name"
    )


@mcp.tool(
    annotations=READ_ONLY,
    title="Get a risk score",
    description=(
        "Fetch one risk score by entitlement_name. Errors if it does not exist. "
        "For several entitlements use list_risk_scores with entitlement_names -- "
        "one call, not one per entitlement."
    ),
)
async def get_risk_score(entitlement_name: EntitlementName) -> dict[str, Any]:
    return await _request("GET", f"/risk-scores/{quote(entitlement_name, safe='')}")


@mcp.tool(
    annotations=WRITE,
    title="Create a risk score",
    description=(
        "Score an entitlement 0-100 and categorise it. Fails if that "
        "entitlement_name already has a score."
    ),
)
async def create_risk_score(
    entitlement_name: EntitlementName,
    application: str,
    risk_score: Annotated[int, Field(ge=0, le=100)],
    risk_category: RiskCategory,
) -> dict[str, Any]:
    return await _request(
        "POST",
        "/risk-scores",
        json={
            "entitlement_name": entitlement_name,
            "application": application,
            "risk_score": risk_score,
            "risk_category": risk_category,
        },
    )


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Update a risk score",
    description=(
        "Partially update a risk score: only the fields you pass are changed. "
        "entitlement_name cannot be changed. Pass at least one field."
    ),
)
async def update_risk_score(
    entitlement_name: EntitlementName,
    application: str | None = None,
    risk_score: Annotated[int | None, Field(ge=0, le=100)] = None,
    risk_category: RiskCategory | None = None,
) -> dict[str, Any]:
    changes = _drop_none(
        {"application": application, "risk_score": risk_score, "risk_category": risk_category}
    )
    if not changes:
        raise ToolError("Pass at least one field to change")
    return await _request(
        "PATCH", f"/risk-scores/{quote(entitlement_name, safe='')}", json=changes
    )


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Replace a risk score",
    description=(
        "Overwrite every field of a risk score. Fields you omit are NOT "
        "preserved -- prefer update_risk_score unless you mean a full replace."
    ),
)
async def replace_risk_score(
    entitlement_name: EntitlementName,
    application: str,
    risk_score: Annotated[int, Field(ge=0, le=100)],
    risk_category: RiskCategory,
) -> dict[str, Any]:
    return await _request(
        "PUT",
        f"/risk-scores/{quote(entitlement_name, safe='')}",
        json={
            "application": application,
            "risk_score": risk_score,
            "risk_category": risk_category,
        },
    )


@mcp.tool(
    annotations=DESTRUCTIVE,
    title="Delete a risk score",
    description="Permanently remove an entitlement's risk score. This cannot be undone.",
)
async def delete_risk_score(entitlement_name: EntitlementName) -> dict[str, Any]:
    await _request("DELETE", f"/risk-scores/{quote(entitlement_name, safe='')}")
    return {"deleted": entitlement_name}


@mcp.tool(
    annotations=READ_ONLY,
    title="API health",
    description="Check that the Entitlements API is reachable and which data files it serves.",
)
async def api_health() -> dict[str, Any]:
    return await _request("GET", "/health")


@mcp.resource(
    "entitlements://catalog",
    name="Entitlement catalog",
    description="The full entitlement catalog as JSON.",
    mime_type="application/json",
)
async def catalog_resource() -> list[dict[str, Any]]:
    return await _request("GET", "/entitlements", params={"limit": 1000})


@mcp.resource(
    "entitlements://risk-scores",
    name="Entitlement risk scores",
    description="The full entitlement risk score table as JSON.",
    mime_type="application/json",
)
async def risk_scores_resource() -> list[dict[str, Any]]:
    return await _request("GET", "/risk-scores", params={"limit": 1000})


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
