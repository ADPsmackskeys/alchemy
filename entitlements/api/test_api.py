"""Smoke test for the CRUD endpoints.

Runs against throwaway copies of the data files, so the real
entitlement_catalog.json and entitlement_risk_scores.json are never modified.

    python test_api.py
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
CATALOG_SOURCE = HERE / "entitlement_catalog.json"
SCORES_SOURCE = HERE / "entitlement_risk_scores.json"

_tmpdir = tempfile.mkdtemp()
_catalog = Path(_tmpdir) / "entitlement_catalog.json"
_scores = Path(_tmpdir) / "entitlement_risk_scores.json"
shutil.copy(CATALOG_SOURCE, _catalog)
shutil.copy(SCORES_SOURCE, _scores)
os.environ["ENTITLEMENT_CATALOG_FILE"] = str(_catalog)
os.environ["ENTITLEMENT_RISK_SCORES_FILE"] = str(_scores)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

NEW_ENT = {
    "entitlement_id": "ENT999",
    "entitlement_name": "ZZ_TEST_ENTITLEMENT",
    "application": "SharePoint",
    "owner": "Audit IT",
}

NEW_SCORE = {
    "entitlement_name": "ZZ_TEST_ENTITLEMENT",
    "application": "SharePoint",
    "risk_score": 20,
    "risk_category": "Low",
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


# ---------------------------------------------------------------- catalog
r = client.get("/entitlements")
check("list returns 18 seed entitlements", r.status_code == 200 and len(r.json()) == 18, len(r.json()))

r = client.get("/entitlements", params={"application": "sap ecc"})
check("filter by application (case-insensitive)", len(r.json()) == 4, len(r.json()))

r = client.get("/entitlements", params={"limit": 2, "offset": 8})
check("pagination", [x["entitlement_id"] for x in r.json()] == ["ENT009", "ENT010"], r.json())

r = client.get("/entitlements/ENT003")
check("get by id", r.status_code == 200 and r.json()["entitlement_name"] == "POWERBI_FINANCE")

r = client.get("/entitlements/NOPE")
check("get missing id -> 404", r.status_code == 404)

r = client.post("/entitlements", json=NEW_ENT)
check("create -> 201", r.status_code == 201, r.text)

r = client.post("/entitlements", json=NEW_ENT)
check("duplicate create -> 409", r.status_code == 409)

r = client.post("/entitlements", json={**NEW_ENT, "entitlement_id": "ENT998", "owner": ""})
check("empty owner -> 422", r.status_code == 422)

r = client.patch("/entitlements/ENT999", json={"owner": "Collaboration Team"})
check(
    "patch changes one field only",
    r.status_code == 200
    and r.json()["owner"] == "Collaboration Team"
    and r.json()["entitlement_name"] == "ZZ_TEST_ENTITLEMENT",
    r.text,
)

r = client.patch("/entitlements/ENT999", json={})
check("empty patch -> 400", r.status_code == 400)

body = {k: v for k, v in NEW_ENT.items() if k != "entitlement_id"}
r = client.put("/entitlements/ENT999", json={**body, "application": "SharePoint Online"})
check(
    "put replaces record",
    r.status_code == 200
    and r.json()["application"] == "SharePoint Online"
    and r.json()["owner"] == "Audit IT",
    r.text,
)

r = client.put("/entitlements/NOPE", json=body)
check("put missing id -> 404", r.status_code == 404)

on_disk = json.loads(_catalog.read_text())
check("write persisted to disk", any(x["entitlement_id"] == "ENT999" for x in on_disk))
check("key order preserved", list(on_disk[-1]) == list(NEW_ENT))

r = client.delete("/entitlements/ENT999")
check("delete -> 204", r.status_code == 204)

r = client.delete("/entitlements/ENT999")
check("delete again -> 404", r.status_code == 404)

check("catalog back to 18 records", len(json.loads(_catalog.read_text())) == 18)

# ------------------------------------------------------------ risk scores
r = client.get("/risk-scores")
check("list returns 18 seed scores", r.status_code == 200 and len(r.json()) == 18, len(r.json()))

r = client.get("/risk-scores", params={"risk_category": "Critical"})
check("filter by risk_category", len(r.json()) == 3, len(r.json()))

r = client.get("/risk-scores", params={"min_score": 70, "max_score": 95})
check("score range filter", sorted(x["risk_score"] for x in r.json()) == [70, 75, 90, 95], r.json())

r = client.get("/risk-scores", params={"risk_category": "Nonsense"})
check("bad enum value -> 422", r.status_code == 422)

r = client.get("/risk-scores/AD_DOMAIN_ADMIN")
check("get by entitlement_name", r.status_code == 200 and r.json()["risk_score"] == 100)

r = client.get("/risk-scores/NOPE")
check("get missing entitlement_name -> 404", r.status_code == 404)

r = client.post("/risk-scores", json=NEW_SCORE)
check("create -> 201", r.status_code == 201, r.text)

r = client.post("/risk-scores", json=NEW_SCORE)
check("duplicate create -> 409", r.status_code == 409)

r = client.post("/risk-scores", json={**NEW_SCORE, "entitlement_name": "OTHER", "risk_score": 101})
check("risk_score > 100 -> 422", r.status_code == 422)

r = client.patch("/risk-scores/ZZ_TEST_ENTITLEMENT", json={"risk_score": 55, "risk_category": "Medium"})
check(
    "patch updates score",
    r.status_code == 200 and r.json()["risk_score"] == 55 and r.json()["application"] == "SharePoint",
    r.text,
)

score_body = {k: v for k, v in NEW_SCORE.items() if k != "entitlement_name"}
r = client.put("/risk-scores/ZZ_TEST_ENTITLEMENT", json=score_body)
check("put replaces record", r.status_code == 200 and r.json()["risk_score"] == 20, r.text)

on_disk = json.loads(_scores.read_text())
check("write persisted to disk", any(x["entitlement_name"] == "ZZ_TEST_ENTITLEMENT" for x in on_disk))
check("key order preserved", list(on_disk[-1]) == list(NEW_SCORE))

r = client.delete("/risk-scores/ZZ_TEST_ENTITLEMENT")
check("delete -> 204", r.status_code == 204)

r = client.delete("/risk-scores/ZZ_TEST_ENTITLEMENT")
check("delete again -> 404", r.status_code == 404)

check("scores back to 18 records", len(json.loads(_scores.read_text())) == 18)

# ------------------------------------------------------- batched lookups
r = client.get("/entitlements?entitlement_name=JIRA_USER")
check("single filter value still works", [x["entitlement_name"] for x in r.json()] == ["JIRA_USER"], r.json())

r = client.get("/entitlements?entitlement_name=JIRA_USER&entitlement_name=GITHUB_DEV")
check(
    "repeated filter ORs its values",
    sorted(x["entitlement_name"] for x in r.json()) == ["GITHUB_DEV", "JIRA_USER"],
    r.json(),
)

r = client.get("/risk-scores?entitlement_name=JIRA_USER&entitlement_name=GITHUB_DEV")
check("risk scores batch by name", len(r.json()) == 2, r.json())

r = client.get("/entitlements?application=SAP%20ECC&owner=Finance%20IT")
check("different filters AND together", all(x["application"] == "SAP ECC" for x in r.json()), r.json())

r = client.get("/entitlements")
check("X-Total-Count reports the unpaginated total", r.headers["X-Total-Count"] == "18", dict(r.headers))

r = client.get("/entitlements?limit=2")
check(
    "truncation is visible in the headers",
    r.headers["X-Returned-Count"] == "2" and r.headers["X-Total-Count"] == "18",
    dict(r.headers),
)

r = client.get("/entitlements?entitlement_name=NO_SUCH_ENTITLEMENT")
check("unknown name matches nothing", r.json() == [] and r.headers["X-Total-Count"] == "0", r.json())

# ------------------------------------------------------------------ misc
check("originals untouched", len(json.loads(CATALOG_SOURCE.read_text())) == 18)
check("originals untouched", len(json.loads(SCORES_SOURCE.read_text())) == 18)
check("health ok", client.get("/health").json()["status"] == "ok")

shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
