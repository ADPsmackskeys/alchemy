"""Smoke test for the CRUD endpoints.

Runs against a throwaway copy of the data file, so the real
sod_rules.json is never modified.

    python test_api.py
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

SOURCE = Path(__file__).parent / "sod_rules.json"
_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "sod_rules.json"
shutil.copy(SOURCE, _copy)
os.environ["SOD_RULES_FILE"] = str(_copy)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

NEW = {
    "sod_id": "SOD999",
    "entitlement_1": "SAP_PAYMENT_APPROVER",
    "entitlement_2": "AD_DOMAIN_ADMIN",
    "severity": "Critical",
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


# READ
r = client.get("/sod-rules")
check("list returns 3 seed rules", r.status_code == 200 and len(r.json()) == 3, len(r.json()))

r = client.get("/sod-rules", params={"severity": "High"})
check("filter by severity", len(r.json()) == 2, len(r.json()))

r = client.get("/sod-rules", params={"entitlement": "sap_vendor_create"})
check("entitlement matches either side", len(r.json()) == 2, len(r.json()))

r = client.get("/sod-rules", params={"severity": "Catastrophic"})
check("bad enum value -> 422", r.status_code == 422)

r = client.get("/sod-rules", params={"limit": 1, "offset": 2})
check("pagination", [x["sod_id"] for x in r.json()] == ["SOD003"], r.json())

r = client.get("/sod-rules/SOD001")
check("get by id", r.status_code == 200 and r.json()["severity"] == "Critical", r.text)

r = client.get("/sod-rules/NOPE")
check("get missing id -> 404", r.status_code == 404)

# CREATE
r = client.post("/sod-rules", json=NEW)
check("create -> 201", r.status_code == 201, r.text)

r = client.post("/sod-rules", json=NEW)
check("duplicate sod_id -> 409", r.status_code == 409)

r = client.post(
    "/sod-rules",
    json={
        "sod_id": "SOD998",
        "entitlement_1": "SAP_PAYMENT_APPROVER",
        "entitlement_2": "SAP_VENDOR_CREATE",
        "severity": "High",
    },
)
check("reversed pair of an existing rule -> 409", r.status_code == 409, r.text)

r = client.post(
    "/sod-rules",
    json={**NEW, "sod_id": "SOD997", "entitlement_2": "SAP_PAYMENT_APPROVER"},
)
check("self-conflicting rule -> 422", r.status_code == 422)

r = client.post("/sod-rules", json={**NEW, "sod_id": "SOD996", "severity": "Severe"})
check("invalid severity -> 422", r.status_code == 422)

# UPDATE
r = client.patch("/sod-rules/SOD999", json={"severity": "High"})
check(
    "patch changes one field only",
    r.status_code == 200
    and r.json()["severity"] == "High"
    and r.json()["entitlement_1"] == "SAP_PAYMENT_APPROVER",
    r.text,
)

r = client.patch("/sod-rules/SOD999", json={})
check("empty patch -> 400", r.status_code == 400)

r = client.patch("/sod-rules/SOD999", json={"entitlement_2": "SAP_VENDOR_CREATE"})
check("patch into an existing pair -> 409", r.status_code == 409, r.text)

body = {k: v for k, v in NEW.items() if k != "sod_id"}
r = client.put("/sod-rules/SOD999", json={**body, "severity": "Medium"})
check(
    "put replaces record",
    r.status_code == 200 and r.json()["severity"] == "Medium",
    r.text,
)

r = client.put("/sod-rules/SOD999", json=body)
check("put keeping its own pair is allowed", r.status_code == 200, r.text)

r = client.put("/sod-rules/NOPE", json=body)
check("put missing id -> 404", r.status_code == 404)

# persistence
on_disk = json.loads(_copy.read_text())
check("write persisted to disk", any(x["sod_id"] == "SOD999" for x in on_disk))
check("key order preserved", list(on_disk[-1]) == list(NEW))

# DELETE
r = client.delete("/sod-rules/SOD999")
check("delete -> 204", r.status_code == 204)

r = client.delete("/sod-rules/SOD999")
check("delete again -> 404", r.status_code == 404)

check("file back to 3 records", len(json.loads(_copy.read_text())) == 3)
check("original file untouched", len(json.loads(SOURCE.read_text())) == 3)
check("health ok", client.get("/health").json()["status"] == "ok")

shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
