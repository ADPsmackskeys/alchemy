"""Smoke test for the Entitlements MCP server.

Boots the real FastAPI app from ../api on a scratch port against throwaway
copies of the data files, then drives main.py through an in-memory FastMCP
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
CATALOG_SOURCE = API_DIR / "entitlement_catalog.json"
SCORES_SOURCE = API_DIR / "entitlement_risk_scores.json"

_tmpdir = tempfile.mkdtemp()
_catalog = Path(_tmpdir) / "entitlement_catalog.json"
_scores = Path(_tmpdir) / "entitlement_risk_scores.json"
shutil.copy(CATALOG_SOURCE, _catalog)
shutil.copy(SCORES_SOURCE, _scores)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]

BASE_URL = f"http://127.0.0.1:{PORT}"
os.environ["ENTITLEMENTS_API_URL"] = BASE_URL  # read at import time by main

from fastmcp import Client  # noqa: E402

import main as mcp_server  # noqa: E402

NEW_ENT = {
    "entitlement_id": "ENT999",
    "entitlement_name": "ZZ_TEST_ENTITLEMENT",
    "application": "SharePoint",
    "owner": "Audit IT",
}
NEW_SCORE = {
    "entitlement_name": "ZZ_TEST_ENTITLEMENT",
    "application": "SharePoint",
    "risk_score": 20,
    "risk_category": "Low",
}


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


def start_api():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=API_DIR,
        env={
            **os.environ,
            "ENTITLEMENT_CATALOG_FILE": str(_catalog),
            "ENTITLEMENT_RISK_SCORES_FILE": str(_scores),
        },
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
                "list_entitlements", "get_entitlement", "create_entitlement",
                "update_entitlement", "replace_entitlement", "delete_entitlement",
                "list_risk_scores", "get_risk_score", "create_risk_score",
                "update_risk_score", "replace_risk_score", "delete_risk_score",
                "api_health",
            },
            sorted(tools),
        )
        check(
            "read tools flagged read-only",
            tools["list_entitlements"].annotations.readOnlyHint is True
            and tools["list_risk_scores"].annotations.readOnlyHint is True,
        )
        check(
            "deletes flagged destructive",
            tools["delete_entitlement"].annotations.destructiveHint is True
            and tools["delete_risk_score"].annotations.destructiveHint is True,
        )
        check(
            "required args in schema",
            tools["create_entitlement"].inputSchema["required"] == list(NEW_ENT),
            tools["create_entitlement"].inputSchema.get("required"),
        )

        resources = {str(r.uri) for r in await client.list_resources()}
        check(
            "both dataset resources exposed",
            resources == {"entitlements://catalog", "entitlements://risk-scores"},
            resources,
        )

        r = await call(client, "api_health", {})
        check("api_health reaches the API", r.data["status"] == "ok", r.data)

        # --- catalog
        r = await call(client, "list_entitlements", {})
        check("catalog: 18 seed rows", r.data["returned"] == 18, r.data["returned"])

        r = await call(client, "list_entitlements", {"applications": ["sap ecc"]})
        check("catalog: filter passes through", r.data["returned"] == 4, r.data["returned"])

        r = await call(
            client,
            "list_entitlements",
            {"entitlement_names": ["JIRA_USER", "GITHUB_DEV", "CONFLUENCE_USER"]},
        )
        check(
            "catalog: three names in ONE call",
            sorted(x["entitlement_name"] for x in r.data["records"])
            == ["CONFLUENCE_USER", "GITHUB_DEV", "JIRA_USER"],
            r.data["records"],
        )
        check("catalog: nothing missing when all exist", r.data["missing"] == [], r.data)

        r = await call(
            client, "list_entitlements", {"entitlement_names": ["JIRA_USER", "NOT_A_REAL_ENT"]}
        )
        check(
            "catalog: an unknown name is named, not silently dropped",
            r.data["missing"] == ["NOT_A_REAL_ENT"] and r.data["returned"] == 1,
            r.data,
        )

        r = await call(client, "list_entitlements", {"limit": 3})
        check(
            "catalog: truncation is flagged",
            r.data["truncated"] is True and r.data["total_matching"] == 18,
            r.data,
        )

        r = await call(client, "list_entitlements", {"entitlement_names": []})
        check(
            "catalog: an empty filter list means unfiltered, not empty",
            r.data["returned"] == 18,
            r.data["returned"],
        )

        r = await call(client, "get_entitlement", {"entitlement_id": "ENT003"})
        check("catalog: get by id", r.data["entitlement_name"] == "POWERBI_FINANCE", r.data)

        r = await call(client, "get_entitlement", {"entitlement_id": "NOPE"})
        check("catalog: missing id -> error", r.is_error and "404" in error_text(r), error_text(r))

        r = await call(client, "create_entitlement", NEW_ENT)
        check("catalog: create", r.data["entitlement_id"] == "ENT999", r.data)

        r = await call(client, "create_entitlement", NEW_ENT)
        check("catalog: duplicate -> 409", r.is_error and "409" in error_text(r), error_text(r))

        r = await call(client, "update_entitlement", {"entitlement_id": "ENT999", "owner": "BI Team"})
        check(
            "catalog: update one field",
            r.data["owner"] == "BI Team" and r.data["entitlement_name"] == "ZZ_TEST_ENTITLEMENT",
            r.data,
        )

        r = await call(client, "update_entitlement", {"entitlement_id": "ENT999"})
        check("catalog: empty update -> error", r.is_error and "at least one" in error_text(r))

        r = await call(
            client,
            "replace_entitlement",
            {**NEW_ENT, "application": "SharePoint Online"},
        )
        check(
            "catalog: replace",
            r.data["application"] == "SharePoint Online" and r.data["owner"] == "Audit IT",
            r.data,
        )

        # --- risk scores
        r = await call(client, "list_risk_scores", {})
        check("scores: 18 seed rows", r.data["returned"] == 18, r.data["returned"])

        r = await call(client, "list_risk_scores", {"risk_categories": ["Critical"]})
        check("scores: filter by category", r.data["returned"] == 3, r.data["returned"])

        r = await call(client, "list_risk_scores", {"min_score": 70, "max_score": 95})
        check(
            "scores: numeric band",
            sorted(x["risk_score"] for x in r.data["records"]) == [70, 75, 90, 95],
            r.data["records"],
        )

        r = await call(
            client,
            "list_risk_scores",
            {"entitlement_names": ["JIRA_USER", "GITHUB_DEV", "CONFLUENCE_USER"]},
        )
        check(
            "scores: a whole set rated in ONE call",
            r.data["returned"] == 3 and r.data["missing"] == [],
            r.data,
        )

        r = await call(client, "get_risk_score", {"entitlement_name": "AD_DOMAIN_ADMIN"})
        check("scores: get by name", r.data["risk_score"] == 100, r.data)

        r = await call(client, "create_risk_score", NEW_SCORE)
        check("scores: create", r.data["entitlement_name"] == "ZZ_TEST_ENTITLEMENT", r.data)

        r = await call(client, "create_risk_score", {**NEW_SCORE, "risk_score": 101})
        check("scores: out-of-range -> error", r.is_error, error_text(r)[:90])

        r = await call(
            client,
            "update_risk_score",
            {"entitlement_name": "ZZ_TEST_ENTITLEMENT", "risk_score": 55, "risk_category": "Medium"},
        )
        check("scores: update", r.data["risk_score"] == 55, r.data)

        r = await call(client, "create_risk_score", {**NEW_SCORE, "risk_category": "Nonsense"})
        check("scores: bad enum rejected", r.is_error, error_text(r)[:90])

        # persistence
        on_disk = json.loads(_catalog.read_text())
        check("write reached the data file", any(x["entitlement_id"] == "ENT999" for x in on_disk))

        # resources read through the API
        contents = await client.read_resource("entitlements://risk-scores")
        check("resource returns scores", len(json.loads(contents[0].text)) == 19, contents[0].text[:60])

        # --- deletes
        r = await call(client, "delete_risk_score", {"entitlement_name": "ZZ_TEST_ENTITLEMENT"})
        check("scores: delete", r.data == {"deleted": "ZZ_TEST_ENTITLEMENT"}, r.data)

        r = await call(client, "delete_entitlement", {"entitlement_id": "ENT999"})
        check("catalog: delete", r.data == {"deleted": "ENT999"}, r.data)

        r = await call(client, "delete_entitlement", {"entitlement_id": "ENT999"})
        check("catalog: delete again -> 404", r.is_error and "404" in error_text(r))

        check("catalog back to 18", len(json.loads(_catalog.read_text())) == 18)
        check("scores back to 18", len(json.loads(_scores.read_text())) == 18)


async def unreachable_api_case():
    """With the API down, tools must fail with a clear message, not a traceback."""
    async with Client(mcp_server.mcp) as client:
        r = await call(client, "list_entitlements", {})
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

check("originals untouched", len(json.loads(CATALOG_SOURCE.read_text())) == 18)
check("originals untouched", len(json.loads(SCORES_SOURCE.read_text())) == 18)
shutil.rmtree(_tmpdir)
print("\nAll checks passed.")
