from functools import lru_cache
from pathlib import Path
from tempfile import gettempdir

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SSH_MCP_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    known_hosts: Path = Path("./known_hosts")
    connections_file: Path | None = None
    transfer_root: Path = Path(gettempdir()) / "ssh-mcp-files"
    transfer_cache_root: Path = Path(gettempdir()) / "ssh-mcp-relay"
    transfer_cache_keep: int = Field(default=7, ge=1)
    max_transfers: int = Field(default=4, ge=1)
    transfer_timeout_seconds: int = Field(default=3600, ge=1)
    mcp_token: str | None = Field(default=None, repr=False)
    max_sessions: int = Field(default=32, ge=1)
    pairing_ttl_seconds: int = Field(default=300, ge=1)
    pairing_max_attempts: int = Field(default=5, ge=1)
    max_global_runs: int = Field(default=16, ge=1)
    max_session_runs: int = Field(default=4, ge=1)
    max_queue: int = Field(default=64, ge=1)
    max_task_records: int = Field(default=1000, ge=1)
    max_output_bytes: int = Field(default=1024 * 1024, ge=4)
    default_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    max_timeout_seconds: int = Field(default=3600, ge=1, le=3600)
    task_retention_seconds: int = Field(default=1800, ge=1)
    ssh_connect_timeout_seconds: int = Field(default=15, ge=1)
    ssh_keepalive_interval: int = Field(default=30, ge=1)

    @field_validator("mcp_token")
    @classmethod
    def empty_token(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def timeout_bounds(self) -> "Settings":
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("default_timeout_seconds must not exceed max_timeout_seconds")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
