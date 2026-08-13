"""MCP server exposing the Peer Affinity API as tools.

This is a client of the FastAPI service, not a second reader/writer of
peer_affinity_scores.json. Going through HTTP keeps a single process owning
the data file, so the API's write lock still means something -- two processes
with their own locks would happily interleave a read-modify-write cycle and
lose records. It also keeps validation and the 404/409 rules in one place.

Start the API first, then this server:

    cd ../api && python main.py
    python main.py

MCP_TRANSPORT decides how this server listens (default streamable-http on
MCP_PORT 8103 -- deliberately not the API's 8003):

    claude mcp add --transport http peer-affinity http://127.0.0.1:8103/mcp

    # or, for a client that spawns the process itself:
    MCP_TRANSPORT=stdio
    claude mcp add peer-affinity -- python /path/to/peer-affinity/mcp/main.py

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

API_URL = settings.peer_affinity_api_url
TIMEOUT = settings.peer_affinity_api_timeout

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
    "peer-affinity",
    version="1.0.0",
    instructions=(
        "How common an entitlement is among peers doing the same job. Each row "
        "says: of the `total_peers` people in this job_role, `peer_count` hold "
        "this entitlement, giving an `affinity_score` percentage. A high score "
        "means the entitlement is normal for the role; a low one means it is an "
        "outlier worth reviewing. Rows have no single id -- address one by its "
        "(job_role, entitlement) pair."
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
            f"Cannot reach the Peer Affinity API at {API_URL} ({exc.__class__.__name__}). "
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


def _row_path(job_role: str, entitlement: str) -> str:
    # job_role contains spaces ("Financial Analyst"), so both path segments
    # are percent-encoded rather than interpolated raw.
    return f"/peer-affinity/{quote(job_role, safe='')}/{quote(entitlement, safe='')}"


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

JobRole = Annotated[str, Field(description="Job role, e.g. 'Financial Analyst'")]
EntitlementName = Annotated[str, Field(description="Entitlement name, e.g. 'SAP_FIN_DISPLAY'")]


@mcp.tool(
    annotations=READ_ONLY,
    title="List peer affinity rows",
    description=(
        "List peer affinity rows. All filters are optional and match "
        "case-insensitively on the whole value. Use max_score to surface "
        "outlier access (entitlements few peers hold), min_score for the "
        "entitlements that are standard for a role."
    ),
)
async def list_peer_affinity(
    job_role: Annotated[str | None, Field(description="e.g. 'Software Engineer'")] = None,
    department: Annotated[str | None, Field(description="e.g. 'Technology'")] = None,
    entitlement: Annotated[str | None, Field(description="e.g. 'GITHUB_DEV'")] = None,
    min_score: Annotated[int | None, Field(ge=0, le=100, description="Minimum affinity %")] = None,
    max_score: Annotated[int | None, Field(ge=0, le=100, description="Maximum affinity %")] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> list[dict[str, Any]]:
    params = _drop_none(
        {
            "job_role": job_role,
            "department": department,
            "entitlement": entitlement,
            "min_score": min_score,
            "max_score": max_score,
            "limit": limit,
            "offset": offset,
        }
    )
    return await _request("GET", "/peer-affinity", params=params)


@mcp.tool(
    annotations=READ_ONLY,
    title="Get a peer affinity row",
    description=(
        "Fetch the affinity row for one (job_role, entitlement) pair. "
        "Errors if no such row exists."
    ),
)
async def get_peer_affinity(job_role: JobRole, entitlement: EntitlementName) -> dict[str, Any]:
    return await _request("GET", _row_path(job_role, entitlement))


@mcp.tool(
    annotations=WRITE,
    title="Create a peer affinity row",
    description=(
        "Add an affinity row. affinity_score is optional: omit it and it is "
        "computed as peer_count/total_peers. peer_count cannot exceed "
        "total_peers. Fails if the (job_role, entitlement) pair already exists."
    ),
)
async def create_peer_affinity(
    job_role: JobRole,
    department: str,
    entitlement: EntitlementName,
    peer_count: Annotated[int, Field(ge=0, description="Peers in the role holding it")],
    total_peers: Annotated[int, Field(ge=1, description="Peers in the role overall")],
    affinity_score: Annotated[
        int | None, Field(ge=0, le=100, description="Computed when omitted")
    ] = None,
) -> dict[str, Any]:
    body = _drop_none(
        {
            "job_role": job_role,
            "department": department,
            "entitlement": entitlement,
            "peer_count": peer_count,
            "total_peers": total_peers,
            "affinity_score": affinity_score,
        }
    )
    return await _request("POST", "/peer-affinity", json=body)


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Update a peer affinity row",
    description=(
        "Partially update an affinity row: only the fields you pass are changed. "
        "The (job_role, entitlement) pair identifies the row and cannot change. "
        "Changing peer_count or total_peers without also passing affinity_score "
        "recomputes the score. Pass at least one field."
    ),
)
async def update_peer_affinity(
    job_role: JobRole,
    entitlement: EntitlementName,
    department: str | None = None,
    peer_count: Annotated[int | None, Field(ge=0)] = None,
    total_peers: Annotated[int | None, Field(ge=1)] = None,
    affinity_score: Annotated[int | None, Field(ge=0, le=100)] = None,
) -> dict[str, Any]:
    changes = _drop_none(
        {
            "department": department,
            "peer_count": peer_count,
            "total_peers": total_peers,
            "affinity_score": affinity_score,
        }
    )
    if not changes:
        raise ToolError("Pass at least one field to change")
    return await _request("PATCH", _row_path(job_role, entitlement), json=changes)


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Replace a peer affinity row",
    description=(
        "Overwrite every field of an affinity row. Fields you omit are NOT "
        "preserved -- prefer update_peer_affinity unless you mean a full replace."
    ),
)
async def replace_peer_affinity(
    job_role: JobRole,
    entitlement: EntitlementName,
    department: str,
    peer_count: Annotated[int, Field(ge=0)],
    total_peers: Annotated[int, Field(ge=1)],
    affinity_score: Annotated[
        int | None, Field(ge=0, le=100, description="Computed when omitted")
    ] = None,
) -> dict[str, Any]:
    body = _drop_none(
        {
            "department": department,
            "peer_count": peer_count,
            "total_peers": total_peers,
            "affinity_score": affinity_score,
        }
    )
    return await _request("PUT", _row_path(job_role, entitlement), json=body)


@mcp.tool(
    annotations=DESTRUCTIVE,
    title="Delete a peer affinity row",
    description="Permanently remove an affinity row. This cannot be undone.",
)
async def delete_peer_affinity(job_role: JobRole, entitlement: EntitlementName) -> dict[str, Any]:
    await _request("DELETE", _row_path(job_role, entitlement))
    return {"deleted": {"job_role": job_role, "entitlement": entitlement}}


@mcp.tool(
    annotations=READ_ONLY,
    title="API health",
    description="Check that the Peer Affinity API is reachable and which data file it serves.",
)
async def api_health() -> dict[str, Any]:
    return await _request("GET", "/health")


@mcp.resource(
    "peer-affinity://all",
    name="All peer affinity rows",
    description="The full peer affinity table as JSON.",
    mime_type="application/json",
)
async def all_peer_affinity() -> list[dict[str, Any]]:
    return await _request("GET", "/peer-affinity", params={"limit": 1000})


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
