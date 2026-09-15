"""FastAPI CRUD backend over the access_requests table in Postgres.

    uvicorn main:app --reload --port 8000

This service only *stores* access requests. It does not decide whether a
request needs approval and it does not grant anything -- the caller works
that out and records the verdict here. Deliberately no state machine
either: any status may follow any other, because the caller drives the
transitions.
"""

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Body, FastAPI, HTTPException, Query, Response, status as http_status
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import settings

# request_id is the primary key and is minted here, never by the caller.
_ID_PREFIX = "REQ"
_ID_PATTERN = re.compile(rf"^{_ID_PREFIX}(\d+)$")

# Serializes "find the highest existing id, then insert the next one" against
# itself, so two concurrent creates can never mint the same request_id. An
# advisory lock rather than a table lock, since inserts don't need to block
# on anything but this.
_ID_LOCK_KEY = 8199251  # arbitrary, only needs to be unique within this DB

pool = ConnectionPool(
    settings.database_url,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pool.open()
    try:
        yield
    finally:
        pool.close()


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


def split_conflicts(value: str) -> list[str]:
    """The table stores sod_conflicts as a single ';'-separated string."""
    return [part.strip() for part in value.split(";") if part.strip()]


class AccessRequestBase(BaseModel):
    """Everything the caller supplies. request_id and created_at are ours."""

    requester_id: str = Field(default="", description="Who raised it, e.g. 'EMP002' or 'HR001'")
    requester_type: RequesterType
    subject_id: str = Field(default="", description="Who the access is for, e.g. 'NJ1004'")
    subject_type: SubjectType
    entitlement_id: str = ""
    entitlement_name: str = Field(
        default="", description="Stored directly so an inbox screen needs no lookup"
    )
    application: str = ""
    risk_score: int | None = None
    risk_category: str = ""
    approval_required: bool = False
    policy_basis: str = Field(
        default="", description="Why, in plain text, e.g. 'POL005 (Risk Review - risk_score >= 70)'"
    )
    sod_conflicts: str = Field(
        default="", description="Semicolon-separated, e.g. 'SOD002:AUDIT_TOOL'. Empty when clean."
    )
    approver_id: str = Field(default="", description="The manager's id; empty when none on record")
    status: RequestStatus
    justification: str = ""
    decision_note: str = Field(default="", description="The manager's reason; empty until decided")
    decided_at: str = Field(default="", description="ISO-8601; empty until a decision")
    granted_at: str = Field(default="", description="ISO-8601; empty until provisioned")

    @field_validator("sod_conflicts")
    @classmethod
    def _normalize_conflicts(cls, value: str) -> str:
        # Re-join the parsed tokens so stray whitespace and empty
        # segments ("A;;B ") never reach the table.
        return ";".join(split_conflicts(value))


class AccessRequest(AccessRequestBase):
    request_id: str = Field(min_length=1)
    created_at: str = Field(default="", description="ISO-8601, stamped by the API on create")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "request_id": "REQ0001",
                "requester_id": "EMP002",
                "requester_type": "EMPLOYEE",
                "subject_id": "EMP002",
                "subject_type": "IDENTITY",
                "entitlement_id": "ENT008",
                "entitlement_name": "RSA_GRC",
                "application": "RSA Archer",
                "risk_score": 85,
                "risk_category": "Critical",
                "approval_required": True,
                "policy_basis": "POL005 (Risk Review - risk_score >= 70)",
                "sod_conflicts": "SOD002:AUDIT_TOOL",
                "approver_id": "EMP001",
                "status": "PENDING_APPROVAL",
                "justification": "Needed for the quarterly GRC review",
                "decision_note": "",
                "created_at": "2026-08-21T09:30:00Z",
                "decided_at": "",
                "granted_at": "",
            }
        }
    )


class AccessRequestUpdate(BaseModel):
    """Every field optional, for PATCH."""

    requester_id: str | None = None
    requester_type: RequesterType | None = None
    subject_id: str | None = None
    subject_type: SubjectType | None = None
    entitlement_id: str | None = None
    entitlement_name: str | None = None
    application: str | None = None
    risk_score: int | None = None
    risk_category: str | None = None
    approval_required: bool | None = None
    policy_basis: str | None = None
    sod_conflicts: str | None = None
    approver_id: str | None = None
    status: RequestStatus | None = None
    justification: str | None = None
    decision_note: str | None = None
    decided_at: str | None = None
    granted_at: str | None = None

    @field_validator("sod_conflicts")
    @classmethod
    def _normalize_conflicts(cls, value: str | None) -> str | None:
        return value if value is None else ";".join(split_conflicts(value))


def find_or_404(cur: psycopg.Cursor, request_id: str) -> dict[str, Any]:
    cur.execute("SELECT * FROM access_requests WHERE request_id = %s", (request_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No access request with request_id {request_id!r}")
    return row


def next_request_id(cur: psycopg.Cursor) -> str:
    # Highest existing number + 1 rather than count(*) + 1, so deleting
    # REQ0003 can never mint a second REQ0003 later. Safe to call only while
    # holding _ID_LOCK_KEY for the surrounding transaction.
    cur.execute("SELECT request_id FROM access_requests WHERE request_id ~ %s", (rf"^{_ID_PREFIX}\d+$",))
    highest = 0
    for row in cur.fetchall():
        match = _ID_PATTERN.match(row["request_id"])
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{_ID_PREFIX}{highest + 1:04d}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


app = FastAPI(
    title="Requests API",
    description="CRUD operations backed by the access_requests table in Postgres",
    version="1.0.0",
    root_path=settings.root_path,
    lifespan=lifespan,
)


@app.get("/requests", response_model=list[AccessRequest], tags=["requests"])
def list_requests(
    response: Response,
    requester_id: Annotated[
        list[str] | None, Query(description="Case-insensitive exact match; repeatable")
    ] = None,
    approver_id: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    subject_id: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    status: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List access requests, oldest first, with optional filters and pagination.

    Serves both the approver's inbox (approver_id + status) and a
    requester's own history (requester_id).

    Every filter is repeatable and ORs its own values, so one request can
    cover a whole cohort of subjects; different filters AND together.
    X-Total-Count reports how many matched before pagination.
    """
    filters = {
        "requester_id": requester_id,
        "approver_id": approver_id,
        "subject_id": subject_id,
        "status": status,
    }
    clauses = []
    params: list[Any] = []
    for field, value in filters.items():
        if value:
            # = ANY on a lowered array is the OR-within-a-field case; an empty
            # list means the filter was not supplied, so it is skipped above.
            clauses.append(f"LOWER({field}) = ANY(%s)")
            params.append([v.lower() for v in value])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params += [limit, offset]

    with pool.connection() as conn, conn.cursor() as cur:
        # COUNT(*) OVER () rides along on the same scan, so the caller can
        # tell a full page from a truncated one without a second query.
        cur.execute(
            f"SELECT *, COUNT(*) OVER () AS _total FROM access_requests {where} "
            "ORDER BY request_id LIMIT %s OFFSET %s",
            params,
        )
        rows = cur.fetchall()

        if rows:
            total = rows[0]["_total"]
        else:
            # An empty page carries no window count, and an offset past the
            # end must still report the true total rather than zero.
            cur.execute(
                f"SELECT COUNT(*) AS total FROM access_requests {where}", params[:-2]
            )
            total = cur.fetchone()["total"]

    for row in rows:
        row.pop("_total", None)
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Returned-Count"] = str(len(rows))
    return rows


@app.get("/requests/{request_id}", response_model=AccessRequest, tags=["requests"])
def get_request(request_id: str):
    with pool.connection() as conn, conn.cursor() as cur:
        return find_or_404(cur, request_id)


@app.post(
    "/requests",
    response_model=AccessRequest,
    status_code=http_status.HTTP_201_CREATED,
    tags=["requests"],
)
def create_request(request: Annotated[AccessRequestBase, Body()]):
    """Create a request. request_id and created_at are generated here."""
    data: dict[str, Any] = request.model_dump()
    with pool.connection() as conn, conn.cursor() as cur:
        # Held for the rest of this transaction, so the mint-then-insert below
        # is atomic with respect to any other create running concurrently.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ID_LOCK_KEY,))
        data["request_id"] = next_request_id(cur)
        data["created_at"] = now_iso()

        columns = ", ".join(data.keys())
        placeholders = ", ".join(["%s"] * len(data))
        cur.execute(
            f"INSERT INTO access_requests ({columns}) VALUES ({placeholders}) RETURNING *",
            list(data.values()),
        )
        return cur.fetchone()


@app.put("/requests/{request_id}", response_model=AccessRequest, tags=["requests"])
def replace_request(request_id: str, request: Annotated[AccessRequestBase, Body()]):
    """Full replace. The request_id comes from the path and cannot be changed."""
    data: dict[str, Any] = request.model_dump()
    with pool.connection() as conn, conn.cursor() as cur:
        existing = find_or_404(cur, request_id)
        # created_at records when the request was raised, so a replace keeps
        # the original rather than re-stamping it.
        data["created_at"] = existing["created_at"]

        set_clause = ", ".join(f"{k} = %s" for k in data)
        cur.execute(
            f"UPDATE access_requests SET {set_clause} WHERE request_id = %s RETURNING *",
            [*data.values(), request_id],
        )
        return cur.fetchone()


@app.patch("/requests/{request_id}", response_model=AccessRequest, tags=["requests"])
def update_request(request_id: str, patch: AccessRequestUpdate):
    """Partial update: only the fields present in the body are changed."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Request body contains no fields to update")

    with pool.connection() as conn, conn.cursor() as cur:
        find_or_404(cur, request_id)
        set_clause = ", ".join(f"{k} = %s" for k in changes)
        cur.execute(
            f"UPDATE access_requests SET {set_clause} WHERE request_id = %s RETURNING *",
            [*changes.values(), request_id],
        )
        return cur.fetchone()


@app.delete(
    "/requests/{request_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    tags=["requests"],
)
def delete_request(request_id: str):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM access_requests WHERE request_id = %s", (request_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"No access request with request_id {request_id!r}")


@app.get("/health", tags=["meta"])
def health():
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return {"status": "ok", "database": "connected"}
    except psycopg.Error as exc:
        return {"status": "error", "database": "unreachable", "detail": str(exc)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
