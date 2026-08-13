"""MCP server exposing the Policy API as tools.

This is a client of the FastAPI service, not a second reader/writer of
policy_rules.json. Going through HTTP keeps a single process owning the data
file, so the API's write lock still means something -- two processes with
their own locks would happily interleave a read-modify-write cycle and lose
records. It also keeps validation and the 404/409 rules in one place.

Start the API first, then this server:

    cd ../api && python main.py
    python main.py

MCP_TRANSPORT decides how this server listens (default streamable-http on
MCP_PORT 8104 -- deliberately not the API's 8004):

    claude mcp add --transport http policy http://127.0.0.1:8104/mcp

    # or, for a client that spawns the process itself:
    MCP_TRANSPORT=stdio
    claude mcp add policy -- python /path/to/policy/mcp/main.py

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

API_URL = settings.policy_api_url
TIMEOUT = settings.policy_api_timeout

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
    "policy",
    version="1.0.0",
    instructions=(
        "The access policy rulebook. Each rule has a type -- ALLOW grants access "
        "outright (birthright entitlements for a role), DENY blocks it, and "
        "HUMAN_APPROVAL routes the request to a reviewer. The `rule` field holds "
        "the condition as free text, either a mapping like "
        "'Financial Analyst -> SAP_FIN_DISPLAY' or a threshold like "
        "'risk_score >= 70'. Rules are addressed by policy_id (e.g. 'POL001')."
    ),
    lifespan=lifespan,
)

PolicyType = Literal["ALLOW", "DENY", "HUMAN_APPROVAL"]


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


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    if _client is None:
        raise ToolError("MCP server is not running; no HTTP client available")
    try:
        response = await _client.request(method, path, **kwargs)
    except httpx.RequestError as exc:
        raise ToolError(
            f"Cannot reach the Policy API at {API_URL} ({exc.__class__.__name__}). "
            "Is it running? Start it with: cd ../api && python main.py"
        ) from exc

    if response.is_success:
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    try:
        detail = _format_detail(response.json())
    except ValueError:
        detail = response.text.strip() or "no response body"
    raise ToolError(f"API returned {response.status_code}: {detail}")


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

PolicyId = Annotated[str, Field(description="Policy identifier, e.g. 'POL001'")]
RuleText = Annotated[
    str,
    Field(description="Condition, e.g. 'Software Engineer -> JIRA_USER' or 'risk_score >= 70'"),
]


@mcp.tool(
    annotations=READ_ONLY,
    title="List policies",
    description=(
        "List policy rules. Filter by type or policy_name (case-insensitive, "
        "whole value), or search rule text with rule_contains (substring). "
        "Page with limit/offset."
    ),
)
async def list_policies(
    type: Annotated[PolicyType | None, Field(description="ALLOW, DENY or HUMAN_APPROVAL")] = None,
    policy_name: Annotated[str | None, Field(description="e.g. 'Finance Birthright'")] = None,
    rule_contains: Annotated[
        str | None, Field(description="Substring of the rule text, e.g. 'risk_score'")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> list[dict[str, Any]]:
    params = _drop_none(
        {
            "type": type,
            "policy_name": policy_name,
            "rule_contains": rule_contains,
            "limit": limit,
            "offset": offset,
        }
    )
    return await _request("GET", "/policies", params=params)


@mcp.tool(
    annotations=READ_ONLY,
    title="Get a policy",
    description="Fetch one policy rule by policy_id. Errors if no such rule exists.",
)
async def get_policy(policy_id: PolicyId) -> dict[str, Any]:
    return await _request("GET", f"/policies/{quote(policy_id, safe='')}")


@mcp.tool(
    annotations=WRITE,
    title="Create a policy",
    description=(
        "Add a policy rule. Every field is required. Fails if policy_id is "
        "already taken -- use update_policy to change an existing rule."
    ),
)
async def create_policy(
    policy_id: PolicyId,
    policy_name: str,
    type: PolicyType,
    rule: RuleText,
) -> dict[str, Any]:
    return await _request(
        "POST",
        "/policies",
        json={"policy_id": policy_id, "policy_name": policy_name, "type": type, "rule": rule},
    )


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Update a policy",
    description=(
        "Partially update a policy rule: only the fields you pass are changed. "
        "policy_id cannot be changed. Pass at least one field."
    ),
)
async def update_policy(
    policy_id: PolicyId,
    policy_name: str | None = None,
    type: PolicyType | None = None,
    rule: str | None = None,
) -> dict[str, Any]:
    changes = _drop_none({"policy_name": policy_name, "type": type, "rule": rule})
    if not changes:
        raise ToolError("Pass at least one field to change")
    return await _request("PATCH", f"/policies/{quote(policy_id, safe='')}", json=changes)


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Replace a policy",
    description=(
        "Overwrite every field of a policy rule. Fields you omit are NOT "
        "preserved -- prefer update_policy unless you intend a full replace."
    ),
)
async def replace_policy(
    policy_id: PolicyId,
    policy_name: str,
    type: PolicyType,
    rule: RuleText,
) -> dict[str, Any]:
    return await _request(
        "PUT",
        f"/policies/{quote(policy_id, safe='')}",
        json={"policy_name": policy_name, "type": type, "rule": rule},
    )


@mcp.tool(
    annotations=DESTRUCTIVE,
    title="Delete a policy",
    description="Permanently remove a policy rule. This cannot be undone.",
)
async def delete_policy(policy_id: PolicyId) -> dict[str, Any]:
    await _request("DELETE", f"/policies/{quote(policy_id, safe='')}")
    return {"deleted": policy_id}


@mcp.tool(
    annotations=READ_ONLY,
    title="API health",
    description="Check that the Policy API is reachable and which data file it serves.",
)
async def api_health() -> dict[str, Any]:
    return await _request("GET", "/health")


@mcp.resource(
    "policy://all",
    name="All policy rules",
    description="The full policy rulebook as JSON.",
    mime_type="application/json",
)
async def all_policies() -> list[dict[str, Any]]:
    return await _request("GET", "/policies", params={"limit": 1000})


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
