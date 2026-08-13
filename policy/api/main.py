"""FastAPI CRUD backend over policy_rules.json.

    uvicorn main:app --reload --port 8004
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from config import settings

DATA_FILE = settings.policy_rules_file

# The JSON file is the single source of truth; the lock keeps concurrent
# requests from interleaving a read-modify-write cycle.
_lock = threading.Lock()

PolicyType = Literal["ALLOW", "DENY", "HUMAN_APPROVAL"]


class PolicyBase(BaseModel):
    policy_name: str = Field(min_length=1)
    type: PolicyType
    rule: str = Field(min_length=1)


class Policy(PolicyBase):
    policy_id: str = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "policy_id": "POL008",
                "policy_name": "SoD Block",
                "type": "DENY",
                "rule": "SAP_VENDOR_CREATE + SAP_PAYMENT_APPROVER",
            }
        }
    )


class PolicyUpdate(BaseModel):
    """Every field optional, for PATCH."""

    policy_name: str | None = Field(default=None, min_length=1)
    type: PolicyType | None = None
    rule: str | None = Field(default=None, min_length=1)


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


def find_index(records: list[dict], policy_id: str) -> int:
    for i, r in enumerate(records):
        if r.get("policy_id") == policy_id:
            return i
    raise HTTPException(status_code=404, detail=f"No policy with policy_id {policy_id!r}")


def serialize(policy: Policy) -> dict:
    # The reorder keeps policy_id first, matching the existing rows in the file.
    record = json.loads(policy.model_dump_json())
    return {"policy_id": record.pop("policy_id"), **record}


app = FastAPI(
    title="Policy API",
    description="CRUD operations backed by policy_rules.json",
    version="1.0.0",
)


@app.get("/policies", response_model=list[Policy], tags=["policies"])
def list_policies(
    type: Annotated[PolicyType | None, Query(description="Filter by policy type")] = None,
    policy_name: Annotated[str | None, Query(description="Case-insensitive exact match")] = None,
    rule_contains: Annotated[
        str | None, Query(description="Case-insensitive substring match on the rule")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List policy rules, with optional filters and pagination."""
    with _lock:
        records = read_all()

    filters = {"type": type, "policy_name": policy_name}
    for field, value in filters.items():
        if value is not None:
            records = [r for r in records if str(r.get(field, "")).lower() == value.lower()]

    if rule_contains is not None:
        needle = rule_contains.lower()
        records = [r for r in records if needle in str(r.get("rule", "")).lower()]

    return records[offset : offset + limit]


@app.get("/policies/{policy_id}", response_model=Policy, tags=["policies"])
def get_policy(policy_id: str):
    with _lock:
        records = read_all()
        return records[find_index(records, policy_id)]


@app.post(
    "/policies",
    response_model=Policy,
    status_code=status.HTTP_201_CREATED,
    tags=["policies"],
)
def create_policy(policy: Policy):
    with _lock:
        records = read_all()
        if any(r.get("policy_id") == policy.policy_id for r in records):
            raise HTTPException(
                status_code=409,
                detail=f"policy_id {policy.policy_id!r} already exists",
            )
        record = serialize(policy)
        records.append(record)
        write_all(records)
    return record


@app.put("/policies/{policy_id}", response_model=Policy, tags=["policies"])
def replace_policy(policy_id: str, policy: Annotated[PolicyBase, Body()]):
    """Full replace. The policy_id comes from the path and cannot be changed."""
    with _lock:
        records = read_all()
        idx = find_index(records, policy_id)
        record = serialize(Policy(policy_id=policy_id, **policy.model_dump()))
        records[idx] = record
        write_all(records)
    return record


@app.patch("/policies/{policy_id}", response_model=Policy, tags=["policies"])
def update_policy(policy_id: str, patch: PolicyUpdate):
    """Partial update: only the fields present in the body are changed."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Request body contains no fields to update")

    with _lock:
        records = read_all()
        idx = find_index(records, policy_id)
        record = serialize(Policy(**{**records[idx], **changes}))
        records[idx] = record
        write_all(records)
    return record


@app.delete(
    "/policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["policies"],
)
def delete_policy(policy_id: str):
    with _lock:
        records = read_all()
        records.pop(find_index(records, policy_id))
        write_all(records)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "data_file": str(DATA_FILE), "exists": DATA_FILE.exists()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
