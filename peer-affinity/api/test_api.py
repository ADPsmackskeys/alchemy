"""Smoke test for the CRUD endpoints.

Runs against a throwaway copy of the data file, so the real
peer_affinity_scores.json is never modified.

    python test_api.py
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

SOURCE = Path(__file__).parent / "peer_affinity_scores.json"
_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "peer_affinity_scores.json"
shutil.copy(SOURCE, _copy)
os.environ["PEER_AFFINITY_FILE"] = str(_copy)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

NEW = {
    "job_role": "QA Engineer",
    "department": "Technology",
    "entitlement": "JIRA_USER",
    "peer_count": 3,
    "total_peers": 4,
    "affinity_score": 75,
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


# READ
r = client.get("/peer-affinity")
check("list returns 13 seed rows", r.status_code == 200 and len(r.json()) == 13, len(r.json()))

r = client.get("/peer-affinity", params={"job_role": "financial analyst"})
check("filter by job_role (case-insensitive)", len(r.json()) == 4, len(r.json()))

r = client.get("/peer-affinity", params={"entitlement": "POWERBI_FINANCE"})
check("filter by entitlement", len(r.json()) == 1, len(r.json()))

r = client.get("/peer-affinity", params={"min_score": 100})
check("min_score filter", len(r.json()) == 10, len(r.json()))

r = client.get("/peer-affinity", params={"max_score": 50})
check("max_score filter", [x["affinity_score"] for x in r.json()] == [20], r.json())

r = client.get("/peer-affinity", params={"limit": 2, "offset": 11})
check("pagination", [x["entitlement"] for x in r.json()] == ["POWERBI_AUDIT", "SHAREPOINT_AUDIT"], r.json())

r = client.get("/peer-affinity/Financial Analyst/SAP_AP_INVOICE")
check("get by composite key", r.status_code == 200 and r.json()["affinity_score"] == 80, r.text)

r = client.get("/peer-affinity/Financial Analyst/NOPE")
check("get missing entitlement -> 404", r.status_code == 404)

r = client.get("/peer-affinity/Nobody/SAP_AP_INVOICE")
check("get missing job_role -> 404", r.status_code == 404)

# CREATE
r = client.post("/peer-affinity", json=NEW)
check("create -> 201", r.status_code == 201, r.text)

r = client.post("/peer-affinity", json=NEW)
check("duplicate composite key -> 409", r.status_code == 409)

r = client.post("/peer-affinity", json={**NEW, "entitlement": "GITHUB_DEV", "peer_count": 9})
check("peer_count > total_peers -> 422", r.status_code == 422)

r = client.post(
    "/peer-affinity",
    json={k: v for k, v in NEW.items() if k != "affinity_score"} | {"entitlement": "GITHUB_DEV"},
)
check("affinity_score computed when omitted", r.json()["affinity_score"] == 75, r.text)

# UPDATE
r = client.patch("/peer-affinity/QA Engineer/JIRA_USER", json={"peer_count": 2})
check(
    "patch recomputes affinity_score",
    r.status_code == 200 and r.json()["affinity_score"] == 50 and r.json()["total_peers"] == 4,
    r.text,
)

r = client.patch("/peer-affinity/QA Engineer/JIRA_USER", json={"peer_count": 1, "affinity_score": 99})
check("explicit affinity_score wins over recompute", r.json()["affinity_score"] == 99, r.text)

r = client.patch("/peer-affinity/QA Engineer/JIRA_USER", json={})
check("empty patch -> 400", r.status_code == 400)

body = {k: v for k, v in NEW.items() if k not in ("job_role", "entitlement")}
r = client.put("/peer-affinity/QA Engineer/JIRA_USER", json={**body, "total_peers": 6})
check(
    "put replaces record",
    r.status_code == 200 and r.json()["total_peers"] == 6 and r.json()["peer_count"] == 3,
    r.text,
)

r = client.put("/peer-affinity/Nobody/JIRA_USER", json=body)
check("put missing key -> 404", r.status_code == 404)

# persistence
on_disk = json.loads(_copy.read_text())
check(
    "write persisted to disk",
    any(x["job_role"] == "QA Engineer" and x["entitlement"] == "JIRA_USER" for x in on_disk),
)
check("key order preserved", list(on_disk[-1]) == list(NEW))

# DELETE
r = client.delete("/peer-affinity/QA Engineer/JIRA_USER")
check("delete -> 204", r.status_code == 204)

r = client.delete("/peer-affinity/QA Engineer/GITHUB_DEV")
check("delete second added row -> 204", r.status_code == 204)

r = client.delete("/peer-affinity/QA Engineer/JIRA_USER")
check("delete again -> 404", r.status_code == 404)

check("file back to 13 rows", len(json.loads(_copy.read_text())) == 13)
check("original file untouched", len(json.loads(SOURCE.read_text())) == 13)
check("health ok", client.get("/health").json()["status"] == "ok")

shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
