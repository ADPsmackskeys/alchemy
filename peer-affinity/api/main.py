"""FastAPI CRUD backend over peer_affinity_scores.json.

A row has no single-column id, so a record is addressed by the
(job_role, entitlement) pair that uniquely identifies it:

    GET /peer-affinity/Financial%20Analyst/SAP_FIN_DISPLAY

    uvicorn main:app --reload --port 8003
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config import settings

DATA_FILE = settings.peer_affinity_file

# The JSON file is the single source of truth; the lock keeps concurrent
# requests from interleaving a read-modify-write cycle.
_lock = threading.Lock()


class PeerAffinityBase(BaseModel):
    department: str = Field(min_length=1)
    peer_count: int = Field(ge=0, description="Peers in the role holding this entitlement")
    total_peers: int = Field(ge=1, description="Peers in the role overall")
    affinity_score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Percentage of peers holding the entitlement. Computed when omitted.",
    )

    @model_validator(mode="after")
    def _check_counts(self):
        if self.peer_count > self.total_peers:
            raise ValueError("peer_count cannot exceed total_peers")
        if self.affinity_score is None:
            self.affinity_score = round(self.peer_count / self.total_peers * 100)
        return self


class PeerAffinity(PeerAffinityBase):
    job_role: str = Field(min_length=1)
    entitlement: str = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_role": "Risk Analyst",
                "department": "Risk",
                "entitlement": "SAP_FIN_DISPLAY",
                "peer_count": 1,
                "total_peers": 2,
                "affinity_score": 50,
            }
        }
    )


class PeerAffinityUpdate(BaseModel):
    """Every field optional, for PATCH."""

    department: str | None = Field(default=None, min_length=1)
    peer_count: int | None = Field(default=None, ge=0)
    total_peers: int | None = Field(default=None, ge=1)
    affinity_score: int | None = Field(default=None, ge=0, le=100)


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


def matches(record: dict, job_role: str, entitlement: str) -> bool:
    return record.get("job_role") == job_role and record.get("entitlement") == entitlement


def find_index(records: list[dict], job_role: str, entitlement: str) -> int:
    for i, r in enumerate(records):
        if matches(r, job_role, entitlement):
            return i
    raise HTTPException(
        status_code=404,
        detail=f"No peer affinity row for job_role {job_role!r} and entitlement {entitlement!r}",
    )


def serialize(score: PeerAffinity) -> dict:
    # Field order matches the existing rows in the file.
    record = json.loads(score.model_dump_json())
    return {
        "job_role": record["job_role"],
        "department": record["department"],
        "entitlement": record["entitlement"],
        "peer_count": record["peer_count"],
        "total_peers": record["total_peers"],
        "affinity_score": record["affinity_score"],
    }


app = FastAPI(
    title="Peer Affinity API",
    description="CRUD operations backed by peer_affinity_scores.json",
    version="1.0.0",
)


@app.get("/peer-affinity", response_model=list[PeerAffinity], tags=["peer-affinity"])
def list_peer_affinity(
    job_role: Annotated[str | None, Query(description="Case-insensitive exact match")] = None,
    department: str | None = None,
    entitlement: str | None = None,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    max_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List peer affinity rows, with optional filters and pagination."""
    with _lock:
        records = read_all()

    filters = {"job_role": job_role, "department": department, "entitlement": entitlement}
    for field, value in filters.items():
        if value is not None:
            records = [r for r in records if str(r.get(field, "")).lower() == value.lower()]

    if min_score is not None:
        records = [r for r in records if r.get("affinity_score", 0) >= min_score]
    if max_score is not None:
        records = [r for r in records if r.get("affinity_score", 0) <= max_score]

    return records[offset : offset + limit]


@app.get(
    "/peer-affinity/{job_role}/{entitlement}",
    response_model=PeerAffinity,
    tags=["peer-affinity"],
)
def get_peer_affinity(job_role: str, entitlement: str):
    with _lock:
        records = read_all()
        return records[find_index(records, job_role, entitlement)]


@app.post(
    "/peer-affinity",
    response_model=PeerAffinity,
    status_code=status.HTTP_201_CREATED,
    tags=["peer-affinity"],
)
def create_peer_affinity(score: PeerAffinity):
    with _lock:
        records = read_all()
        if any(matches(r, score.job_role, score.entitlement) for r in records):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A row for job_role {score.job_role!r} and "
                    f"entitlement {score.entitlement!r} already exists"
                ),
            )
        record = serialize(score)
        records.append(record)
        write_all(records)
    return record


@app.put(
    "/peer-affinity/{job_role}/{entitlement}",
    response_model=PeerAffinity,
    tags=["peer-affinity"],
)
def replace_peer_affinity(
    job_role: str,
    entitlement: str,
    score: Annotated[PeerAffinityBase, Body()],
):
    """Full replace. The job_role and entitlement come from the path and cannot be changed."""
    with _lock:
        records = read_all()
        idx = find_index(records, job_role, entitlement)
        record = serialize(
            PeerAffinity(job_role=job_role, entitlement=entitlement, **score.model_dump())
        )
        records[idx] = record
        write_all(records)
    return record


@app.patch(
    "/peer-affinity/{job_role}/{entitlement}",
    response_model=PeerAffinity,
    tags=["peer-affinity"],
)
def update_peer_affinity(job_role: str, entitlement: str, patch: PeerAffinityUpdate):
    """Partial update: only the fields present in the body are changed.

    Changing peer_count or total_peers without also supplying affinity_score
    recomputes the score.
    """
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Request body contains no fields to update")

    with _lock:
        records = read_all()
        idx = find_index(records, job_role, entitlement)
        merged = {**records[idx], **changes}
        if "affinity_score" not in changes and {"peer_count", "total_peers"} & changes.keys():
            merged["affinity_score"] = None
        record = serialize(PeerAffinity(**merged))
        records[idx] = record
        write_all(records)
    return record


@app.delete(
    "/peer-affinity/{job_role}/{entitlement}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["peer-affinity"],
)
def delete_peer_affinity(job_role: str, entitlement: str):
    with _lock:
        records = read_all()
        records.pop(find_index(records, job_role, entitlement))
        write_all(records)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "data_file": str(DATA_FILE), "exists": DATA_FILE.exists()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
