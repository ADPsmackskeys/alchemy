"""MCP server exposing the New Joiners API as tools.

This is a client of the FastAPI service, not a second reader/writer of
new_joiners.json. Going through HTTP keeps a single process owning the
data file, so the API's write lock still means something -- two processes
with their own locks would happily interleave a read-modify-write cycle
and lose records. It also keeps validation and the 404/409 rules in one
place instead of reimplementing them here.

Start the API first, then point the server at it. Settings come from
config.py, so NEW_JOINERS_API_URL can be set in the environment or in a
.env file next to this module:

    python main.py
    python mcp_server.py

MCP_TRANSPORT decides how this server listens (default streamable-http on
MCP_PORT 8100 -- deliberately not the API's 8000). Register it accordingly:

    # streamable-http (default): server runs on its own, client connects
    claude mcp add --transport http new-joiners http://127.0.0.1:8100/mcp

    # stdio: the client spawns this process itself
    MCP_TRANSPORT=stdio
    claude mcp add new-joiners -- python /path/to/new-joiners-api/mcp_server.py

Built on FastMCP 3.x (the standalone `fastmcp` package), not the FastMCP 1.0
bundled inside the `mcp` SDK -- hence `from fastmcp import ...` rather than
`from mcp.server.fastmcp import ...`. Install it with:

    pip install -r requirements-mcp.txt
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from config import settings

API_URL = settings.new_joiners_api_url
TIMEOUT = settings.new_joiners_api_timeout

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
    "new-joiners",
    version="1.0.0",
    instructions=(
        "CRUD access to the New Joiners HR dataset: employees who have been "
        "hired and have a start date, with their department, job role, level, "
        "location, manager and cost center. Use list_new_joiners to search or "
        "browse, and employee_id (e.g. 'NJ1004') to address a single record."
    ),
    lifespan=lifespan,
)


def _format_detail(payload: Any) -> str:
    """Turn a FastAPI error body into one readable line."""
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
    else:
        detail = payload
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
            f"Cannot reach the New Joiners API at {API_URL} ({exc.__class__.__name__}). "
            "Is it running? Start it with: python main.py"
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


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

EmployeeId = Annotated[str, Field(description="Employee identifier, e.g. 'NJ1004'")]
StartDate = Annotated[str, Field(description="Start date as YYYY-MM-DD")]


@mcp.tool(
    annotations=READ_ONLY,
    title="List new joiners",
    description=(
        "List new joiners, newest additions last. All filters are optional and "
        "match case-insensitively on the whole value. Returns at most `limit` "
        "records; page through larger result sets with `offset`."
    ),
)
async def list_new_joiners(
    department: Annotated[str | None, Field(description="e.g. 'Finance', 'Technology'")] = None,
    location: Annotated[str | None, Field(description="e.g. 'Bangalore'")] = None,
    job_level: Annotated[str | None, Field(description="e.g. 'L2'")] = None,
    manager_id: Annotated[str | None, Field(description="e.g. 'MGR100'")] = None,
    limit: Annotated[int, Field(ge=1, le=1000, description="Max records to return")] = 100,
    offset: Annotated[int, Field(ge=0, description="Records to skip")] = 0,
) -> list[dict[str, Any]]:
    params = {
        k: v
        for k, v in {
            "department": department,
            "location": location,
            "job_level": job_level,
            "manager_id": manager_id,
            "limit": limit,
            "offset": offset,
        }.items()
        if v is not None
    }
    return await _request("GET", "/new-joiners", params=params)


@mcp.tool(
    annotations=READ_ONLY,
    title="Get a new joiner",
    description="Fetch one new joiner by employee_id. Errors if no such record exists.",
)
async def get_new_joiner(employee_id: EmployeeId) -> dict[str, Any]:
    return await _request("GET", f"/new-joiners/{employee_id}")


@mcp.tool(
    annotations=WRITE,
    title="Create a new joiner",
    description=(
        "Add a new joiner. Every field is required. Fails if employee_id is "
        "already taken -- use update_new_joiner to change an existing record."
    ),
)
async def create_new_joiner(
    employee_id: EmployeeId,
    name: str,
    department: str,
    job_role: str,
    job_level: str,
    location: str,
    manager_id: str,
    cost_center: str,
    start_date: StartDate,
) -> dict[str, Any]:
    return await _request(
        "POST",
        "/new-joiners",
        json={
            "employee_id": employee_id,
            "name": name,
            "department": department,
            "job_role": job_role,
            "job_level": job_level,
            "location": location,
            "manager_id": manager_id,
            "cost_center": cost_center,
            "start_date": start_date,
        },
    )


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Update a new joiner",
    description=(
        "Partially update a new joiner: only the fields you pass are changed, "
        "the rest keep their current values. employee_id cannot be changed. "
        "Pass at least one field."
    ),
)
async def update_new_joiner(
    employee_id: EmployeeId,
    name: str | None = None,
    department: str | None = None,
    job_role: str | None = None,
    job_level: str | None = None,
    location: str | None = None,
    manager_id: str | None = None,
    cost_center: str | None = None,
    start_date: Annotated[str | None, Field(description="Start date as YYYY-MM-DD")] = None,
) -> dict[str, Any]:
    changes = {
        k: v
        for k, v in {
            "name": name,
            "department": department,
            "job_role": job_role,
            "job_level": job_level,
            "location": location,
            "manager_id": manager_id,
            "cost_center": cost_center,
            "start_date": start_date,
        }.items()
        if v is not None
    }
    if not changes:
        raise ToolError("Pass at least one field to change")
    return await _request("PATCH", f"/new-joiners/{employee_id}", json=changes)


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Replace a new joiner",
    description=(
        "Overwrite every field of an existing new joiner. Fields you omit are "
        "NOT preserved -- prefer update_new_joiner unless you intend a full "
        "replace. employee_id cannot be changed."
    ),
)
async def replace_new_joiner(
    employee_id: EmployeeId,
    name: str,
    department: str,
    job_role: str,
    job_level: str,
    location: str,
    manager_id: str,
    cost_center: str,
    start_date: StartDate,
) -> dict[str, Any]:
    return await _request(
        "PUT",
        f"/new-joiners/{employee_id}",
        json={
            "name": name,
            "department": department,
            "job_role": job_role,
            "job_level": job_level,
            "location": location,
            "manager_id": manager_id,
            "cost_center": cost_center,
            "start_date": start_date,
        },
    )


@mcp.tool(
    annotations=DESTRUCTIVE,
    title="Delete a new joiner",
    description="Permanently remove a new joiner. This cannot be undone.",
)
async def delete_new_joiner(employee_id: EmployeeId) -> dict[str, Any]:
    await _request("DELETE", f"/new-joiners/{employee_id}")
    return {"deleted": employee_id}


@mcp.tool(
    annotations=READ_ONLY,
    title="API health",
    description="Check that the New Joiners API is reachable and which data file it is serving.",
)
async def api_health() -> dict[str, Any]:
    return await _request("GET", "/health")


@mcp.resource(
    "new-joiners://all",
    name="All new joiners",
    description="The full new joiners dataset as JSON.",
    mime_type="application/json",
)
async def all_new_joiners() -> list[dict[str, Any]]:
    return await _request("GET", "/new-joiners", params={"limit": 1000})


if __name__ == "__main__":
    # FastMCP 3.x defaults run() to streamable-http on port 8000 -- which is
    # the API's own port -- so the transport and address are always explicit.
    if settings.mcp_transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport=settings.mcp_transport,
            host=settings.mcp_host,
            port=settings.mcp_port,
        )

