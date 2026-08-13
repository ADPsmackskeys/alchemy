"""Environment-backed settings for the Policy API.

Each field is read from the environment variable of the same name in upper
case (policy_rules_file -> POLICY_RULES_FILE), falling back to a .env file
next to this module, then to the default below. Values are validated once at
import time, so a typo like PORT=eight fails immediately with a clear
message instead of surfacing deep inside a request.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HERE = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_HERE / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    policy_rules_file: Path = Field(
        default=_HERE / "policy_rules.json",
        description="JSON array the API reads and rewrites. Must sit in a writable directory.",
    )

    host: str = Field(default="127.0.0.1", description="Bind address for `python main.py`")
    port: int = Field(default=8004, ge=1, le=65535, description="Port for `python main.py`")

    @field_validator("policy_rules_file")
    @classmethod
    def _anchor_to_module_dir(cls, value: Path) -> Path:
        # A relative path in .env means "next to config.py", not "relative to
        # whatever directory the process happened to be started from".
        return value if value.is_absolute() else (_HERE / value).resolve()


settings = Settings()
