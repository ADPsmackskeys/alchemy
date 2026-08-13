"""Environment-backed settings for the New Joiners API.

Each field is read from the environment variable of the same name in upper
case (new_joiners_file -> NEW_JOINERS_FILE), falling back to a .env file
next to this module, then to the default below. Values are validated once
at import time, so a typo like PORT=eight fails immediately with a clear
message instead of surfacing deep inside a request.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HERE = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_HERE / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Used by mcp_server.py, which talks to this API over HTTP.
    new_joiners_api_url: str = Field(
        default="http://127.0.0.1:8000",
        description="Base URL of the running API, for the MCP server to call",
    )
    new_joiners_api_timeout: float = Field(
        default=10.0, gt=0, description="Per-request timeout in seconds for the MCP server"
    )
    mcp_transport: Literal["stdio", "http", "sse", "streamable-http"] = Field(
        default="streamable-http",
        description="How mcp_server.py listens. Use 'stdio' for a client that spawns it directly.",
    )
    mcp_host: str = Field(default="127.0.0.1", description="Bind address for the MCP server")
    mcp_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Port for the MCP server. Must differ from PORT, which the API uses.",
    )

    @field_validator("new_joiners_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


settings = Settings()
