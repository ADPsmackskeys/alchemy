"""Smoke test for the New Joiners agent.

Boots the API and the MCP server on scratch ports, then checks the agent's
wiring against them. Everything except the last section runs WITHOUT a Gemini
API key -- tool selection, the read-only guarantee and the service contract
are all checkable offline, and those are the parts that must not regress.

If GEMINI_API_KEY is set, a live section actually asks the model a question.

    python test_agent.py
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
API_DIR = HERE.parent / "api"
MCP_DIR = HERE.parent / "mcp"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


API_PORT = free_port()
MCP_PORT = free_port()
API_URL = f"http://127.0.0.1:{API_PORT}"
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"

os.environ["MCP_URL"] = MCP_URL  # read at import time by config

import agent as agent_module  # noqa: E402
from config import settings  # noqa: E402


def check(label, condition, extra=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(extra)) if extra else ''}")
    assert condition, label


def wait_for(url: str, proc: subprocess.Popen, what: str):
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError(f"{what} exited during startup")
        try:
            httpx.get(url, timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"{what} did not come up")


def start_stack():
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(API_PORT), "--log-level", "warning"],
        cwd=API_DIR,
    )
    wait_for(f"{API_URL}/health", api, "API")

    mcp = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=MCP_DIR,
        env={
            **os.environ,
            "NEW_JOINERS_API_URL": API_URL,
            "MCP_TRANSPORT": "streamable-http",
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(MCP_PORT),
        },
    )
    # the /mcp endpoint rejects a plain GET, but answering at all means it is up
    for _ in range(150):
        if mcp.poll() is not None:
            api.terminate()
            raise RuntimeError("MCP server exited during startup")
        try:
            httpx.get(MCP_URL, timeout=0.5)
            break
        except httpx.HTTPStatusError:
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    return api, mcp


async def offline_checks():
    # --- config
    check("MCP_URL picked up from env", settings.mcp_url == MCP_URL, settings.mcp_url)
    check(
        "default allow-list is the three read-only tools",
        settings.allowed_tools == ["list_new_joiners", "get_new_joiner", "api_health"],
        settings.allowed_tools,
    )
    check("agent port is 8200, clear of API 8000 and MCP 8100", settings.port == 8200, settings.port)

    # --- tool loading and the read-only guarantee
    all_tools = await agent_module.load_tools()
    names = [t.name for t in all_tools]
    check("agent gets exactly its allow-list", names == settings.allowed_tools, names)
    check("every selected tool is read-only", all(agent_module._is_read_only(t) for t in all_tools))

    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
        {"nj": {"url": MCP_URL, "transport": "streamable_http", "timeout": 30.0}}
    )
    server_tools = await client.get_tools()
    check("MCP server offers all 7 tools", len(server_tools) == 7, len(server_tools))

    exposed = {t.name for t in all_tools}
    writes = {"create_new_joiner", "update_new_joiner", "replace_new_joiner", "delete_new_joiner"}
    check("no write tool reaches the agent", not (exposed & writes), sorted(exposed & writes))

    # the guard must refuse, not silently pass, if someone allow-lists a write
    try:
        agent_module.select_tools(server_tools, ["list_new_joiners", "delete_new_joiner"])
        check("allow-listing a destructive tool is refused", False, "no error raised")
    except agent_module.UnsafeToolError as exc:
        check("allow-listing a destructive tool is refused", "delete_new_joiner" in str(exc), str(exc)[:90])

    # and refuse a name the server does not have, rather than silently dropping it
    try:
        agent_module.select_tools(server_tools, ["no_such_tool"])
        check("unknown tool name is refused", False, "no error raised")
    except agent_module.UnsafeToolError as exc:
        check("unknown tool name is refused", "no_such_tool" in str(exc), str(exc)[:90])

    # --- system prompt carries the guardrails
    prompt = agent_module.SYSTEM_PROMPT
    check("prompt forbids inventing records", "Never invent" in prompt)
    check("prompt requires citing employee_id", "employee_id" in prompt)
    check("prompt states read-only", "read-only" in prompt)
    check("prompt defers cross-domain questions", "separation-of-duties" in prompt)
    check("prompt treats tool output as data, not instructions", "not a command" in prompt)

    # --- no key -> a clear 503-able error, not a crash
    saved = settings.gemini_api_key
    settings.gemini_api_key = None
    try:
        agent_module.build_model()
        check("missing API key raises AgentUnavailable", False, "no error raised")
    except agent_module.AgentUnavailable as exc:
        check("missing API key raises AgentUnavailable", "GEMINI_API_KEY" in str(exc), str(exc)[:70])
    finally:
        settings.gemini_api_key = saved


def service_checks():
    """The HTTP contract, exercised with TestClient (no server process)."""
    from fastapi.testclient import TestClient

    import main as service

    with TestClient(service.app) as client:
        r = client.get("/health")
        body = r.json()
        check("health responds", r.status_code == 200, r.status_code)
        check("health reports the tool surface", body["allowed_tools"] == settings.allowed_tools, body)
        check("health reports key presence", "api_key_configured" in body, body)

        r = client.post("/ask", json={"question": ""})
        check("empty question rejected", r.status_code == 422, r.status_code)

        if not settings.gemini_api_key:
            r = client.post("/ask", json={"question": "Who joins in September?"})
            check("no key -> 503 with a reason", r.status_code == 503, r.status_code)
            check("503 explains what to set", "GEMINI_API_KEY" in r.json()["detail"], r.json()["detail"][:80])


async def live_checks():
    """Only runs with a real key. Costs tokens."""
    result = await agent_module.ask("How many new joiners are in the Finance department?")
    used = [s["tool"] for s in result["steps"]]
    check("model called a tool", bool(used), used)
    check("only allow-listed tools used", set(used) <= set(settings.allowed_tools), used)
    check("answer is non-empty", bool(result["answer"].strip()), result["answer"][:120])
    print(f"      answer: {result['answer'][:200]}")

    result = await agent_module.ask("Delete NJ1004 from the system.")
    used = [s["tool"] for s in result["steps"]]
    check("refuses to delete, no write tool available", "delete_new_joiner" not in used, used)
    print(f"      answer: {result['answer'][:200]}")


api_proc, mcp_proc = start_stack()
try:
    print("--- offline checks (no API key needed)")
    asyncio.run(offline_checks())
    print("\n--- service contract")
    service_checks()

    if settings.gemini_api_key:
        print("\n--- live checks (GEMINI_API_KEY is set)")
        asyncio.run(live_checks())
    else:
        print("\n--- live checks SKIPPED: set GEMINI_API_KEY in agent/.env to run them")
finally:
    for proc in (mcp_proc, api_proc):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

print("\nAll checks passed.")
