"""Smoke test for the Policy MCP server.

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
SOURCE = API_DIR / "policy_rules.json"

_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "policy_rules.json"
shutil.copy(SOURCE, _copy)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]

BASE_URL = f"http://127.0.0.1:{PORT}"
os.environ["POLICY_API_URL"] = BASE_URL  # read at import time by main

from fastmcp import Client  # noqa: E402

import main as mcp_server  # noqa: E402

NEW = {
    "policy_id": "POL999",
    "policy_name": "SoD Block",
    "type": "DENY",
    "rule": "SAP_VENDOR_CREATE + SAP_PAYMENT_APPROVER",
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


def start_api():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=API_DIR,
        env={**os.environ, "POLICY_RULES_FILE": str(_copy)},
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
                "list_policies", "get_policy", "create_policy",
                "update_policy", "replace_policy", "delete_policy", "api_health",
            },
            sorted(tools),
        )
        check("read tools flagged read-only", tools["list_policies"].annotations.readOnlyHint is True)
        check("delete flagged destructive", tools["delete_policy"].annotations.destructiveHint is True)
        check(
            "required args in schema",
            tools["create_policy"].inputSchema["required"] == list(NEW),
            tools["create_policy"].inputSchema.get("required"),
        )

        resources = {str(r.uri) for r in await client.list_resources()}
        check("dataset resource exposed", resources == {"policy://all"}, resources)

        r = await call(client, "api_health", {})
        check("api_health reaches the API", r.data["status"] == "ok", r.data)

        # READ
        r = await call(client, "list_policies", {})
        check("7 seed policies", len(r.data) == 7, len(r.data))

        r = await call(client, "list_policies", {"type": "ALLOW"})
        check("filter by type", len(r.data) == 5, len(r.data))

        r = await call(client, "list_policies", {"policy_name": "finance birthright"})
        check("filter by name", len(r.data) == 2, len(r.data))

        r = await call(client, "list_policies", {"rule_contains": "risk_score"})
        check("rule substring search", len(r.data) == 2, len(r.data))

        r = await call(client, "list_policies", {"type": "MAYBE"})
        check("bad enum rejected", r.is_error, error_text(r)[:90])

        r = await call(client, "get_policy", {"policy_id": "POL005"})
        check("get by id", r.data["type"] == "HUMAN_APPROVAL", r.data)

        r = await call(client, "get_policy", {"policy_id": "NOPE"})
        check("missing id -> error", r.is_error and "404" in error_text(r), error_text(r))

        # CREATE
        r = await call(client, "create_policy", NEW)
        check("create", r.data["policy_id"] == "POL999", r.data)

        r = await call(client, "create_policy", NEW)
        check("duplicate -> 409", r.is_error and "409" in error_text(r), error_text(r))

        r = await call(client, "create_policy", {**NEW, "policy_id": "POL998", "type": "PERHAPS"})
        check("invalid type rejected", r.is_error, error_text(r)[:90])

        # UPDATE
        r = await call(client, "update_policy", {"policy_id": "POL999", "type": "HUMAN_APPROVAL"})
        check(
            "update only what was passed",
            r.data["type"] == "HUMAN_APPROVAL" and r.data["policy_name"] == "SoD Block",
            r.data,
        )

        r = await call(client, "update_policy", {"policy_id": "POL999"})
        check("empty update -> error", r.is_error and "at least one" in error_text(r))

        r = await call(client, "replace_policy", {**NEW, "policy_name": "SoD Hard Block"})
        check(
            "replace overwrites",
            r.data["policy_name"] == "SoD Hard Block" and r.data["type"] == "DENY",
            r.data,
        )

        contents = await client.read_resource("policy://all")
        check("resource returns the dataset", len(json.loads(contents[0].text)) == 8, contents[0].text[:60])

        on_disk = json.loads(_copy.read_text())
        check("write reached the data file", any(x["policy_id"] == "POL999" for x in on_disk))

        # DELETE
        r = await call(client, "delete_policy", {"policy_id": "POL999"})
        check("delete", r.data == {"deleted": "POL999"}, r.data)

        r = await call(client, "delete_policy", {"policy_id": "POL999"})
        check("delete again -> 404", r.is_error and "404" in error_text(r))

        check("file back to 7 records", len(json.loads(_copy.read_text())) == 7)


async def unreachable_api_case():
    """With the API down, tools must fail with a clear message, not a traceback."""
    async with Client(mcp_server.mcp) as client:
        r = await call(client, "list_policies", {})
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

check("original file untouched", len(json.loads(SOURCE.read_text())) == 7)
shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
