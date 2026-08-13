"""FastAPI CRUD backend over identities.json.

    uvicorn main:app --reload --port 8002
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query, status
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


def serialize(identity: Identity) -> dict:
    # The reorder keeps employee_id first, matching the existing rows in the file.
    record = json.loads(identity.model_dump_json())
    return {"employee_id": record.pop("employee_id"), **record}


app = FastAPI(
    title="Identities API",
    description="CRUD operations backed by identities.json",
    version="1.0.0",
)


@app.get("/identities", response_model=list[Identity], tags=["identities"])
def list_identities(
    department: Annotated[str | None, Query(description="Case-insensitive exact match")] = None,
    location: str | None = None,
    job_level: str | None = None,
    job_role: str | None = None,
    entitlement: Annotated[
        str | None, Query(description="Only identities holding this entitlement")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List identities, with optional filters and pagination."""
    with _lock:
        records = read_all()

    filters = {
        "department": department,
        "location": location,
        "job_level": job_level,
        "job_role": job_role,
    }
    for field, value in filters.items():
        if value is not None:
            records = [r for r in records if str(r.get(field, "")).lower() == value.lower()]

    if entitlement is not None:
        wanted = entitlement.lower()
        records = [
            r
            for r in records
            if wanted in [e.lower() for e in split_entitlements(str(r.get("entitlements", "")))]
        ]

    return records[offset : offset + limit]


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
