import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass


def secure_equal(left: str, right: str) -> bool:
    # compare_digest(str, str) raises for non-ASCII input; reject malformed
    # credentials normally, including JSON strings with unpaired surrogates.
    return secrets.compare_digest(
        left.encode("utf-8", errors="surrogatepass"),
        right.encode("utf-8", errors="surrogatepass"),
    )


@dataclass
class PairingService:
    ttl_seconds: int
    max_attempts: int
    code: str = ""
    expires_at: float = 0
    attempts: int = 0

    def __post_init__(self) -> None:
        self.rotate()

    def rotate(self) -> str:
        self.code = f"{secrets.randbelow(1_000_000):06d}"
        self.expires_at = time.monotonic() + self.ttl_seconds
        self.attempts = 0
        return self.code

    def verify(self, candidate: str) -> bool:
        if self.attempts >= self.max_attempts or time.monotonic() > self.expires_at:
            return False
        self.attempts += 1
        valid = secure_equal(candidate, self.code)
        if valid:
            self.expires_at = 0
        return valid


class TokenService:
    def __init__(self, configured: str | None = None) -> None:
        self._token = configured or secrets.token_urlsafe(32)
        self.revoked = asyncio.Event()

    @property
    def token_hint(self) -> str:
        return hashlib.sha256(self._token.encode()).hexdigest()[:12]

    def verify(self, candidate: str | None) -> bool:
        return candidate is not None and secure_equal(candidate, self._token)

    def rotate(self) -> str:
        self.revoked.set()
        self.revoked = asyncio.Event()
        self._token = secrets.token_urlsafe(32)
        return self._token

    def current(self) -> str:
        return self._token
