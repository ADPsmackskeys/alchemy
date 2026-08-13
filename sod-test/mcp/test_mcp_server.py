"""Smoke test for the SoD Rules MCP server.

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
SOURCE = API_DIR / "sod_rules.json"

_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "sod_rules.json"
shutil.copy(SOURCE, _copy)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]

BASE_URL = f"http://127.0.0.1:{PORT}"
os.environ["SOD_API_URL"] = BASE_URL  # read at import time by main

from fastmcp import Client  # noqa: E402

import main as mcp_server  # noqa: E402

NEW = {
    "sod_id": "SOD999",
    "entitlement_1": "SAP_PAYMENT_APPROVER",
    "entitlement_2": "AD_DOMAIN_ADMIN",
    "severity": "Critical",
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


def start_api():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=API_DIR,
        env={**os.environ, "SOD_RULES_FILE": str(_copy)},
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
                "list_sod_rules", "get_sod_rule", "create_sod_rule",
                "update_sod_rule", "replace_sod_rule", "delete_sod_rule", "api_health",
            },
            sorted(tools),
        )
        check("read tools flagged read-only", tools["list_sod_rules"].annotations.readOnlyHint is True)
        check("delete flagged destructive", tools["delete_sod_rule"].annotations.destructiveHint is True)
        check(
            "required args in schema",
            tools["create_sod_rule"].inputSchema["required"] == list(NEW),
            tools["create_sod_rule"].inputSchema.get("required"),
        )

        resources = {str(r.uri) for r in await client.list_resources()}
        check("dataset resource exposed", resources == {"sod://all"}, resources)

        r = await call(client, "api_health", {})
        check("api_health reaches the API", r.data["status"] == "ok", r.data)

        # READ
        r = await call(client, "list_sod_rules", {})
        check("3 seed rules", len(r.data) == 3, len(r.data))

        r = await call(client, "list_sod_rules", {"severity": "High"})
        check("filter by severity", len(r.data) == 2, len(r.data))

        r = await call(client, "list_sod_rules", {"entitlement": "sap_vendor_create"})
        check("entitlement matches either side", len(r.data) == 2, len(r.data))

        r = await call(client, "list_sod_rules", {"severity": "Catastrophic"})
        check("bad enum rejected", r.is_error, error_text(r)[:90])

        r = await call(client, "get_sod_rule", {"sod_id": "SOD001"})
        check("get by id", r.data["severity"] == "Critical", r.data)

        r = await call(client, "get_sod_rule", {"sod_id": "NOPE"})
        check("missing id -> error", r.is_error and "404" in error_text(r), error_text(r))

        # CREATE
        r = await call(client, "create_sod_rule", NEW)
        check("create", r.data["sod_id"] == "SOD999", r.data)

        r = await call(client, "create_sod_rule", NEW)
        check("duplicate sod_id -> 409", r.is_error and "409" in error_text(r), error_text(r))

        r = await call(
            client,
            "create_sod_rule",
            {
                "sod_id": "SOD998",
                "entitlement_1": "SAP_PAYMENT_APPROVER",
                "entitlement_2": "SAP_VENDOR_CREATE",
                "severity": "High",
            },
        )
        check("reversed pair of an existing rule -> 409", r.is_error and "409" in error_text(r), error_text(r))

        r = await call(
            client, "create_sod_rule", {**NEW, "sod_id": "SOD997", "entitlement_2": "SAP_PAYMENT_APPROVER"}
        )
        check("self-conflicting rule rejected", r.is_error, error_text(r)[:90])

        # UPDATE
        r = await call(client, "update_sod_rule", {"sod_id": "SOD999", "severity": "High"})
        check(
            "update only what was passed",
            r.data["severity"] == "High" and r.data["entitlement_1"] == "SAP_PAYMENT_APPROVER",
            r.data,
        )

        r = await call(client, "update_sod_rule", {"sod_id": "SOD999"})
        check("empty update -> error", r.is_error and "at least one" in error_text(r))

        r = await call(client, "update_sod_rule", {"sod_id": "SOD999", "entitlement_2": "SAP_VENDOR_CREATE"})
        check("patch into an existing pair -> 409", r.is_error and "409" in error_text(r), error_text(r))

        r = await call(client, "replace_sod_rule", {**NEW, "severity": "Medium"})
        check("replace overwrites", r.data["severity"] == "Medium", r.data)

        contents = await client.read_resource("sod://all")
        check("resource returns the dataset", len(json.loads(contents[0].text)) == 4, contents[0].text[:60])

        on_disk = json.loads(_copy.read_text())
        check("write reached the data file", any(x["sod_id"] == "SOD999" for x in on_disk))

        # DELETE
        r = await call(client, "delete_sod_rule", {"sod_id": "SOD999"})
        check("delete", r.data == {"deleted": "SOD999"}, r.data)

        r = await call(client, "delete_sod_rule", {"sod_id": "SOD999"})
        check("delete again -> 404", r.is_error and "404" in error_text(r))

        check("file back to 3 records", len(json.loads(_copy.read_text())) == 3)


async def unreachable_api_case():
    """With the API down, tools must fail with a clear message, not a traceback."""
    async with Client(mcp_server.mcp) as client:
        r = await call(client, "list_sod_rules", {})
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

check("original file untouched", len(json.loads(SOURCE.read_text())) == 3)
shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
