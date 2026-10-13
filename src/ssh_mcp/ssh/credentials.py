from typing import Any

import asyncssh


class OneShotCredentials(asyncssh.SSHClient):
    """Authentication via callbacks avoids secrets retained in AsyncSSH options."""

    def __init__(self, password: str | None, key: asyncssh.SSHKey | None) -> None:
        self.password = password
        self.key = key

    def password_auth_requested(self) -> str | None:
        value, self.password = self.password, None
        return value

    def public_key_auth_requested(self) -> Any:
        value, self.key = self.key, None
        return [value] if value is not None else None

    def clear(self) -> None:
        self.password = None
        self.key = None

    def auth_completed(self) -> None:
        self.clear()

    def connection_lost(self, exc: Exception | None) -> None:
        self.clear()
