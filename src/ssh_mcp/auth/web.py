import secrets
from dataclasses import dataclass

from ssh_mcp.auth.service import PairingService, TokenService, secure_equal


@dataclass
class BrowserSession:
    cookie: str
    csrf: str


class AuthState:
    def __init__(self, pairing: PairingService, tokens: TokenService) -> None:
        self.pairing = pairing
        self.tokens = tokens
        self.browser: BrowserSession | None = None
        self.token_revealed = False

    def pair(self, code: str) -> BrowserSession | None:
        if not self.pairing.verify(code):
            return None
        self.browser = BrowserSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        return self.browser

    def valid_cookie(self, cookie: str | None) -> bool:
        return (
            self.browser is not None
            and cookie is not None
            and secure_equal(self.browser.cookie, cookie)
        )

    def valid_csrf(self, csrf: str | None) -> bool:
        return (
            self.browser is not None and csrf is not None and secure_equal(self.browser.csrf, csrf)
        )
