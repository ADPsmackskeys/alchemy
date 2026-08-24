"""MCP server exposing the Requests API as tools.

This is a client of the FastAPI service, not a second reader/writer of
access_requests.json. Going through HTTP keeps a single process owning the
data file, so the API's write lock still means something -- two processes
with their own locks would happily interleave a read-modify-write cycle and
lose records. It also keeps validation and the 404 rules in one place.

Start the API first, then this server:

    cd ../api && python main.py
    python main.py

MCP_TRANSPORT decides how this server listens (default streamable-http on
MCP_PORT 8000 -- the same as the API's 8000, since the two are expected to
run on separate hosts):

    claude mcp add --transport http requests http://127.0.0.1:8000/mcp

    # or, for a client that spawns the process itself:
    MCP_TRANSPORT=stdio
    claude mcp add requests -- python /path/to/requests/mcp/main.py

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

API_URL = settings.requests_api_url
TIMEOUT = settings.requests_api_timeout

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
    "requests",
    version="1.0.0",
    instructions=(
        "The access request tracker: one record per 'person X wants entitlement "
        "Y', carrying its status from raised to decided to granted. Records are "
        "addressed by request_id (e.g. 'REQ0001'), which the API mints on create. "
        "This service only stores requests -- it does not decide whether approval "
        "is needed and it does not grant anything; the caller works that out and "
        "records the verdict here. Use list_access_requests with approver_id + "
        "status='PENDING_APPROVAL' for a manager's inbox, or requester_id for one "
        "person's own history."
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
            f"Cannot reach the Requests API at {API_URL} ({exc.__class__.__name__}). "
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

RequesterType = Literal["EMPLOYEE", "HR"]
SubjectType = Literal["IDENTITY", "NEW_JOINER"]
RequestStatus = Literal[
    "AUTO_GRANTED",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "GRANTED",
    "BLOCKED_NO_APPROVER",
    "PROVISIONING_FAILED",
]

RequestId = Annotated[str, Field(description="Access request identifier, e.g. 'REQ0001'")]
SodConflicts = Annotated[
    str,
    Field(description="Semicolon-separated conflicts, e.g. 'SOD002:AUDIT_TOOL'. Empty when clean."),
]
Timestamp = Annotated[str, Field(description="ISO-8601 timestamp, e.g. '2026-08-21T09:30:00Z'")]


@mcp.tool(
    annotations=READ_ONLY,
    title="List access requests",
    description=(
        "List access requests, oldest first. All filters are optional and match "
        "case-insensitively on the whole value. This one tool serves both the "
        "manager's inbox (approver_id plus status='PENDING_APPROVAL') and a "
        "requester's own history (requester_id). Note requester_id is who *raised* "
        "the request and subject_id is who the access is *for* -- they differ when "
        "HR onboards someone. Page with limit/offset."
    ),
)
async def list_access_requests(
    requester_id: Annotated[str | None, Field(description="Who raised it, e.g. 'EMP002'")] = None,
    approver_id: Annotated[str | None, Field(description="The manager, e.g. 'EMP001'")] = None,
    subject_id: Annotated[
        str | None, Field(description="Who the access is for, e.g. 'NJ1004'")
    ] = None,
    status: RequestStatus | None = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> list[dict[str, Any]]:
    params = _drop_none(
        {
            "requester_id": requester_id,
            "approver_id": approver_id,
            "subject_id": subject_id,
            "status": status,
            "limit": limit,
            "offset": offset,
        }
    )
    return await _request("GET", "/requests", params=params)


@mcp.tool(
    annotations=READ_ONLY,
    title="Get an access request",
    description="Fetch one access request by request_id. Errors if no such record exists.",
)
async def get_access_request(request_id: RequestId) -> dict[str, Any]:
    return await _request("GET", f"/requests/{quote(request_id, safe='')}")


@mcp.tool(
    annotations=WRITE,
    title="Create an access request",
    description=(
        "Record one person's request for one entitlement. The API mints request_id "
        "and stamps created_at -- do not pass them. requester_type, subject_type "
        "and status are required and must be one of their listed values; every "
        "other field defaults to empty (or null for risk_score) if omitted. "
        "This tool only files the request: decide approval_required and the "
        "starting status yourself, then record them here."
    ),
)
async def create_access_request(
    requester_type: RequesterType,
    subject_type: SubjectType,
    status: RequestStatus,
    requester_id: Annotated[str, Field(description="Who raised it, e.g. 'EMP002'")] = "",
    subject_id: Annotated[str, Field(description="Who the access is for, e.g. 'NJ1004'")] = "",
    entitlement_id: Annotated[str, Field(description="e.g. 'ENT008'")] = "",
    entitlement_name: Annotated[str, Field(description="e.g. 'RSA_GRC'")] = "",
    application: Annotated[str, Field(description="e.g. 'RSA Archer'")] = "",
    risk_score: Annotated[int | None, Field(description="Copied in at request time")] = None,
    risk_category: Annotated[str, Field(description="Low / Medium / High / Critical")] = "",
    approval_required: Annotated[bool, Field(description="The verdict, decided by you")] = False,
    policy_basis: Annotated[
        str, Field(description="Why, e.g. 'POL005 (Risk Review - risk_score >= 70)'")
    ] = "",
    sod_conflicts: SodConflicts = "",
    approver_id: Annotated[str, Field(description="The manager's id; empty if none")] = "",
    justification: Annotated[str, Field(description="Free text from whoever asked")] = "",
) -> dict[str, Any]:
    return await _request(
        "POST",
        "/requests",
        json={
            "requester_id": requester_id,
            "requester_type": requester_type,
            "subject_id": subject_id,
            "subject_type": subject_type,
            "entitlement_id": entitlement_id,
            "entitlement_name": entitlement_name,
            "application": application,
            "risk_score": risk_score,
            "risk_category": risk_category,
            "approval_required": approval_required,
            "policy_basis": policy_basis,
            "sod_conflicts": sod_conflicts,
            "approver_id": approver_id,
            "status": status,
            "justification": justification,
        },
    )


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Update an access request",
    description=(
        "Move an access request along: only the fields you pass are changed, the "
        "rest keep their current values. This is how a decision is recorded -- set "
        "status plus decision_note and decided_at on approve/reject, and "
        "granted_at once the access is actually applied. request_id and created_at "
        "cannot be changed. Pass at least one field."
    ),
)
async def update_access_request(
    request_id: RequestId,
    status: RequestStatus | None = None,
    decision_note: Annotated[
        str | None, Field(description="The manager's reason for approving or rejecting")
    ] = None,
    decided_at: Timestamp | None = None,
    granted_at: Timestamp | None = None,
    approver_id: Annotated[str | None, Field(description="The manager's id")] = None,
) -> dict[str, Any]:
    changes = _drop_none(
        {
            "status": status,
            "decision_note": decision_note,
            "decided_at": decided_at,
            "granted_at": granted_at,
            "approver_id": approver_id,
        }
    )
    if not changes:
        raise ToolError("Pass at least one field to change")
    return await _request("PATCH", f"/requests/{quote(request_id, safe='')}", json=changes)


@mcp.tool(
    annotations=IDEMPOTENT_WRITE,
    title="Replace an access request",
    description=(
        "Overwrite every field of an access request. Fields you omit are NOT "
        "preserved -- prefer update_access_request unless you intend a full "
        "replace. request_id and created_at are kept as they are."
    ),
)
async def replace_access_request(
    request_id: RequestId,
    requester_type: RequesterType,
    subject_type: SubjectType,
    status: RequestStatus,
    requester_id: str = "",
    subject_id: str = "",
    entitlement_id: str = "",
    entitlement_name: str = "",
    application: str = "",
    risk_score: int | None = None,
    risk_category: str = "",
    approval_required: bool = False,
    policy_basis: str = "",
    sod_conflicts: SodConflicts = "",
    approver_id: str = "",
    justification: str = "",
    decision_note: str = "",
    decided_at: str = "",
    granted_at: str = "",
) -> dict[str, Any]:
    return await _request(
        "PUT",
        f"/requests/{quote(request_id, safe='')}",
        json={
            "requester_id": requester_id,
            "requester_type": requester_type,
            "subject_id": subject_id,
            "subject_type": subject_type,
            "entitlement_id": entitlement_id,
            "entitlement_name": entitlement_name,
            "application": application,
            "risk_score": risk_score,
            "risk_category": risk_category,
            "approval_required": approval_required,
            "policy_basis": policy_basis,
            "sod_conflicts": sod_conflicts,
            "approver_id": approver_id,
            "status": status,
            "justification": justification,
            "decision_note": decision_note,
            "decided_at": decided_at,
            "granted_at": granted_at,
        },
    )


@mcp.tool(
    annotations=DESTRUCTIVE,
    title="Delete an access request",
    description=(
        "Permanently remove an access request, losing the record that it was ever "
        "raised. This cannot be undone -- to close a request, prefer "
        "update_access_request with a REJECTED status."
    ),
)
async def delete_access_request(request_id: RequestId) -> dict[str, Any]:
    await _request("DELETE", f"/requests/{quote(request_id, safe='')}")
    return {"deleted": request_id}


@mcp.tool(
    annotations=READ_ONLY,
    title="API health",
    description="Check that the Requests API is reachable and which data file it serves.",
)
async def api_health() -> dict[str, Any]:
    return await _request("GET", "/health")


@mcp.resource(
    "requests://all",
    name="All access requests",
    description="The full access request log as JSON.",
    mime_type="application/json",
)
async def all_access_requests() -> list[dict[str, Any]]:
    return await _request("GET", "/requests", params={"limit": 1000})


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
