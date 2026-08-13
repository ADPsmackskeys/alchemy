"""Smoke test for the Peer Affinity MCP server.

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
SOURCE = API_DIR / "peer_affinity_scores.json"

_tmpdir = tempfile.mkdtemp()
_copy = Path(_tmpdir) / "peer_affinity_scores.json"
shutil.copy(SOURCE, _copy)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]

BASE_URL = f"http://127.0.0.1:{PORT}"
os.environ["PEER_AFFINITY_API_URL"] = BASE_URL  # read at import time by main

from fastmcp import Client  # noqa: E402

import main as mcp_server  # noqa: E402

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


def start_api():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=API_DIR,
        env={**os.environ, "PEER_AFFINITY_FILE": str(_copy)},
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
                "list_peer_affinity", "get_peer_affinity", "create_peer_affinity",
                "update_peer_affinity", "replace_peer_affinity", "delete_peer_affinity",
                "api_health",
            },
            sorted(tools),
        )
        check("read tools flagged read-only", tools["list_peer_affinity"].annotations.readOnlyHint is True)
        check(
            "delete flagged destructive",
            tools["delete_peer_affinity"].annotations.destructiveHint is True,
        )
        check(
            "affinity_score optional on create",
            "affinity_score" not in tools["create_peer_affinity"].inputSchema.get("required", []),
            tools["create_peer_affinity"].inputSchema.get("required"),
        )

        resources = {str(r.uri) for r in await client.list_resources()}
        check("dataset resource exposed", resources == {"peer-affinity://all"}, resources)

        r = await call(client, "api_health", {})
        check("api_health reaches the API", r.data["status"] == "ok", r.data)

        # READ
        r = await call(client, "list_peer_affinity", {})
        check("13 seed rows", len(r.data) == 13, len(r.data))

        r = await call(client, "list_peer_affinity", {"job_role": "financial analyst"})
        check("filter by job_role", len(r.data) == 4, len(r.data))

        r = await call(client, "list_peer_affinity", {"max_score": 50})
        check("outlier filter", [x["affinity_score"] for x in r.data] == [20], r.data)

        # the composite key has a space in job_role -- exercises percent-encoding
        r = await call(
            client, "get_peer_affinity", {"job_role": "Financial Analyst", "entitlement": "SAP_AP_INVOICE"}
        )
        check("get by composite key (space in job_role)", r.data["affinity_score"] == 80, r.data)

        r = await call(client, "get_peer_affinity", {"job_role": "Nobody", "entitlement": "X"})
        check("missing row -> error", r.is_error and "404" in error_text(r), error_text(r))

        # CREATE
        r = await call(client, "create_peer_affinity", NEW)
        check("create", r.data["job_role"] == "QA Engineer", r.data)

        r = await call(client, "create_peer_affinity", NEW)
        check("duplicate pair -> 409", r.is_error and "409" in error_text(r), error_text(r))

        r = await call(client, "create_peer_affinity", {**NEW, "entitlement": "GITHUB_DEV", "peer_count": 9})
        check("peer_count > total_peers rejected", r.is_error, error_text(r)[:90])

        without_score = {k: v for k, v in NEW.items() if k != "affinity_score"}
        r = await call(client, "create_peer_affinity", {**without_score, "entitlement": "GITHUB_DEV"})
        check("affinity_score computed when omitted", r.data["affinity_score"] == 75, r.data)

        # UPDATE
        r = await call(
            client, "update_peer_affinity",
            {"job_role": "QA Engineer", "entitlement": "JIRA_USER", "peer_count": 2},
        )
        check(
            "update recomputes score",
            r.data["affinity_score"] == 50 and r.data["total_peers"] == 4,
            r.data,
        )

        r = await call(
            client, "update_peer_affinity",
            {"job_role": "QA Engineer", "entitlement": "JIRA_USER", "peer_count": 1, "affinity_score": 99},
        )
        check("explicit score wins over recompute", r.data["affinity_score"] == 99, r.data)

        r = await call(
            client, "update_peer_affinity", {"job_role": "QA Engineer", "entitlement": "JIRA_USER"}
        )
        check("empty update -> error", r.is_error and "at least one" in error_text(r))

        r = await call(
            client, "replace_peer_affinity",
            {"job_role": "QA Engineer", "entitlement": "JIRA_USER",
             "department": "Technology", "peer_count": 3, "total_peers": 6},
        )
        check(
            "replace overwrites and recomputes",
            r.data["total_peers"] == 6 and r.data["affinity_score"] == 50,
            r.data,
        )

        contents = await client.read_resource("peer-affinity://all")
        check("resource returns the dataset", len(json.loads(contents[0].text)) == 15, contents[0].text[:60])

        on_disk = json.loads(_copy.read_text())
        check(
            "write reached the data file",
            any(x["job_role"] == "QA Engineer" and x["entitlement"] == "JIRA_USER" for x in on_disk),
        )

        # DELETE
        r = await call(client, "delete_peer_affinity", {"job_role": "QA Engineer", "entitlement": "JIRA_USER"})
        check("delete", r.data == {"deleted": {"job_role": "QA Engineer", "entitlement": "JIRA_USER"}}, r.data)

        r = await call(client, "delete_peer_affinity", {"job_role": "QA Engineer", "entitlement": "GITHUB_DEV"})
        check("delete second row", r.is_error is False, r.data)

        r = await call(client, "delete_peer_affinity", {"job_role": "QA Engineer", "entitlement": "JIRA_USER"})
        check("delete again -> 404", r.is_error and "404" in error_text(r))

        check("file back to 13 rows", len(json.loads(_copy.read_text())) == 13)


async def unreachable_api_case():
    """With the API down, tools must fail with a clear message, not a traceback."""
    async with Client(mcp_server.mcp) as client:
        r = await call(client, "list_peer_affinity", {})
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

check("original file untouched", len(json.loads(SOURCE.read_text())) == 13)
shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
