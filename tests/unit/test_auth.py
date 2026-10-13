import time

from ssh_mcp.auth.service import PairingService, TokenService


def test_pairing_expires_and_attempts() -> None:
    pairing = PairingService(300, 3)
    code = pairing.code
    assert not pairing.verify("invalid")
    assert pairing.verify(code)
    assert not pairing.verify(code), "pairing must be one-time"


def test_pairing_limits() -> None:
    pairing = PairingService(300, 2)
    pairing.verify("wrong")
    pairing.verify("wrong")
    assert not pairing.verify(pairing.code)
    pairing.rotate()
    pairing.expires_at = time.monotonic() - 1
    assert not pairing.verify(pairing.code)


def test_mcp_token_rotation() -> None:
    tokens = TokenService("original")
    assert tokens.verify("original")
    assert not tokens.verify(None)
    new = tokens.rotate()
    assert tokens.verify(new)
    assert not tokens.verify("original")
