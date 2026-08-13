"""FastAPI CRUD backend over new_joiners.json."""

import json
import os
import tempfile
import threading
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from config import settings

DATA_FILE = settings.new_joiners_file

# The JSON file is the single source of truth; the lock keeps concurrent
# requests from interleaving a read-modify-write cycle.
_lock = threading.Lock()


class NewJoinerBase(BaseModel):
    name: str = Field(min_length=1)
    department: str = Field(min_length=1)
    job_role: str = Field(min_length=1)
    job_level: str = Field(min_length=1)
    location: str = Field(min_length=1)
    manager_id: str = Field(min_length=1)
    cost_center: str = Field(min_length=1)
    start_date: date


class NewJoiner(NewJoinerBase):
    employee_id: str = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "employee_id": "NJ1011",
                "name": "Meera Krishnan",
                "department": "Finance",
                "job_role": "Financial Analyst",
                "job_level": "L2",
                "location": "Chennai",
                "manager_id": "MGR100",
                "cost_center": "FIN001",
                "start_date": "2026-09-01",
            }
        }
    )


class NewJoinerUpdate(BaseModel):
    """Every field optional, for PATCH."""

    name: str | None = Field(default=None, min_length=1)
    department: str | None = Field(default=None, min_length=1)
    job_role: str | None = Field(default=None, min_length=1)
    job_level: str | None = Field(default=None, min_length=1)
    location: str | None = Field(default=None, min_length=1)
    manager_id: str | None = Field(default=None, min_length=1)
    cost_center: str | None = Field(default=None, min_length=1)
    start_date: date | None = None


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
    raise HTTPException(status_code=404, detail=f"No new joiner with employee_id {employee_id!r}")


def serialize(joiner: NewJoiner) -> dict:
    # model_dump_json handles the date -> "YYYY-MM-DD" conversion; the reorder
    # keeps employee_id first, matching the existing rows in the file.
    record = json.loads(joiner.model_dump_json())
    return {"employee_id": record.pop("employee_id"), **record}


app = FastAPI(
    title="New Joiners API",
    description="CRUD operations backed by new_joiners.json",
    version="1.0.0",
)


@app.get("/new-joiners", response_model=list[NewJoiner], tags=["new-joiners"])
def list_new_joiners(
    department: Annotated[str | None, Query(description="Case-insensitive exact match")] = None,
    location: str | None = None,
    job_level: str | None = None,
    manager_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List new joiners, with optional filters and pagination."""
    with _lock:
        records = read_all()

    filters = {
        "department": department,
        "location": location,
        "job_level": job_level,
        "manager_id": manager_id,
    }
    for field, value in filters.items():
        if value is not None:
            records = [r for r in records if str(r.get(field, "")).lower() == value.lower()]

    return records[offset : offset + limit]


@app.get("/new-joiners/{employee_id}", response_model=NewJoiner, tags=["new-joiners"])
def get_new_joiner(employee_id: str):
    with _lock:
        records = read_all()
        return records[find_index(records, employee_id)]


@app.post(
    "/new-joiners",
    response_model=NewJoiner,
    status_code=status.HTTP_201_CREATED,
    tags=["new-joiners"],
)
def create_new_joiner(joiner: NewJoiner):
    with _lock:
        records = read_all()
        if any(r.get("employee_id") == joiner.employee_id for r in records):
            raise HTTPException(
                status_code=409,
                detail=f"employee_id {joiner.employee_id!r} already exists",
            )
        record = serialize(joiner)
        records.append(record)
        write_all(records)
    return record


@app.put("/new-joiners/{employee_id}", response_model=NewJoiner, tags=["new-joiners"])
def replace_new_joiner(
    employee_id: str,
    joiner: Annotated[NewJoinerBase, Body()],
):
    """Full replace. The employee_id comes from the path and cannot be changed."""
    with _lock:
        records = read_all()
        idx = find_index(records, employee_id)
        record = serialize(NewJoiner(employee_id=employee_id, **joiner.model_dump()))
        records[idx] = record
        write_all(records)
    return record


@app.patch("/new-joiners/{employee_id}", response_model=NewJoiner, tags=["new-joiners"])
def update_new_joiner(employee_id: str, patch: NewJoinerUpdate):
    """Partial update: only the fields present in the body are changed."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Request body contains no fields to update")

    with _lock:
        records = read_all()
        idx = find_index(records, employee_id)
        merged = {**records[idx], **changes}
        record = serialize(NewJoiner(**merged))
        records[idx] = record
        write_all(records)
    return record


@app.delete("/new-joiners/{employee_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["new-joiners"])
def delete_new_joiner(employee_id: str):
    with _lock:
        records = read_all()
        idx = find_index(records, employee_id)
        records.pop(idx)
        write_all(records)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "data_file": str(DATA_FILE), "exists": DATA_FILE.exists()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
