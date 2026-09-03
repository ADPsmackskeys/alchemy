"""FastAPI CRUD backend over identities.json.

    uvicorn main:app --reload --port 8000
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import settings

DATA_FILE = settings.identities_file

# The JSON file is the single source of truth; the lock keeps concurrent
# requests from interleaving a read-modify-write cycle.
_lock = threading.Lock()


def split_entitlements(value: str) -> list[str]:
    """The file stores entitlements as a single ';'-separated string."""
    return [part.strip() for part in value.split(";") if part.strip()]


class IdentityBase(BaseModel):
    name: str = Field(min_length=1)
    department: str = Field(min_length=1)
    job_role: str = Field(min_length=1)
    job_level: str = Field(min_length=1)
    location: str = Field(min_length=1)
    manager_id: str = Field(
        default="",
        description="Manager's employee_id, and the approver for this identity's access requests. Empty when none on record.",
    )
    entitlements: str = Field(description="Semicolon-separated entitlement names")

    @field_validator("entitlements")
    @classmethod
    def _normalize_entitlements(cls, value: str) -> str:
        # Re-join the parsed tokens so stray whitespace and empty
        # segments ("A;;B ") never reach the data file.
        return ";".join(split_entitlements(value))


class Identity(IdentityBase):
    employee_id: str = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "employee_id": "EMP011",
                "name": "Divya",
                "department": "Finance",
                "job_role": "Financial Analyst",
                "job_level": "L2",
                "location": "Chennai",
                "manager_id": "EMP001",
                "entitlements": "SAP_FIN_DISPLAY;POWERBI_FINANCE",
            }
        }
    )


class IdentityUpdate(BaseModel):
    """Every field optional, for PATCH."""

    name: str | None = Field(default=None, min_length=1)
    department: str | None = Field(default=None, min_length=1)
    job_role: str | None = Field(default=None, min_length=1)
    job_level: str | None = Field(default=None, min_length=1)
    location: str | None = Field(default=None, min_length=1)
    manager_id: str | None = None
    entitlements: str | None = None

    @field_validator("entitlements")
    @classmethod
    def _normalize_entitlements(cls, value: str | None) -> str | None:
        return value if value is None else ";".join(split_entitlements(value))


def read_all() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise HTTPException(status_code=500, detail="Data file must contain a JSON array")
    return data


def write_all(records: list[dict]) -> None:
    # Write to a temp file in the same directory, then rename, so a crash
    # mid-write can never truncate the original data file.
    fd, tmp_path = tempfile.mkstemp(dir=DATA_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, DATA_FILE)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def find_index(records: list[dict], employee_id: str) -> int:
    for i, r in enumerate(records):
        if r.get("employee_id") == employee_id:
            return i
    raise HTTPException(status_code=404, detail=f"No identity with employee_id {employee_id!r}")


def apply_filters(records: list[dict], filters: dict[str, str | list[str] | None]) -> list[dict]:
    """Case-insensitive whole-value match: OR within a field, AND across fields.

    A filter is either one value or a list of them, so a caller asking about
    five records makes one request instead of five. An explicitly empty list
    means "filter not supplied" and matches everything, which is what a client
    that built its list dynamically sends when it has nothing to narrow by.
    """
    for field, value in filters.items():
        if value is None:
            continue
        wanted = {v.lower() for v in ([value] if isinstance(value, str) else value)}
        if not wanted:
            continue
        records = [r for r in records if str(r.get(field, "")).lower() in wanted]
    return records


def paginate(records: list[dict], limit: int, offset: int, response: Response) -> list[dict]:
    """Return one page and report the pre-pagination total in the headers.

    Without X-Total-Count the caller cannot tell a complete page from a
    truncated one, which is what drives blind offset-walking.
    """
    page = records[offset : offset + limit]
    response.headers["X-Total-Count"] = str(len(records))
    response.headers["X-Returned-Count"] = str(len(page))
    return page

def serialize(identity: Identity) -> dict:
    # The reorder keeps employee_id first, matching the existing rows in the file.
    record = json.loads(identity.model_dump_json())
    return {"employee_id": record.pop("employee_id"), **record}


app = FastAPI(
    title="Identities API",
    description="CRUD operations backed by identities.json",
    version="1.0.0",
    root_path=settings.root_path,
)


@app.get("/identities", response_model=list[Identity], tags=["identities"])
def list_identities(
    response: Response,
    employee_id: Annotated[
        list[str] | None,
        Query(description="Fetch these people by id; repeatable, so one call covers a team"),
    ] = None,
    department: Annotated[
        list[str] | None, Query(description="Case-insensitive exact match; repeatable")
    ] = None,
    location: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    job_level: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    job_role: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    manager_id: Annotated[
        list[str] | None, Query(description="Only identities reporting to these managers")
    ] = None,
    entitlement: Annotated[
        list[str] | None, Query(description="Only identities holding these entitlements")
    ] = None,
    match: Annotated[
        Literal["any", "all"],
        Query(description="Whether `entitlement` means holding ANY of them or ALL of them"),
    ] = "any",
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List identities, with optional filters and pagination.

    Every filter is repeatable and ORs its own values; different filters AND
    together. `entitlement` is the exception where OR is not always what you
    want, so `match=all` asks for identities holding every named entitlement
    rather than any of them.

    employee_id lets one request fetch a set of people, which otherwise took
    one GET /identities/{employee_id} per person.
    """
    with _lock:
        records = read_all()

    records = apply_filters(
        records,
        {
            "employee_id": employee_id,
            "department": department,
            "location": location,
            "job_level": job_level,
            "job_role": job_role,
            "manager_id": manager_id,
        },
    )

    if entitlement:
        wanted = {e.lower() for e in entitlement}
        combine = set.issuperset if match == "all" else lambda held, w: bool(held & w)
        records = [
            r
            for r in records
            if combine(
                {e.lower() for e in split_entitlements(str(r.get("entitlements", "")))}, wanted
            )
        ]

    return paginate(records, limit, offset, response)


@app.get("/identities/{employee_id}", response_model=Identity, tags=["identities"])
def get_identity(employee_id: str):
    with _lock:
        records = read_all()
        return records[find_index(records, employee_id)]


@app.get("/identities/{employee_id}/entitlements", response_model=list[str], tags=["identities"])
def get_identity_entitlements(employee_id: str):
    """The identity's entitlements string, parsed into a list."""
    with _lock:
        records = read_all()
        record = records[find_index(records, employee_id)]
    return split_entitlements(str(record.get("entitlements", "")))


@app.post(
    "/identities",
    response_model=Identity,
    status_code=status.HTTP_201_CREATED,
    tags=["identities"],
)
def create_identity(identity: Identity):
    with _lock:
        records = read_all()
        if any(r.get("employee_id") == identity.employee_id for r in records):
            raise HTTPException(
                status_code=409,
                detail=f"employee_id {identity.employee_id!r} already exists",
            )
        record = serialize(identity)
        records.append(record)
        write_all(records)
    return record


@app.put("/identities/{employee_id}", response_model=Identity, tags=["identities"])
def replace_identity(employee_id: str, identity: Annotated[IdentityBase, Body()]):
    """Full replace. The employee_id comes from the path and cannot be changed."""
    with _lock:
        records = read_all()
        idx = find_index(records, employee_id)
        record = serialize(Identity(employee_id=employee_id, **identity.model_dump()))
        records[idx] = record
        write_all(records)
    return record


@app.patch("/identities/{employee_id}", response_model=Identity, tags=["identities"])
def update_identity(employee_id: str, patch: IdentityUpdate):
    """Partial update: only the fields present in the body are changed."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Request body contains no fields to update")

    with _lock:
        records = read_all()
        idx = find_index(records, employee_id)
        record = serialize(Identity(**{**records[idx], **changes}))
        records[idx] = record
        write_all(records)
    return record


@app.delete(
    "/identities/{employee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["identities"],
)
def delete_identity(employee_id: str):
    with _lock:
        records = read_all()
        records.pop(find_index(records, employee_id))
        write_all(records)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "data_file": str(DATA_FILE), "exists": DATA_FILE.exists()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
