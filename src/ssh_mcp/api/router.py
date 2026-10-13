from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator

from ssh_mcp.errors import ToolError
from ssh_mcp.schemas import ConnectRequest, SessionView, SudoPasswordRequest, SudoRequest
from ssh_mcp.services import Services


class PairRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class ProbeRequest(BaseModel):
    host: Annotated[str, Field(min_length=1, max_length=255)]
    port: int = Field(default=22, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        return ConnectRequest.valid_host(value)


class ConfirmRequest(BaseModel):
    probe_id: str = Field(min_length=1, max_length=128)


def create_router(s: Services) -> APIRouter:
    api = APIRouter(prefix="/api")

    @api.post("/auth/pair")
    async def pair(body: PairRequest, response: Response) -> dict[str, str]:
        browser = s.auth.pair(body.code)
        if browser is None:
            raise ToolError(
                "PAIRING_FAILED", "配对码错误、已使用或过期；请检查启动终端，必要时重启", 401
            )
        response.set_cookie(
            "ssh_mcp_session", browser.cookie, httponly=True, samesite="strict", path="/"
        )
        return {"csrf_token": browser.csrf}

    @api.get("/auth/session")
    async def auth_session() -> dict[str, str | bool]:
        assert s.auth.browser is not None
        return {"csrf_token": s.auth.browser.csrf, "authenticated": True}

    @api.get("/sessions")
    async def sessions() -> list[SessionView]:
        return s.registry.list()

    @api.post("/host-keys/probe")
    async def probe(body: ProbeRequest) -> dict[str, object]:
        return await s.trust.probe(body.host, body.port)

    @api.post("/host-keys/confirm")
    async def confirm(body: ConfirmRequest) -> dict[str, bool]:
        s.trust.confirm(body.probe_id)
        return {"confirmed": True}

    @api.post("/sessions")
    async def connect(body: ConnectRequest) -> SessionView:
        return await s.registry.connect(body)

    @api.get("/sessions/{session_id}")
    async def session(session_id: str) -> SessionView:
        return s.registry.get(session_id).view()

    @api.post("/sessions/{session_id}/disconnect")
    async def disconnect(session_id: str) -> SessionView:
        return await s.registry.disconnect(session_id)

    @api.delete("/sessions/{session_id}")
    async def forget(session_id: str) -> dict[str, bool]:
        await s.registry.forget(session_id)
        return {"removed": True}

    @api.put("/sessions/{session_id}/sudo")
    async def policy(session_id: str, body: SudoRequest) -> SessionView:
        return await s.registry.sudo_policy(session_id, body.enabled)

    @api.put("/sessions/{session_id}/sudo/password")
    async def password(session_id: str, body: SudoPasswordRequest) -> SessionView:
        value = body.password.get_secret_value()
        return await s.registry.sudo_password(session_id, value)

    @api.delete("/sessions/{session_id}/sudo/password")
    async def clear_password(session_id: str) -> SessionView:
        return await s.registry.sudo_password(session_id, None)

    @api.post("/mcp/token/reveal")
    async def reveal() -> dict[str, str]:
        if s.auth.token_revealed:
            raise ToolError("TOKEN_ALREADY_REVEALED", "旧 token 不会回填；请轮换生成新 token", 409)
        s.auth.token_revealed = True
        return {"token": s.auth.tokens.current()}

    @api.post("/mcp/token/rotate")
    async def rotate() -> dict[str, str]:
        s.auth.token_revealed = True
        return {"token": s.auth.tokens.rotate()}

    @api.get("/mcp/info")
    async def mcp_info(request: Request) -> dict[str, str | bool]:
        return {
            "endpoint": str(request.base_url).rstrip("/") + "/mcp",
            "token_revealed": s.auth.token_revealed,
            "token_hint": s.auth.tokens.token_hint,
            "configured_from_environment": bool(s.settings.mcp_token),
        }

    return api
