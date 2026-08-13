"""Smoke test for the Identities MCP server.

Boots the real FastAPI app from ../api on a scratch port against a throwaway
copy of the data file, then drives main.py through an in-memory FastMCP
client -- so this exercises the actual tool schemas and dispatch, not just
the Python functions.

Requires FastMCP 3.x (see requirements.txt).

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
API_DIR = HERE.parent / "api"
SOURCE = API_DIR / "identities.json"

_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "identities.json"
shutil.copy(SOURCE, _copy)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]

BASE_URL = f"http://127.0.0.1:{PORT}"
os.environ["IDENTITIES_API_URL"] = BASE_URL  # read at import time by main

from fastmcp import Client  # noqa: E402

import main as mcp_server  # noqa: E402

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


def start_api():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=API_DIR,
        env={**os.environ, "IDENTITIES_FILE": str(_copy)},
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


async def main_test():
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        check(
            "all tools exposed",
            set(tools)
            == {
                "list_identities", "get_identity", "get_identity_entitlements",
                "create_identity", "update_identity", "replace_identity",
                "delete_identity", "api_health",
            },
            sorted(tools),
        )
        check(
            "read tools flagged read-only",
            tools["list_identities"].annotations.readOnlyHint is True
            and tools["get_identity_entitlements"].annotations.readOnlyHint is True,
        )
        check("delete flagged destructive", tools["delete_identity"].annotations.destructiveHint is True)
        check(
            "required args in schema",
            tools["create_identity"].inputSchema["required"] == list(NEW),
            tools["create_identity"].inputSchema.get("required"),
        )

        resources = {str(r.uri) for r in await client.list_resources()}
        check("dataset resource exposed", resources == {"identities://all"}, resources)

        r = await call(client, "api_health", {})
        check("api_health reaches the API", r.data["status"] == "ok", r.data)

        # READ
        r = await call(client, "list_identities", {})
        check("10 seed records", len(r.data) == 10, len(r.data))

        r = await call(client, "list_identities", {"department": "finance"})
        check("filter by department", len(r.data) == 5, len(r.data))

        r = await call(client, "list_identities", {"entitlement": "github_dev"})
        check("filter by entitlement", len(r.data) == 3, len(r.data))

        r = await call(client, "list_identities", {"entitlement": "SAP_FIN"})
        check("entitlement filter is exact, not substring", len(r.data) == 0, len(r.data))

        r = await call(client, "list_identities", {"limit": 2, "offset": 8})
        check(
            "pagination passes through",
            [x["employee_id"] for x in r.data] == ["EMP009", "EMP010"],
            r.data,
        )

        r = await call(client, "get_identity", {"employee_id": "EMP009"})
        check("get by id", r.data["name"] == "Meera", r.data)

        r = await call(client, "get_identity_entitlements", {"employee_id": "EMP009"})
        check("parsed entitlements", r.data == ["RSA_GRC", "POWERBI_RISK", "RISK_PORTAL"], r.data)

        r = await call(client, "get_identity", {"employee_id": "NOPE"})
        check("missing id -> error", r.is_error and "404" in error_text(r), error_text(r))

        # CREATE
        r = await call(client, "create_identity", NEW)
        check("create", r.data["employee_id"] == "EMP999", r.data)

        r = await call(client, "create_identity", NEW)
        check("duplicate -> 409", r.is_error and "409" in error_text(r), error_text(r))

        r = await call(client, "create_identity", {**NEW, "employee_id": "EMP998", "name": ""})
        check("empty name -> validation error", r.is_error, error_text(r)[:90])

        r = await call(client, "create_identity", {"employee_id": "EMP997"})
        check("missing required args rejected", r.is_error, error_text(r)[:90])

        # UPDATE
        r = await call(client, "update_identity", {"employee_id": "EMP999", "location": "Kolkata"})
        check(
            "update only what was passed",
            r.data["location"] == "Kolkata" and r.data["name"] == "Test Person",
            r.data,
        )

        r = await call(client, "update_identity", {"employee_id": "EMP999"})
        check("empty update -> error", r.is_error and "at least one" in error_text(r))

        r = await call(
            client, "update_identity", {"employee_id": "EMP999", "entitlements": " JIRA_USER ;; GITHUB_DEV ;"}
        )
        check("entitlements normalized", r.data["entitlements"] == "JIRA_USER;GITHUB_DEV", r.data)

        body = {k: v for k, v in NEW.items() if k != "employee_id"}
        r = await call(client, "replace_identity", {"employee_id": "EMP999", **body, "job_level": "L4"})
        check(
            "replace overwrites",
            r.data["job_level"] == "L4" and r.data["location"] == "Chennai",
            r.data,
        )

        contents = await client.read_resource("identities://all")
        check("resource returns the dataset", len(json.loads(contents[0].text)) == 11, contents[0].text[:60])

        on_disk = json.loads(_copy.read_text())
        check("write reached the data file", any(x["employee_id"] == "EMP999" for x in on_disk))

        # DELETE
        r = await call(client, "delete_identity", {"employee_id": "EMP999"})
        check("delete", r.data == {"deleted": "EMP999"}, r.data)

        r = await call(client, "delete_identity", {"employee_id": "EMP999"})
        check("delete again -> 404", r.is_error and "404" in error_text(r))

        check("file back to 10 records", len(json.loads(_copy.read_text())) == 10)


async def unreachable_api_case():
    """With the API down, tools must fail with a clear message, not a traceback."""
    async with Client(mcp_server.mcp) as client:
        r = await call(client, "list_identities", {})
        check(
            "API down -> actionable error",
            r.is_error and "Cannot reach" in error_text(r),
            error_text(r),
        )


api = start_api()
try:
    asyncio.run(main_test())
finally:
    api.terminate()
    api.wait(timeout=10)

asyncio.run(unreachable_api_case())

check("original file untouched", len(json.loads(SOURCE.read_text())) == 10)
shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
