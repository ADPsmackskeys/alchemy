"""Smoke test for the CRUD endpoints.

Runs against a throwaway copy of the data file, so the real
identities.json is never modified.

    python test_api.py
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

SOURCE = Path(__file__).parent / "identities.json"
_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "identities.json"
shutil.copy(SOURCE, _copy)
os.environ["IDENTITIES_FILE"] = str(_copy)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

NEW = {
    "employee_id": "EMP999",
    "name": "Test Person",
    "department": "Technology",
    "job_role": "QA Engineer",
    "job_level": "L2",
    "location": "Chennai",
    "entitlements": "JIRA_USER;CONFLUENCE_USER",
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


# READ
r = client.get("/identities")
check("list returns 10 seed records", r.status_code == 200 and len(r.json()) == 10, len(r.json()))

r = client.get("/identities", params={"department": "finance"})
check("filter by department (case-insensitive)", len(r.json()) == 5, len(r.json()))

r = client.get("/identities", params={"entitlement": "github_dev"})
check("filter by entitlement", len(r.json()) == 3, len(r.json()))

r = client.get("/identities", params={"entitlement": "SAP_FIN"})
check("entitlement filter is exact, not substring", len(r.json()) == 0, len(r.json()))

r = client.get("/identities", params={"limit": 2, "offset": 8})
check("pagination", [x["employee_id"] for x in r.json()] == ["EMP009", "EMP010"], r.json())

r = client.get("/identities/EMP009")
check("get by id", r.status_code == 200 and r.json()["name"] == "Meera")

r = client.get("/identities/EMP009/entitlements")
check(
    "parsed entitlements",
    r.json() == ["RSA_GRC", "POWERBI_RISK", "RISK_PORTAL"],
    r.json(),
)

r = client.get("/identities/NOPE")
check("get missing id -> 404", r.status_code == 404)

r = client.get("/identities/NOPE/entitlements")
check("parsed entitlements for missing id -> 404", r.status_code == 404)

# CREATE
r = client.post("/identities", json=NEW)
check("create -> 201", r.status_code == 201, r.text)

r = client.post("/identities", json=NEW)
check("duplicate create -> 409", r.status_code == 409)

r = client.post("/identities", json={**NEW, "employee_id": "EMP998", "name": ""})
check("empty name -> 422", r.status_code == 422)

r = client.post(
    "/identities",
    json={**NEW, "employee_id": "EMP997", "entitlements": " JIRA_USER ;; GITHUB_DEV ;"},
)
check("entitlements normalized on write", r.json()["entitlements"] == "JIRA_USER;GITHUB_DEV", r.text)

# UPDATE
r = client.patch("/identities/EMP999", json={"location": "Kolkata"})
check(
    "patch changes one field only",
    r.status_code == 200 and r.json()["location"] == "Kolkata" and r.json()["name"] == "Test Person",
    r.text,
)

r = client.patch("/identities/EMP999", json={})
check("empty patch -> 400", r.status_code == 400)

r = client.patch("/identities/EMP999", json={"entitlements": "JIRA_USER; GITHUB_DEV"})
check("patch normalizes entitlements", r.json()["entitlements"] == "JIRA_USER;GITHUB_DEV", r.text)

body = {k: v for k, v in NEW.items() if k != "employee_id"}
r = client.put("/identities/EMP999", json={**body, "job_level": "L4"})
check(
    "put replaces record",
    r.status_code == 200 and r.json()["job_level"] == "L4" and r.json()["location"] == "Chennai",
    r.text,
)

r = client.put("/identities/NOPE", json=body)
check("put missing id -> 404", r.status_code == 404)

# persistence
on_disk = json.loads(_copy.read_text())
check("write persisted to disk", any(x["employee_id"] == "EMP999" for x in on_disk))
check("key order preserved", list(on_disk[-1]) == list(NEW))

# DELETE
r = client.delete("/identities/EMP999")
check("delete -> 204", r.status_code == 204)

r = client.delete("/identities/EMP997")
check("delete second added record -> 204", r.status_code == 204)

r = client.delete("/identities/EMP999")
check("delete again -> 404", r.status_code == 404)

check("file back to 10 records", len(json.loads(_copy.read_text())) == 10)
check("original file untouched", len(json.loads(SOURCE.read_text())) == 10)
check("health ok", client.get("/health").json()["status"] == "ok")

shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
