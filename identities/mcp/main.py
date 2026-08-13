"""MCP server exposing the Identities API as tools.

This is a client of the FastAPI service, not a second reader/writer of
identities.json. Going through HTTP keeps a single process owning the data
file, so the API's write lock still means something -- two processes with
their own locks would happily interleave a read-modify-write cycle and lose
records. It also keeps validation and the 404/409 rules in one place.

Start the API first, then this server:

    cd ../api && python main.py
    python main.py

MCP_TRANSPORT decides how this server listens (default streamable-http on
MCP_PORT 8102 -- deliberately not the API's 8002):

    claude mcp add --transport http identities http://127.0.0.1:8102/mcp

    # or, for a client that spawns the process itself:
    MCP_TRANSPORT=stdio
    claude mcp add identities -- python /path/to/identities/mcp/main.py

Built on FastMCP 3.x (the standalone `fastmcp` package), not the FastMCP 1.0
bundled inside the `mcp` SDK -- hence `from fastmcp import ...`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from config import settings

API_URL = settings.identities_api_url
TIMEOUT = settings.identities_api_timeout

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
    "identities",
    version="1.0.0",
    instructions=(
        "Access to the workforce identity register: who each employee is, their "
        "department, job role, level and location, plus the entitlements they "
        "currently hold. Records are addressed by employee_id (e.g. 'EMP001'). "
        "Entitlements are stored as one ';'-separated string; use "
        "get_identity_entitlements for a parsed list, or the entitlement filter "
        "on list_identities to find everyone holding a given entitlement."
    ),
    lifespan=lifespan,
)


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
            f"Cannot reach the Identities API at {API_URL} ({exc.__class__.__name__}). "
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

EmployeeId = Annotated[str, Field(description="Employee identifier, e.g. 'EMP001'")]
Entitlements = Annotated[
    str,
    Field(description="Semicolon-separated entitlement names, e.g. 'JIRA_USER;GITHUB_DEV'"),
]


@mcp.tool(
    annotations=READ_ONLY,
    title="List identities",
    description=(
        "List identities. All filters are optional and match case-insensitively "
        "on the whole value; `entitlement` matches an exact entitlement name held "
        "by the identity, not a substring. Page with limit/offset."
    ),
)
async def list_identities(
    department: Annotated[str | None, Field(description="e.g. 'Finance', 'Technology'")] = None,
    location: Annotated[str | None, Field(description="e.g. 'Bangalore'")] = None,
    job_level: Annotated[str | None, Field(description="e.g. 'L2'")] = None,
    job_role: Annotated[str | None, Field(description="e.g. 'Financial Analyst'")] = None,
    entitlement: Annotated[
        str | None, Field(description="Only identities holding this entitlement")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> list[dict[str, Any]]:
    params = _drop_none(
        {
            "department": department,
            "location": location,
            "job_level": job_level,
            "job_role": job_role,
            "entitlement": entitlement,
            "limit": limit,
            "offset": offset,
        }
    )
    return await _request("GET", "/identities", params=params)


@mcp.tool(
    annotations=READ_ONLY,
    title="Get an identity",
    description="Fetch one identity by employee_id. Errors if no such record exists.",
)
async def get_identity(employee_id: EmployeeId) -> dict[str, Any]:
    return await _request("GET", f"/identities/{quote(employee_id, safe='')}")


@mcp.tool(
    annotations=READ_ONLY,
    title="Get an identity's entitlements",
    description=(
        "The entitlements held by one identity, parsed from the stored "
        "';'-separated string into a list."
    ),
)
async def get_identity_entitlements(employee_id: EmployeeId) -> list[str]:
    return await _request("GET", f"/identities/{quote(employee_id, safe='')}/entitlements")


@mcp.tool(
    annotations=WRITE,
    title="Create an identity",
    description=(
        "Add an identity. Every field is required. Fails if employee_id is "
        "already taken -- use update_identity to change an existing record."
    ),
)
async def create_identity(
    employee_id: EmployeeId,
    name: str,
    department: str,
    job_role: str,
    job_level: str,
    location: str,
    entitlements: Entitlements,
) -> dict[str, Any]:
    return await _request(
        "POST",
        "/identities",
        json={
            "employee_id": employee_id,
            "name": name,
            "department": department,
            "job_role": job_role,
            "job_level": job_level,
            "location": location,
            "entitlements": entitlements,
        },
    )


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Update an identity",
    description=(
        "Partially update an identity: only the fields you pass are changed. "
        "employee_id cannot be changed. Passing `entitlements` replaces the whole "
        "set, so send the complete list, not just the additions. "
        "Pass at least one field."
    ),
)
async def update_identity(
    employee_id: EmployeeId,
    name: str | None = None,
    department: str | None = None,
    job_role: str | None = None,
    job_level: str | None = None,
    location: str | None = None,
    entitlements: Annotated[
        str | None, Field(description="Semicolon-separated; replaces the entire set")
    ] = None,
) -> dict[str, Any]:
    changes = _drop_none(
        {
            "name": name,
            "department": department,
            "job_role": job_role,
            "job_level": job_level,
            "location": location,
            "entitlements": entitlements,
        }
    )
    if not changes:
        raise ToolError("Pass at least one field to change")
    return await _request("PATCH", f"/identities/{quote(employee_id, safe='')}", json=changes)


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Replace an identity",
    description=(
        "Overwrite every field of an identity. Fields you omit are NOT preserved "
        "-- prefer update_identity unless you intend a full replace."
    ),
)
async def replace_identity(
    employee_id: EmployeeId,
    name: str,
    department: str,
    job_role: str,
    job_level: str,
    location: str,
    entitlements: Entitlements,
) -> dict[str, Any]:
    return await _request(
        "PUT",
        f"/identities/{quote(employee_id, safe='')}",
        json={
            "name": name,
            "department": department,
            "job_role": job_role,
            "job_level": job_level,
            "location": location,
            "entitlements": entitlements,
        },
    )


@mcp.tool(
    annotations=DESTRUCTIVE,
    title="Delete an identity",
    description="Permanently remove an identity. This cannot be undone.",
)
async def delete_identity(employee_id: EmployeeId) -> dict[str, Any]:
    await _request("DELETE", f"/identities/{quote(employee_id, safe='')}")
    return {"deleted": employee_id}


@mcp.tool(
    annotations=READ_ONLY,
    title="API health",
    description="Check that the Identities API is reachable and which data file it serves.",
)
async def api_health() -> dict[str, Any]:
    return await _request("GET", "/health")


@mcp.resource(
    "identities://all",
    name="All identities",
    description="The full identity register as JSON.",
    mime_type="application/json",
)
async def all_identities() -> list[dict[str, Any]]:
    return await _request("GET", "/identities", params={"limit": 1000})


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
