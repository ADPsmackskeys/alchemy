"""Smoke test for the MCP server.

Boots the real FastAPI app on a scratch port against a throwaway copy of
the data file, then drives mcp_server through an in-memory FastMCP client
-- so this exercises the actual tool schemas and dispatch, not just the
Python functions.

Requires FastMCP 3.x (see requirements-mcp.txt).

    python test_mcp_server.py
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
SOURCE = HERE / "new_joiners.json"

_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "new_joiners.json"
shutil.copy(SOURCE, _copy)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]

BASE_URL = f"http://127.0.0.1:{PORT}"
os.environ["NEW_JOINERS_API_URL"] = BASE_URL  # read at import time by mcp_server

from fastmcp import Client  # noqa: E402

import server  # noqa: E402

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


def start_api():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=HERE,
        env={**os.environ, "NEW_JOINERS_FILE": str(_copy)},
    )
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError("uvicorn exited during startup")
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=0.5).status_code == 200:
                return proc
        except httpx.RequestError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("API did not come up")


def error_text(result):
    return result.content[0].text if result.content else ""


async def call(client, name, args):
    """FastMCP 3.x raises on tool errors by default; we want to inspect them."""
    return await client.call_tool(name, args, raise_on_error=False)


async def main():
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        check(
            "all CRUD tools exposed",
            set(tools)
            == {
                "list_new_joiners",
                "get_new_joiner",
                "create_new_joiner",
                "update_new_joiner",
                "replace_new_joiner",
                "delete_new_joiner",
                "api_health",
            },
            sorted(tools),
        )
        check(
            "read tools flagged read-only",
            tools["list_new_joiners"].annotations.readOnlyHint is True
            and tools["get_new_joiner"].annotations.readOnlyHint is True,
        )
        check(
            "delete flagged destructive",
            tools["delete_new_joiner"].annotations.destructiveHint is True
            and tools["create_new_joiner"].annotations.destructiveHint is False,
        )
        check(
            "required args in schema",
            tools["create_new_joiner"].inputSchema["required"] == list(NEW),
            tools["create_new_joiner"].inputSchema.get("required"),
        )
        check(
            "optional filters not required",
            not tools["list_new_joiners"].inputSchema.get("required"),
            tools["list_new_joiners"].inputSchema.get("required"),
        )

        resources = {str(r.uri) for r in await client.list_resources()}
        check("dataset resource exposed", resources == {"new-joiners://all"}, resources)

        # READ
        r = await call(client, "api_health", {})
        check("api_health reaches the API", r.data["status"] == "ok", r.data)

        r = await call(client, "list_new_joiners", {})
        check("list returns 10 seed records", len(r.data) == 10, len(r.data))

        r = await call(client, "list_new_joiners", {"department": "finance"})
        check("filter passes through", len(r.data) == 4, len(r.data))

        r = await call(client, "list_new_joiners", {"limit": 2, "offset": 8})
        check(
            "pagination passes through",
            [x["employee_id"] for x in r.data] == ["NJ1009", "NJ1010"],
            r.data,
        )

        r = await call(client, "get_new_joiner", {"employee_id": "NJ1004"})
        check("get by id", r.data["name"] == "Anjali Rao", r.data)

        r = await call(client, "get_new_joiner", {"employee_id": "NOPE"})
        check("missing id -> tool error", r.is_error and "404" in error_text(r), error_text(r))

        # CREATE
        r = await call(client, "create_new_joiner", NEW)
        check("create", r.data["employee_id"] == "NJ9999", r.data)

        r = await call(client, "create_new_joiner", NEW)
        check("duplicate -> 409 surfaced", r.is_error and "409" in error_text(r), error_text(r))

        r = await call(client, "create_new_joiner", {**NEW, "employee_id": "NJ8888", "start_date": "nope"})
        check(
            "invalid date -> readable validation error",
            r.is_error and "start_date" in error_text(r),
            error_text(r),
        )

        r = await call(client, "create_new_joiner", {"employee_id": "NJ7777"})
        check("missing required args rejected", r.is_error, error_text(r)[:120])

        # UPDATE
        r = await call(client, "update_new_joiner", {"employee_id": "NJ9999", "location": "Kolkata"})
        check(
            "update changes only what was passed",
            r.data["location"] == "Kolkata" and r.data["name"] == "Test Person",
            r.data,
        )

        r = await call(client, "update_new_joiner", {"employee_id": "NJ9999"})
        check("update with no fields -> error", r.is_error and "at least one" in error_text(r))

        body = {k: v for k, v in NEW.items() if k != "employee_id"}
        r = await call(client, "replace_new_joiner", {"employee_id": "NJ9999", **body, "job_level": "L4"})
        check(
            "replace overwrites",
            r.data["job_level"] == "L4" and r.data["location"] == "Chennai",
            r.data,
        )

        # the resource reads through the same API
        contents = await client.read_resource("new-joiners://all")
        check("resource returns the dataset", len(json.loads(contents[0].text)) == 11, contents[0].text[:80])

        # persistence through the API, on disk
        on_disk = json.loads(_copy.read_text())
        check("write reached the data file", any(x["employee_id"] == "NJ9999" for x in on_disk))

        # DELETE
        r = await call(client, "delete_new_joiner", {"employee_id": "NJ9999"})
        check("delete", r.data == {"deleted": "NJ9999"}, r.data)

        r = await call(client, "delete_new_joiner", {"employee_id": "NJ9999"})
        check("delete again -> 404 surfaced", r.is_error and "404" in error_text(r))

        check("file back to 10 records", len(json.loads(_copy.read_text())) == 10)


async def unreachable_api_case():
    """With the API down, tools must fail with a clear message, not a traceback."""
    async with Client(mcp_server.mcp) as client:
        r = await call(client, "list_new_joiners", {})
        check(
            "API down -> actionable error",
            r.is_error and "Cannot reach" in error_text(r) and "main.py" in error_text(r),
            error_text(r),
        )


api = start_api()
try:
    asyncio.run(main())
finally:
    api.terminate()
    api.wait(timeout=10)

asyncio.run(unreachable_api_case())

check("original file untouched", len(json.loads(SOURCE.read_text())) == 10)
shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
