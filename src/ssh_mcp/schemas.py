from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, SecretStr, field_validator


class SessionStatus(StrEnum):
    connecting = "connecting"
    connected = "connected"
    failed = "failed"
    disconnected = "disconnected"


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    finished = "finished"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"
    disconnected = "disconnected"


class SessionView(BaseModel):
    session_id: str
    alias: str
    host: str
    port: int
    username: str
    status: SessionStatus
    sudo_enabled: bool
    sudo_password_configured: bool
    policy_version: int
    created_at: datetime
    last_activity_at: datetime | None = None
    error: str | None = None


class ConnectRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = None
    private_key: SecretStr | None = Field(default=None, max_length=128 * 1024)
    passphrase: SecretStr | None = None
    probe_id: str = Field(min_length=1, max_length=128)

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if not all(c.isascii() and (c.isalnum() or c in ".-:") for c in value):
            raise ValueError("invalid host")
        return value

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        if not all(c.isascii() and (c.isalnum() or c in "_-.") for c in value):
            raise ValueError("invalid username")
        return value

    @field_validator("password", "passphrase")
    @classmethod
    def bounded_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) > 4096:
            raise ValueError("authentication input too large")
        return value


class HostKeyView(BaseModel):
    host: str
    port: int
    algorithm: str
    fingerprint: str
    trusted: bool


class SudoRequest(BaseModel):
    enabled: bool


class SudoPasswordRequest(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=4096)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: SecretStr) -> SecretStr:
        if any(c in value.get_secret_value() for c in "\x00\r\n"):
            raise ValueError("sudo password must be one line without NUL")
        return value


class ExecuteRequest(BaseModel):
    session_id: str
    command: str = Field(min_length=1, max_length=128 * 1024)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    cwd: str | None = Field(default=None, max_length=4096)
    env: dict[str, str] | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class RunView(BaseModel):
    run_id: str
    session_id: str
    command: str
    source: str = "MCP"
    created_at: datetime
    policy_version: int | None = None
    status: RunStatus
    exit_code: int | None = None
    signal: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    sudo_used: bool = False
    error_code: str | None = None
    error: str | None = None


class OutputView(BaseModel):
    run_id: str
    stdout: str
    stderr: str
    next_stdout_cursor: int
    next_stderr_cursor: int
    complete: bool
    stdout_truncated: bool
    stderr_truncated: bool
