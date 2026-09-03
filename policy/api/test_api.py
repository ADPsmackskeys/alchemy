"""Smoke test for the CRUD endpoints.

Runs against a throwaway copy of the data file, so the real
policy_rules.json is never modified.

    python test_api.py
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

SOURCE = Path(__file__).parent / "policy_rules.json"
_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "policy_rules.json"
shutil.copy(SOURCE, _copy)
os.environ["POLICY_RULES_FILE"] = str(_copy)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

NEW = {
    "policy_id": "POL999",
    "policy_name": "SoD Block",
    "type": "DENY",
    "rule": "SAP_VENDOR_CREATE + SAP_PAYMENT_APPROVER",
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


# READ
r = client.get("/policies")
check("list returns 14 seed policies", r.status_code == 200 and len(r.json()) == 14, len(r.json()))

r = client.get("/policies", params={"type": "ALLOW"})
check("filter by type", len(r.json()) == 12, len(r.json()))

r = client.get("/policies", params={"policy_name": "finance birthright"})
check("filter by name (case-insensitive)", len(r.json()) == 2, len(r.json()))

r = client.get("/policies", params={"rule_contains": "risk_score"})
check("rule substring filter", len(r.json()) == 2, len(r.json()))

r = client.get("/policies", params={"type": "MAYBE"})
check("bad enum value -> 422", r.status_code == 422)

r = client.get("/policies", params={"limit": 2, "offset": 5})
check("pagination", [x["policy_id"] for x in r.json()] == ["POL006", "POL007"], r.json())

r = client.get("/policies/POL005")
check("get by id", r.status_code == 200 and r.json()["type"] == "HUMAN_APPROVAL", r.text)

r = client.get("/policies/NOPE")
check("get missing id -> 404", r.status_code == 404)

# CREATE
r = client.post("/policies", json=NEW)
check("create -> 201", r.status_code == 201, r.text)

r = client.post("/policies", json=NEW)
check("duplicate create -> 409", r.status_code == 409)

r = client.post("/policies", json={**NEW, "policy_id": "POL998", "type": "PERHAPS"})
check("invalid type -> 422", r.status_code == 422)

r = client.post("/policies", json={**NEW, "policy_id": "POL997", "rule": ""})
check("empty rule -> 422", r.status_code == 422)

# UPDATE
r = client.patch("/policies/POL999", json={"type": "HUMAN_APPROVAL"})
check(
    "patch changes one field only",
    r.status_code == 200
    and r.json()["type"] == "HUMAN_APPROVAL"
    and r.json()["policy_name"] == "SoD Block",
    r.text,
)

r = client.patch("/policies/POL999", json={})
check("empty patch -> 400", r.status_code == 400)

body = {k: v for k, v in NEW.items() if k != "policy_id"}
r = client.put("/policies/POL999", json={**body, "policy_name": "SoD Hard Block"})
check(
    "put replaces record",
    r.status_code == 200 and r.json()["policy_name"] == "SoD Hard Block" and r.json()["type"] == "DENY",
    r.text,
)

r = client.put("/policies/NOPE", json=body)
check("put missing id -> 404", r.status_code == 404)

# persistence
on_disk = json.loads(_copy.read_text())
check("write persisted to disk", any(x["policy_id"] == "POL999" for x in on_disk))
check("key order preserved", list(on_disk[-1]) == list(NEW))

# DELETE
r = client.delete("/policies/POL999")
check("delete -> 204", r.status_code == 204)

r = client.delete("/policies/POL999")
check("delete again -> 404", r.status_code == 404)

# ------------------------------------------------------- batched lookups
r = client.get("/policies?rule_contains=risk_score")
check("single needle still works", len(r.json()) == 2, r.json())

r = client.get("/policies?rule_contains=JIRA_USER&rule_contains=risk_score")
check(
    "repeated rule_contains ORs its needles",
    len(r.json()) >= 2 and any("risk_score" in x["rule"].lower() for x in r.json()),
    [x["policy_id"] for x in r.json()],
)

r = client.get("/policies?type=ALLOW&type=HUMAN_APPROVAL")
check("repeated type ORs its values", len(r.json()) == 14, len(r.json()))

r = client.get("/policies")
check("X-Total-Count reports the unpaginated total", r.headers["X-Total-Count"] == "14", dict(r.headers))

check("file back to 14 records", len(json.loads(_copy.read_text())) == 14)
check("original file untouched", len(json.loads(SOURCE.read_text())) == 14)
check("health ok", client.get("/health").json()["status"] == "ok")

shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
