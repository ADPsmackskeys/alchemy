"""Environment-backed settings for the New Joiners agent.

Each field is read from the environment variable of the same name in upper
case (mcp_url -> MCP_URL), falling back to a .env file next to this module,
then to the default below. Values are validated once at import time, so a
typo like PORT=eight fails immediately with a clear message instead of
surfacing deep inside a request.
"""

from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_HERE = Path(__file__).parent

# The three read-only tools on the new-joiners MCP server. Kept as an explicit
# list rather than derived from annotations at runtime: a governance agent
# should have an auditable tool surface that someone approved, not one that
# silently changes when the MCP server changes. agent.py additionally verifies
# every name here is flagged readOnlyHint by the server and refuses to start
# otherwise, so the list cannot quietly grow write access.
DEFAULT_ALLOWED_TOOLS = ["list_new_joiners", "get_new_joiner", "api_health"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_HERE / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        description="Gemini API key. Without it the agent starts but /ask returns 503.",
    )
    model: str = Field(
        default="gemini-3.7-flash",
        description="Gemini model id. Must be one your key can access.",
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="0 keeps answers repeatable, which is what a governance tool wants.",
    )

    mcp_url: str = Field(
        default="http://127.0.0.1:8100/mcp",
        description="Streamable-HTTP endpoint of the new-joiners MCP server",
    )
    mcp_timeout: float = Field(default=30.0, gt=0, description="MCP call timeout in seconds")

    # NoDecode stops pydantic-settings JSON-decoding this in the env source,
    # which would reject the comma-separated form before _split_csv ever runs.
    allowed_tools: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS),
        description="Tools the agent may call. Every one must be read-only.",
    )
    recursion_limit: int = Field(
        default=12,
        ge=2,
        le=100,
        description="Cap on agent steps, so a confused model cannot loop indefinitely.",
    )

    host: str = Field(default="127.0.0.1", description="Bind address for `python main.py`")
    port: int = Field(default=8200, ge=1, le=65535, description="Port for `python main.py`")

    @field_validator("mcp_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        # Accept ALLOWED_TOOLS=a,b,c as well as a JSON array. NoDecode turned
        # off the built-in JSON handling to allow the comma form, so both are
        # parsed here -- the comma form is what anyone actually types.
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value


settings = Settings()
