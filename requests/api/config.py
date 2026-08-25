"""Environment-backed settings for the Requests API.

Each field is read from the environment variable of the same name in upper
case (database_url -> DATABASE_URL), falling back to a .env file next to
this module, then to the default below. Values are validated once at import
time, so a typo like PORT=eight fails immediately with a clear message
instead of surfacing deep inside a request.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_HERE = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_HERE / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql://alchemy:alchemy@127.0.0.1:5433/alchemy",
        description="Postgres connection string for the access_requests table.",
    )

    host: str = Field(default="127.0.0.1", description="Bind address for `python main.py`")
    port: int = Field(default=8000, ge=1, le=65535, description="Port for `python main.py`")
    root_path: str = Field(
        default="",
        description="ASGI root_path, for when the API is served behind a path-prefixing proxy (e.g. /api)",
    )


settings = Settings()
