"""FastAPI CRUD backend over sod_rules.json.

    uvicorn main:app --reload --port 8000
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config import settings

DATA_FILE = settings.sod_rules_file

# The JSON file is the single source of truth; the lock keeps concurrent
# requests from interleaving a read-modify-write cycle.
_lock = threading.Lock()

Severity = Literal["Low", "Medium", "High", "Critical"]
SEVERITY_ORDER = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}


class SodRuleBase(BaseModel):
    entitlement_1: str = Field(min_length=1)
    entitlement_2: str = Field(min_length=1)
    severity: Severity

    @model_validator(mode="after")
    def _check_distinct(self):
        # A rule pairing an entitlement with itself can never be a
        # separation-of-duties conflict.
        if self.entitlement_1.strip().lower() == self.entitlement_2.strip().lower():
            raise ValueError("entitlement_1 and entitlement_2 must differ")
        return self


class SodRule(SodRuleBase):
    sod_id: str = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sod_id": "SOD004",
                "entitlement_1": "SAP_PAYMENT_APPROVER",
                "entitlement_2": "AD_DOMAIN_ADMIN",
                "severity": "Critical",
            }
        }
    )


class SodRuleUpdate(BaseModel):
    """Every field optional, for PATCH."""

    entitlement_1: str | None = Field(default=None, min_length=1)
    entitlement_2: str | None = Field(default=None, min_length=1)
    severity: Severity | None = None


class SodCheckRequest(BaseModel):
    entitlements: list[str] = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"entitlements": ["JIRA_USER", "SAP_VENDOR_CREATE", "GITHUB_DEV"]}
        }
    )


class SodConflict(BaseModel):
    """A rule with BOTH sides inside the checked set: a real conflict."""

    sod_id: str
    entitlement_1: str
    entitlement_2: str
    severity: Severity


class SodAdjacent(BaseModel):
    """A rule with one side in the set and its counterpart outside it.

    Not a conflict for this set. Kept separate precisely because the union of
    per-entitlement queries mixes these in with real conflicts, leaving the
    caller to do the pair intersection by hand.
    """

    sod_id: str
    in_set: str
    conflicts_with: str
    severity: Severity


class SodCheckResult(BaseModel):
    entitlements: list[str]
    conflicts: list[SodConflict]
    adjacent: list[SodAdjacent]
    clear: bool
    highest_severity: Severity | None


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


def find_index(records: list[dict], sod_id: str) -> int:
    for i, r in enumerate(records):
        if r.get("sod_id") == sod_id:
            return i
    raise HTTPException(status_code=404, detail=f"No SoD rule with sod_id {sod_id!r}")


def pair_key(entitlement_1: str, entitlement_2: str) -> frozenset[str]:
    # SoD conflicts are symmetric, so A/B and B/A are the same rule.
    return frozenset({entitlement_1.strip().lower(), entitlement_2.strip().lower()})


def check_pair_unique(records: list[dict], rule: SodRule, skip_index: int | None = None) -> None:
    key = pair_key(rule.entitlement_1, rule.entitlement_2)
    for i, r in enumerate(records):
        if i == skip_index:
            continue
        if pair_key(str(r.get("entitlement_1", "")), str(r.get("entitlement_2", ""))) == key:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Rule {r.get('sod_id')!r} already covers the pair "
                    f"{rule.entitlement_1!r} / {rule.entitlement_2!r}"
                ),
            )


def apply_filters(records: list[dict], filters: dict[str, str | list[str] | None]) -> list[dict]:
    """Case-insensitive whole-value match: OR within a field, AND across fields.

    A filter is either one value or a list of them, so a caller asking about
    five entitlements makes one request instead of five. An explicitly empty
    list means "filter not supplied" and matches everything.
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


def serialize(rule: SodRule) -> dict:
    # The reorder keeps sod_id first, matching the existing rows in the file.
    record = json.loads(rule.model_dump_json())
    return {"sod_id": record.pop("sod_id"), **record}


app = FastAPI(
    title="SoD Rules API",
    description="CRUD operations backed by sod_rules.json",
    version="1.0.0",
    root_path=settings.root_path,
)


@app.get("/sod-rules", response_model=list[SodRule], tags=["sod-rules"])
def list_sod_rules(
    response: Response,
    severity: Annotated[list[Severity] | None, Query(description="Repeatable")] = None,
    entitlement: Annotated[
        list[str] | None,
        Query(description="Rules where any of these is on either side; repeatable"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List SoD rules, with optional filters and pagination.

    Filters are repeatable and OR their own values together. Note that
    `entitlement` returns rules touching any of the given names, including
    ones whose counterpart is NOT in the set -- for "do any two of these
    conflict with each other", use POST /sod-rules/check instead.
    """
    with _lock:
        records = read_all()

    records = apply_filters(records, {"severity": severity})

    if entitlement:
        wanted = {e.lower() for e in entitlement}
        records = [
            r
            for r in records
            if wanted
            & {str(r.get("entitlement_1", "")).lower(), str(r.get("entitlement_2", "")).lower()}
        ]

    return paginate(records, limit, offset, response)


@app.post("/sod-rules/check", response_model=SodCheckResult, tags=["sod-rules"])
def check_sod_rules(payload: SodCheckRequest):
    """Check one SET of entitlements for separation-of-duties conflicts.

    This answers the question a caller actually has -- "may this person hold
    all of these at once?" -- in a single request. Unioning per-entitlement
    lookups cannot answer it: that returns rules whose other side is outside
    the set, and leaves the caller to intersect the pairs itself.

    Read-only despite being a POST; the set goes in the body because it can be
    long.
    """
    wanted = {e.strip().lower() for e in payload.entitlements if e.strip()}

    with _lock:
        records = read_all()

    conflicts: list[dict] = []
    adjacent: list[dict] = []
    for r in records:
        e1, e2 = str(r.get("entitlement_1", "")), str(r.get("entitlement_2", ""))
        in1, in2 = e1.strip().lower() in wanted, e2.strip().lower() in wanted
        if in1 and in2:
            conflicts.append(
                {
                    "sod_id": r.get("sod_id"),
                    "entitlement_1": e1,
                    "entitlement_2": e2,
                    "severity": r.get("severity"),
                }
            )
        elif in1 or in2:
            adjacent.append(
                {
                    "sod_id": r.get("sod_id"),
                    "in_set": e1 if in1 else e2,
                    "conflicts_with": e2 if in1 else e1,
                    "severity": r.get("severity"),
                }
            )

    highest = None
    if conflicts:
        highest = max((c["severity"] for c in conflicts), key=lambda s: SEVERITY_ORDER.get(s, -1))

    return {
        "entitlements": payload.entitlements,
        "conflicts": conflicts,
        "adjacent": adjacent,
        "clear": not conflicts,
        "highest_severity": highest,
    }


@app.get("/sod-rules/{sod_id}", response_model=SodRule, tags=["sod-rules"])
def get_sod_rule(sod_id: str):
    with _lock:
        records = read_all()
        return records[find_index(records, sod_id)]


@app.post(
    "/sod-rules",
    response_model=SodRule,
    status_code=status.HTTP_201_CREATED,
    tags=["sod-rules"],
)
def create_sod_rule(rule: SodRule):
    with _lock:
        records = read_all()
        if any(r.get("sod_id") == rule.sod_id for r in records):
            raise HTTPException(status_code=409, detail=f"sod_id {rule.sod_id!r} already exists")
        check_pair_unique(records, rule)
        record = serialize(rule)
        records.append(record)
        write_all(records)
    return record


@app.put("/sod-rules/{sod_id}", response_model=SodRule, tags=["sod-rules"])
def replace_sod_rule(sod_id: str, rule: Annotated[SodRuleBase, Body()]):
    """Full replace. The sod_id comes from the path and cannot be changed."""
    with _lock:
        records = read_all()
        idx = find_index(records, sod_id)
        full = SodRule(sod_id=sod_id, **rule.model_dump())
        check_pair_unique(records, full, skip_index=idx)
        record = serialize(full)
        records[idx] = record
        write_all(records)
    return record


@app.patch("/sod-rules/{sod_id}", response_model=SodRule, tags=["sod-rules"])
def update_sod_rule(sod_id: str, patch: SodRuleUpdate):
    """Partial update: only the fields present in the body are changed."""
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Request body contains no fields to update")

    with _lock:
        records = read_all()
        idx = find_index(records, sod_id)
        full = SodRule(**{**records[idx], **changes})
        check_pair_unique(records, full, skip_index=idx)
        record = serialize(full)
        records[idx] = record
        write_all(records)
    return record


@app.delete(
    "/sod-rules/{sod_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["sod-rules"],
)
def delete_sod_rule(sod_id: str):
    with _lock:
        records = read_all()
        records.pop(find_index(records, sod_id))
        write_all(records)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "data_file": str(DATA_FILE), "exists": DATA_FILE.exists()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
