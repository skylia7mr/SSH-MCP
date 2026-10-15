import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ssh_mcp.errors import ToolError
from ssh_mcp.schemas import (
    ConnectRequest,
    EditConnectionRequest,
    OutputView,
    ReconnectRequest,
    RunView,
    SessionView,
    SudoRequest,
    TransferView,
)
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

    @api.post("/sessions/{session_id}/reconnect")
    async def reconnect(session_id: str, body: ReconnectRequest) -> SessionView:
        return await s.registry.reconnect(session_id, body)

    @api.put("/sessions/{session_id}")
    async def edit(session_id: str, body: EditConnectionRequest) -> SessionView:
        updated = await s.registry.edit(session_id, body)
        await s.cancel_session_work(session_id)
        return updated

    @api.get("/sessions/{session_id}")
    async def session(session_id: str) -> SessionView:
        return s.registry.get(session_id).view()

    @api.post("/sessions/{session_id}/disconnect")
    async def disconnect(session_id: str) -> SessionView:
        result = await s.registry.disconnect(session_id)
        await s.cancel_session_work(session_id)
        return result

    @api.delete("/sessions/{session_id}")
    async def forget(session_id: str) -> dict[str, bool]:
        await s.registry.forget(session_id)
        await s.cancel_session_work(session_id)
        return {"removed": True}

    @api.put("/sessions/{session_id}/sudo")
    async def policy(session_id: str, body: SudoRequest) -> SessionView:
        return await s.registry.sudo_policy(session_id, body.enabled)

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
            "endpoint": f"http://127.0.0.1:{s.settings.port}/mcp",
            "token_revealed": s.auth.token_revealed,
            "token_hint": s.auth.tokens.token_hint,
            "configured_from_environment": bool(s.settings.mcp_token),
        }

    @api.get("/runs")
    async def runs(
        session_id: str, limit: int = Query(default=100, ge=1, le=1000)
    ) -> list[RunView]:
        s.tasks.cleanup()
        views = sorted(
            (r for r in s.tasks.runs.values() if r.request.session_id == session_id),
            key=lambda r: r.created_at,
            reverse=True,
        )[:limit]
        return [
            r.view().model_copy(
                update={
                    "command": r.request.command[:512],
                    "command_truncated": len(r.request.command) > 512,
                }
            )
            for r in views
        ]

    @api.get("/runs/{run_id}")
    async def run(run_id: str) -> RunView:
        return s.tasks.get(run_id).view()

    @api.get("/transfers")
    async def transfers(
        session_id: str, limit: int = Query(default=100, ge=1, le=1000)
    ) -> list[TransferView]:
        s.transfers.cleanup()
        return sorted(
            (
                record
                for record in s.transfers.records.values()
                if session_id in (record.source_session_id, record.target_session_id)
            ),
            key=lambda record: record.created_at,
            reverse=True,
        )[:limit]

    @api.get("/runs/{run_id}/output")
    async def output(
        run_id: str, stdout_cursor: int = 0, stderr_cursor: int = 0, limit: int = 65536
    ) -> OutputView:
        return s.tasks.read_output(run_id, stdout_cursor, stderr_cursor, limit)

    @api.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> RunView:
        return await s.tasks.cancel(run_id)

    @api.get("/events")
    async def events(request: Request) -> StreamingResponse:
        cookie = request.cookies.get("ssh_mcp_session")

        async def refresh() -> AsyncIterator[str]:
            while s.auth.valid_cookie(cookie) and not s.tasks.closing:
                if await request.is_disconnected():
                    break
                yield "event: refresh\ndata: {}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            refresh(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return api
