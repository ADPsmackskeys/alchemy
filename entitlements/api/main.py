"""FastAPI CRUD backend over entitlement_catalog.json and entitlement_risk_scores.json.

    uvicorn main:app --reload --port 8001
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

CATALOG_FILE = settings.entitlement_catalog_file
RISK_SCORES_FILE = settings.entitlement_risk_scores_file

# The JSON files are the single source of truth; the lock keeps concurrent
# requests from interleaving a read-modify-write cycle.
_lock = threading.Lock()

RiskCategory = Literal["Low", "Medium", "High", "Critical"]


class EntitlementBase(BaseModel):
    entitlement_name: str = Field(min_length=1)
    application: str = Field(min_length=1)
    owner: str = Field(min_length=1)


class Entitlement(EntitlementBase):
    entitlement_id: str = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "entitlement_id": "ENT011",
                "entitlement_name": "SHAREPOINT_AUDIT",
                "application": "SharePoint",
                "owner": "Audit IT",
            }
        }
    )


class EntitlementUpdate(BaseModel):
    """Every field optional, for PATCH."""

    entitlement_name: str | None = Field(default=None, min_length=1)
    application: str | None = Field(default=None, min_length=1)
    owner: str | None = Field(default=None, min_length=1)


class RiskScoreBase(BaseModel):
    application: str = Field(min_length=1)
    risk_score: int = Field(ge=0, le=100)
    risk_category: RiskCategory


class RiskScore(RiskScoreBase):
    # Joins to the catalog on entitlement_name.
    entitlement_name: str = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "entitlement_name": "SHAREPOINT_AUDIT",
                "application": "SharePoint",
                "risk_score": 20,
                "risk_category": "Low",
            }
        }
    )


class RiskScoreUpdate(BaseModel):
    """Every field optional, for PATCH."""

    application: str | None = Field(default=None, min_length=1)
    risk_score: int | None = Field(default=None, ge=0, le=100)
    risk_category: RiskCategory | None = None


def read_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise HTTPException(status_code=500, detail=f"{path.name} must contain a JSON array")
    return data


def write_all(path: Path, records: list[dict]) -> None:
    # Write to a temp file in the same directory, then rename, so a crash
    # mid-write can never truncate the original data file.
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def find_index(records: list[dict], key: str, value: str) -> int:
    for i, r in enumerate(records):
        if r.get(key) == value:
            return i
    raise HTTPException(status_code=404, detail=f"No record with {key} {value!r}")


def serialize(model: BaseModel, key: str) -> dict:
    # The reorder keeps the identifier first, matching the existing rows in the file.
    record = json.loads(model.model_dump_json())
    return {key: record.pop(key), **record}


def apply_filters(records: list[dict], filters: dict[str, str | None]) -> list[dict]:
    for field, value in filters.items():
        if value is not None:
            records = [r for r in records if str(r.get(field, "")).lower() == value.lower()]
    return records


app = FastAPI(
    title="Entitlements API",
    description="CRUD operations backed by entitlement_catalog.json and entitlement_risk_scores.json",
    version="1.0.0",
)


# --------------------------------------------------------------------------
# Entitlement catalog
# --------------------------------------------------------------------------


@app.get("/entitlements", response_model=list[Entitlement], tags=["entitlements"])
def list_entitlements(
    application: Annotated[str | None, Query(description="Case-insensitive exact match")] = None,
    owner: str | None = None,
    entitlement_name: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List catalog entitlements, with optional filters and pagination."""
    with _lock:
        records = read_all(CATALOG_FILE)

    records = apply_filters(
        records,
        {"application": application, "owner": owner, "entitlement_name": entitlement_name},
    )
    return records[offset : offset + limit]


@app.get("/entitlements/{entitlement_id}", response_model=Entitlement, tags=["entitlements"])
def get_entitlement(entitlement_id: str):
    with _lock:
        records = read_all(CATALOG_FILE)
        return records[find_index(records, "entitlement_id", entitlement_id)]


@app.post(
    "/entitlements",
    response_model=Entitlement,
    status_code=status.HTTP_201_CREATED,
    tags=["entitlements"],
)
def create_entitlement(entitlement: Entitlement):
    with _lock:
        records = read_all(CATALOG_FILE)
        if any(r.get("entitlement_id") == entitlement.entitlement_id for r in records):
            raise HTTPException(
                status_code=409,
                detail=f"entitlement_id {entitlement.entitlement_id!r} already exists",
            )
        record = serialize(entitlement, "entitlement_id")
        records.append(record)
        write_all(CATALOG_FILE, records)
    return record


@app.put("/entitlements/{entitlement_id}", response_model=Entitlement, tags=["entitlements"])
def replace_entitlement(entitlement_id: str, entitlement: Annotated[EntitlementBase, Body()]):
    """Full replace. The entitlement_id comes from the path and cannot be changed."""
    with _lock:
        records = read_all(CATALOG_FILE)
        idx = find_index(records, "entitlement_id", entitlement_id)
        record = serialize(
            Entitlement(entitlement_id=entitlement_id, **entitlement.model_dump()),
            "entitlement_id",
        )
        records[idx] = record
        write_all(CATALOG_FILE, records)
    return record


@app.patch("/entitlements/{entitlement_id}", response_model=Entitlement, tags=["entitlements"])
def update_entitlement(entitlement_id: str, patch: EntitlementUpdate):
    """Partial update: only the fields present in the body are changed."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Request body contains no fields to update")

    with _lock:
        records = read_all(CATALOG_FILE)
        idx = find_index(records, "entitlement_id", entitlement_id)
        record = serialize(Entitlement(**{**records[idx], **changes}), "entitlement_id")
        records[idx] = record
        write_all(CATALOG_FILE, records)
    return record


@app.delete(
    "/entitlements/{entitlement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["entitlements"],
)
def delete_entitlement(entitlement_id: str):
    with _lock:
        records = read_all(CATALOG_FILE)
        records.pop(find_index(records, "entitlement_id", entitlement_id))
        write_all(CATALOG_FILE, records)


# --------------------------------------------------------------------------
# Entitlement risk scores
# --------------------------------------------------------------------------


@app.get("/risk-scores", response_model=list[RiskScore], tags=["risk-scores"])
def list_risk_scores(
    application: Annotated[str | None, Query(description="Case-insensitive exact match")] = None,
    risk_category: RiskCategory | None = None,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    max_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List risk scores, with optional filters and pagination."""
    with _lock:
        records = read_all(RISK_SCORES_FILE)

    records = apply_filters(records, {"application": application, "risk_category": risk_category})
    if min_score is not None:
        records = [r for r in records if r.get("risk_score", 0) >= min_score]
    if max_score is not None:
        records = [r for r in records if r.get("risk_score", 0) <= max_score]

    return records[offset : offset + limit]


@app.get("/risk-scores/{entitlement_name}", response_model=RiskScore, tags=["risk-scores"])
def get_risk_score(entitlement_name: str):
    with _lock:
        records = read_all(RISK_SCORES_FILE)
        return records[find_index(records, "entitlement_name", entitlement_name)]


@app.post(
    "/risk-scores",
    response_model=RiskScore,
    status_code=status.HTTP_201_CREATED,
    tags=["risk-scores"],
)
def create_risk_score(score: RiskScore):
    with _lock:
        records = read_all(RISK_SCORES_FILE)
        if any(r.get("entitlement_name") == score.entitlement_name for r in records):
            raise HTTPException(
                status_code=409,
                detail=f"entitlement {score.entitlement_name!r} already has a risk score",
            )
        record = serialize(score, "entitlement_name")
        records.append(record)
        write_all(RISK_SCORES_FILE, records)
    return record


@app.put("/risk-scores/{entitlement_name}", response_model=RiskScore, tags=["risk-scores"])
def replace_risk_score(entitlement_name: str, score: Annotated[RiskScoreBase, Body()]):
    """Full replace. The entitlement_name comes from the path and cannot be changed."""
    with _lock:
        records = read_all(RISK_SCORES_FILE)
        idx = find_index(records, "entitlement_name", entitlement_name)
        record = serialize(RiskScore(entitlement_name=entitlement_name, **score.model_dump()), "entitlement_name")
        records[idx] = record
        write_all(RISK_SCORES_FILE, records)
    return record


@app.patch("/risk-scores/{entitlement_name}", response_model=RiskScore, tags=["risk-scores"])
def update_risk_score(entitlement_name: str, patch: RiskScoreUpdate):
    """Partial update: only the fields present in the body are changed."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Request body contains no fields to update")

    with _lock:
        records = read_all(RISK_SCORES_FILE)
        idx = find_index(records, "entitlement_name", entitlement_name)
        record = serialize(RiskScore(**{**records[idx], **changes}), "entitlement_name")
        records[idx] = record
        write_all(RISK_SCORES_FILE, records)
    return record


@app.delete(
    "/risk-scores/{entitlement_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["risk-scores"],
)
def delete_risk_score(entitlement_name: str):
    with _lock:
        records = read_all(RISK_SCORES_FILE)
        records.pop(find_index(records, "entitlement_name", entitlement_name))
        write_all(RISK_SCORES_FILE, records)


@app.get("/health", tags=["meta"])
def health():
    return {
        "status": "ok",
        "data_files": {
            "catalog": {"path": str(CATALOG_FILE), "exists": CATALOG_FILE.exists()},
            "risk_scores": {"path": str(RISK_SCORES_FILE), "exists": RISK_SCORES_FILE.exists()},
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
