"""Smoke test for the CRUD endpoints.

Runs against a throwaway copy of the data file, so the real
new_joiners.json is never modified.

    python test_api.py
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

SOURCE = Path(__file__).parent / "new_joiners.json"
_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "new_joiners.json"
shutil.copy(SOURCE, _copy)
os.environ["NEW_JOINERS_FILE"] = str(_copy)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)

NEW = {
    "employee_id": "NJ9999",
    "name": "Test Person",
    "department": "Technology",
    "job_role": "QA Engineer",
    "job_level": "L2",
    "location": "Chennai",
    "manager_id": "MGR200",
    "cost_center": "TECH001",
    "start_date": "2026-09-15",
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


# READ
r = client.get("/new-joiners")
check("list returns 10 seed records", r.status_code == 200 and len(r.json()) == 10, len(r.json()))

r = client.get("/new-joiners", params={"department": "finance"})
check("filter by department (case-insensitive)", len(r.json()) == 4, len(r.json()))

r = client.get("/new-joiners", params={"limit": 2, "offset": 8})
check("pagination", [x["employee_id"] for x in r.json()] == ["NJ1009", "NJ1010"], r.json())

r = client.get("/new-joiners/NJ1004")
check("get by id", r.status_code == 200 and r.json()["name"] == "Anjali Rao")

r = client.get("/new-joiners/NOPE")
check("get missing id -> 404", r.status_code == 404)

# CREATE
r = client.post("/new-joiners", json=NEW)
check("create -> 201", r.status_code == 201, r.text)

r = client.post("/new-joiners", json=NEW)
check("duplicate create -> 409", r.status_code == 409)

r = client.post("/new-joiners", json={**NEW, "employee_id": "NJ8888", "start_date": "not-a-date"})
check("invalid date -> 422", r.status_code == 422)

# UPDATE
r = client.patch("/new-joiners/NJ9999", json={"location": "Kolkata"})
check(
    "patch changes one field only",
    r.status_code == 200 and r.json()["location"] == "Kolkata" and r.json()["name"] == "Test Person",
    r.text,
)

r = client.patch("/new-joiners/NJ9999", json={})
check("empty patch -> 400", r.status_code == 400)

body = {k: v for k, v in NEW.items() if k != "employee_id"}
r = client.put("/new-joiners/NJ9999", json={**body, "job_level": "L4"})
check(
    "put replaces record",
    r.status_code == 200 and r.json()["job_level"] == "L4" and r.json()["location"] == "Chennai",
    r.text,
)

r = client.put("/new-joiners/NOPE", json=body)
check("put missing id -> 404", r.status_code == 404)

# persistence
on_disk = json.loads(_copy.read_text())
check("write persisted to disk", any(x["employee_id"] == "NJ9999" for x in on_disk))

# DELETE
r = client.delete("/new-joiners/NJ9999")
check("delete -> 204", r.status_code == 204)

r = client.delete("/new-joiners/NJ9999")
check("delete again -> 404", r.status_code == 404)

check("file back to 10 records", len(json.loads(_copy.read_text())) == 10)
check("original file untouched", len(json.loads(SOURCE.read_text())) == 10)

shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
