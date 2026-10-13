from pathlib import Path

from fastapi.testclient import TestClient

from ssh_mcp.app import create_app
from ssh_mcp.config import Settings


def test_real_web_authorization_and_validation(tmp_path: Path) -> None:
    app = create_app(Settings(known_hosts=tmp_path / "known_hosts", mcp_token="fixed"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/api/sessions").status_code == 401
        assert c.get("/mcp", headers={"authorization": b"Bearer \xff"}).status_code == 401
        assert (
            c.post(
                "/api/auth/pair",
                json={"code": "测" * 6},
                headers={"origin": "http://127.0.0.1:8000"},
            ).status_code
            == 401
        )
        code = app.state.services.auth.pairing.code
        assert c.post("/api/auth/pair", json={"code": code}).status_code == 403
        assert (
            c.post(
                "/api/auth/pair", json={"code": code}, headers={"origin": "http://evil.test"}
            ).status_code
            == 403
        )
        headers = {"origin": "http://127.0.0.1:8000"}
        paired = c.post("/api/auth/pair", json={"code": code}, headers=headers)
        assert paired.status_code == 200
        assert "HttpOnly" in paired.headers["set-cookie"]
        assert "SameSite=strict" in paired.headers["set-cookie"]
        assert (
            c.get("/api/sessions", headers={"cookie": b"ssh_mcp_session=\xff"}).status_code == 401
        )
        assert (
            c.post(
                "/api/mcp/token/reveal",
                headers={"origin": "http://127.0.0.1:8000", "x-csrf-token": b"\xff"},
            ).status_code
            == 403
        )
        assert c.get("/api/sessions").json() == []
        assert c.post("/api/mcp/token/reveal", headers=headers).status_code == 403
        headers["x-csrf-token"] = paired.json()["csrf_token"]
        assert c.post("/api/auth/pair", json={"code": code}, headers=headers).status_code == 401
        assert c.post("/api/mcp/token/reveal", headers=headers).json() == {"token": "fixed"}
        assert c.post("/api/mcp/token/reveal", headers=headers).status_code == 409
        new = c.post("/api/mcp/token/rotate", headers=headers).json()["token"]
        assert new != "fixed" and app.state.services.auth.tokens.verify(new)
        assert not app.state.services.auth.tokens.verify("fixed")
        assert c.get("/mcp", headers={"authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/healthz", headers={"host": "evil.test:8000"}).status_code == 421
        assert c.get("/healthz", headers={"host": "["}).status_code == 421
        secret = "not-for-error-body"
        invalid = c.post("/api/sessions", json={"password": secret}, headers=headers)
        assert invalid.status_code == 422 and secret not in invalid.text
        assert (
            c.post("/api/sessions", content="x" * (256 * 1024 + 1), headers=headers).status_code
            == 413
        )
        assert c.get("/api/missing").status_code == 404
        assert c.get("/mcp/missing", headers={"authorization": "Bearer " + new}).status_code == 404


def test_restart_revokes_web_cookie(tmp_path: Path) -> None:
    first = create_app(Settings(known_hosts=tmp_path / "known_hosts"))
    with TestClient(first, base_url="http://127.0.0.1:8000") as c:
        c.post(
            "/api/auth/pair",
            json={"code": first.state.services.auth.pairing.code},
            headers={"origin": "http://127.0.0.1:8000"},
        )
        cookie = c.cookies.get("ssh_mcp_session")
    restarted = create_app(Settings(known_hosts=tmp_path / "known_hosts"))
    with TestClient(restarted, base_url="http://127.0.0.1:8000") as c:
        c.cookies.set("ssh_mcp_session", cookie)
        assert c.get("/api/sessions").status_code == 401
