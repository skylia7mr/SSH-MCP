from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SSH_MCP_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    known_hosts: Path = Path("./known_hosts")
    mcp_token: str | None = None
    max_sessions: int = 32
    pairing_ttl_seconds: int = 300
    pairing_max_attempts: int = 5
    max_global_runs: int = 16
    max_session_runs: int = 4
    max_queue: int = 64
    max_task_records: int = 1000
    max_output_bytes: int = 1024 * 1024
    default_timeout_seconds: int = 60
    max_timeout_seconds: int = 3600
    task_retention_seconds: int = 1800
    ssh_connect_timeout_seconds: int = 15
    ssh_keepalive_interval: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
