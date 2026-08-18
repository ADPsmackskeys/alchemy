"""HTTP service wrapping the New Joiners agent.

    python main.py

POST /ask is stateless on purpose -- one question, one answer, no session.
Conversation memory belongs to the supervisor, so the six domain agents
cannot drift into six divergent views of the same conversation.

The agent is built lazily on first use rather than at startup, so this
service can boot before the MCP server and the API it depends on. Until the
whole chain is up, /ask returns 503 with the reason and /health says which
piece is missing.
"""

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

import agent as agent_module
from config import settings

_agent = None
_agent_error: str | None = None


async def get_agent():
    """Build once, reuse. Re-attempted on each request until it succeeds."""
    global _agent, _agent_error
    if _agent is not None:
        return _agent
    try:
        _agent = await agent_module.build_agent()
        _agent_error = None
    except agent_module.AgentUnavailable as exc:
        _agent_error = str(exc)
        raise
    except agent_module.UnsafeToolError as exc:
        _agent_error = str(exc)
        raise
    return _agent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Try once at startup so misconfiguration shows up in the logs immediately,
    # but never block boot on it.
    try:
        await get_agent()
    except (agent_module.AgentUnavailable, agent_module.UnsafeToolError):
        pass
    yield


app = FastAPI(
    title="New Joiners Agent",
    description=(
        "Natural-language questions over the new joiners dataset. Read-only: the "
        "agent can look records up but cannot change them, and it does not decide "
        "access."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000, examples=["Who joins Finance in September?"])


class Step(BaseModel):
    tool: str | None
    args: dict[str, Any]
    result_preview: str


class AskResponse(BaseModel):
    question: str
    answer: str
    steps: list[Step] = Field(description="Every tool call made, so the answer can be checked")
    model: str


@app.post(
    "/ask",
    response_model=AskResponse,
    tags=["agent"],
    summary="Ask a question about new joiners",
    description=(
        "Answers from the new-joiners MCP tools only. The tool calls behind the "
        "answer are always returned so it can be verified.\n\n"
        "Returns 503 when no Gemini API key is configured or the MCP server is "
        "unreachable."
    ),
    responses={503: {"description": "Agent unavailable: no API key, or MCP server down."}},
)
async def ask(payload: AskRequest):
    try:
        active = await get_agent()
    except (agent_module.AgentUnavailable, agent_module.UnsafeToolError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        return await agent_module.ask(payload.question, agent=active)
    except Exception as exc:  # noqa: BLE001 - one bad call should not kill the service
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Agent run failed ({exc.__class__.__name__}): {exc}",
        ) from exc


@app.get("/health", tags=["meta"])
def health():
    return {
        "status": "ok" if _agent is not None else "degraded",
        "model": settings.model,
        "api_key_configured": bool(settings.gemini_api_key),
        "mcp_url": settings.mcp_url,
        "allowed_tools": settings.allowed_tools,
        "detail": _agent_error,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
