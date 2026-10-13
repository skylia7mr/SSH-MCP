import asyncio
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ssh_mcp.auth.web import AuthState


class LocalSecurity:
    def __init__(self, app: ASGIApp, auth: AuthState, origins: list[str], body_limit: int) -> None:
        self.app = app
        self.auth = auth
        self.origins = set(origins)
        self.body_limit = body_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        original_send = send

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start" and (
                path.startswith(("/api/", "/mcp/")) or path == "/mcp"
            ):
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await original_send(message)

        send = private_send
        body = bytearray()

        async def deny(code: str, message: str, status: int) -> None:
            await JSONResponse({"error_code": code, "error": message}, status_code=status)(
                scope, receive, send
            )

        host = request.headers.get("host", "")
        try:
            parsed_host = urlsplit("//" + host)
            hostname, port = parsed_host.hostname, parsed_host.port
        except ValueError:
            await deny("INVALID_HOST", "Host 无效", 421)
            return
        if (
            hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed_host.username is not None
            or not port
        ):
            await deny("INVALID_HOST", "仅允许本机 Host", 421)
            return
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.origins:
            await deny("INVALID_ORIGIN", "跨站来源被拒绝", 403)
            return
        mcp_request = path == "/mcp" or path.startswith("/mcp/")
        if mcp_request:
            header = request.headers.get("authorization", "")
            candidate = header[7:] if header.startswith("Bearer ") else None
            if not self.auth.tokens.verify(candidate):
                await deny("UNAUTHORIZED", "需要有效 MCP Bearer token", 401)
                return
        elif path.startswith("/api/") and path != "/api/auth/pair":
            if not self.auth.valid_cookie(request.cookies.get("ssh_mcp_session")):
                await deny("UNAUTHORIZED", "请使用本次启动的配对码重新配对", 401)
                return
            if request.method not in {"GET", "HEAD", "OPTIONS"} and (
                origin is None or not self.auth.valid_csrf(request.headers.get("x-csrf-token"))
            ):
                await deny("CSRF_REJECTED", "会话校验失败，请刷新页面", 403)
                return
        elif path == "/api/auth/pair" and origin is None:
            await deny("CSRF_REJECTED", "配对请求必须来自本机工作台", 403)
            return
        # Fully bounded body before framework validation; errors omit submitted input.
        next_receive: Receive
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            raw_length = request.headers.get("content-length", "0")
            if not raw_length.isdecimal() or int(raw_length) > self.body_limit:
                await deny("REQUEST_TOO_LARGE", "请求体超过限制", 413)
                return
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > self.body_limit:
                            await deny("REQUEST_TOO_LARGE", "请求体超过限制", 413)
                            return
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await deny("REQUEST_TIMEOUT", "请求体读取超时", 408)
                return
            consumed = False

            async def buffered_receive() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            next_receive = buffered_receive
        else:
            next_receive = receive
        if not mcp_request:
            try:
                await self.app(scope, next_receive, send)
            finally:
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    body.clear()
            return

        if not self.auth.tokens.verify(candidate):
            await deny("TOKEN_REVOKED", "MCP token 已轮换，请重新接入", 401)
            body.clear()
            return

        started = False
        finished = False

        async def track_send(message: Message) -> None:
            nonlocal started, finished
            if not self.auth.tokens.verify(candidate):
                return
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
            await send(message)

        async def dispatch() -> None:
            await self.app(scope, next_receive, track_send)

        handler = asyncio.create_task(dispatch())
        revoked = asyncio.create_task(self.auth.tokens.revoked.wait())
        try:
            completed, _ = await asyncio.wait(
                {handler, revoked}, return_when=asyncio.FIRST_COMPLETED
            )
            if revoked in completed or not self.auth.tokens.verify(candidate):
                handler.cancel()
                await asyncio.gather(handler, return_exceptions=True)
                if not started:
                    await deny("TOKEN_REVOKED", "MCP token 已轮换，请重新接入", 401)
                elif not finished:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await handler
        finally:
            handler.cancel()
            revoked.cancel()
            await asyncio.gather(handler, revoked, return_exceptions=True)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                body.clear()
