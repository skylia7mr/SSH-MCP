from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ssh_mcp.api.router import create_router
from ssh_mcp.auth.middleware import LocalSecurity
from ssh_mcp.config import Settings, get_settings
from ssh_mcp.errors import ToolError
from ssh_mcp.mcp_server.server import build_mcp
from ssh_mcp.services import Services


def create_app(settings: Settings | None = None) -> FastAPI:
    s = Services(settings or get_settings())
    mcp = build_mcp(s)
    mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        print(
            f"SSH MCP 本机一次性配对码：{s.auth.pairing.code}（{s.settings.pairing_ttl_seconds} 秒有效）",
            flush=True,
        )
        async with mcp.session_manager.run():
            s.tasks.start()
            try:
                yield
            finally:
                await s.transfers.close()
                await s.tasks.close()
                await s.registry.close()
                s.auth.browser = None

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = s
    origins = [f"http://{h}:{s.settings.port}" for h in ("localhost", "127.0.0.1", "[::1]")]
    origins += ["http://localhost:5173", "http://127.0.0.1:5173"]
    app.add_middleware(
        LocalSecurity,
        auth=s.auth,
        origins=origins,
        body_limit=256 * 1024,
    )

    @app.exception_handler(ToolError)
    async def tool_error(request: Request, exc: ToolError) -> JSONResponse:
        return JSONResponse(
            {"error_code": exc.code, "error": exc.message}, status_code=exc.http_status
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error_code": "INVALID_ARGUMENT",
                "error": "输入校验失败，请检查字段",
                "fields": [list(e["loc"]) for e in exc.errors()],
            },
            status_code=422,
        )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(create_router(s))
    app.router.routes.extend(mcp_app.routes)
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", response_model=None)
    async def spa(path: str) -> FileResponse | JSONResponse:
        if path in {"api", "mcp"} or path.startswith(("api/", "mcp/")):
            return JSONResponse({"error": "未知路径"}, status_code=404)
        if not (dist / "index.html").exists():
            return JSONResponse(
                {"error": "网页未构建，请运行 cd frontend && npm ci && npm run build"},
                status_code=503,
            )
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})

    return app
