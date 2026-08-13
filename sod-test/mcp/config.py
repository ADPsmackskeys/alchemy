"""Environment-backed settings for the SoD Rules MCP server.

Each field is read from the environment variable of the same name in upper
case (sod_api_url -> SOD_API_URL), falling back to a .env file next
to this module, then to the default below. Values are validated once at
import time, so a typo like MCP_PORT=eight fails immediately with a clear
message instead of surfacing deep inside a tool call.
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

    # This server is an HTTP client of the SoD Rules API.
    sod_api_url: str = Field(
        default="http://127.0.0.1:8005",
        description="Base URL of the running API, for the MCP server to call",
    )
    sod_api_timeout: float = Field(
        default=10.0, gt=0, description="Per-request timeout in seconds"
    )
    mcp_transport: Literal["stdio", "http", "sse", "streamable-http"] = Field(
        default="streamable-http",
        description="How main.py listens. Use 'stdio' for a client that spawns it directly.",
    )
    mcp_host: str = Field(default="127.0.0.1", description="Bind address for the MCP server")
    mcp_port: int = Field(
        default=8105,
        ge=1,
        le=65535,
        description="Port for the MCP server. Must differ from the API's 8005.",
    )

    @field_validator("sod_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


settings = Settings()
