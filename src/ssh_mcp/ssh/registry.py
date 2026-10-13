import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncssh

from ssh_mcp.config import Settings
from ssh_mcp.errors import ToolError
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.schemas import ConnectRequest, SessionStatus, SessionView
from ssh_mcp.ssh.credentials import OneShotCredentials


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Session:
    session_id: str
    alias: str
    host: str
    port: int
    username: str
    created_at: datetime = field(default_factory=now)
    connection: asyncssh.SSHClientConnection | None = None
    last_activity_at: datetime = field(default_factory=now)
    status: SessionStatus = SessionStatus.connecting
    error: str | None = None
    sudo_enabled: bool = False
    sudo_password: str | None = field(default=None, repr=False)
    sudo_version: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    monitor: asyncio.Task[None] | None = None
    disconnect_requested: bool = False

    def view(self) -> SessionView:
        return SessionView(
            session_id=self.session_id,
            alias=self.alias,
            host=self.host,
            port=self.port,
            username=self.username,
            status=self.status,
            sudo_enabled=self.sudo_enabled,
            sudo_password_configured=self.sudo_password is not None,
            policy_version=self.sudo_version,
            created_at=self.created_at,
            last_activity_at=self.last_activity_at,
            error=self.error,
        )

    def require_connection(self) -> asyncssh.SSHClientConnection:
        if (
            self.status != SessionStatus.connected
            or self.connection is None
            or self.connection.is_closed()
        ):
            raise ToolError("SESSION_UNAVAILABLE", "SSH 连接已不可用，请重新登录", 409)
        return self.connection


class SessionRegistry:
    def __init__(self, settings: Settings, trust: HostKeyTrust) -> None:
        self.settings = settings
        self.trust = trust
        self.sessions: dict[str, Session] = {}

    async def connect(self, request: ConnectRequest) -> SessionView:
        if len(self.sessions) >= self.settings.max_sessions:
            raise ToolError("SESSION_LIMIT", "连接总数已达上限，请先移除连接", 429)
        key = self.trust.consume(request.probe_id, request.host, request.port)
        session = Session(
            secrets.token_urlsafe(18), request.alias, request.host, request.port, request.username
        )
        self.sessions[session.session_id] = session
        credentials = OneShotCredentials(
            request.password.get_secret_value() if request.password else None, None
        )
        await session.lock.acquire()
        try:
            if request.private_key:
                credentials.key = asyncssh.import_private_key(
                    request.private_key.get_secret_value(),
                    passphrase=request.passphrase.get_secret_value()
                    if request.passphrase
                    else None,
                )
            async with asyncio.timeout(self.settings.ssh_connect_timeout_seconds):
                session.connection = await asyncssh.connect(
                    request.host,
                    port=request.port,
                    username=request.username,
                    client_factory=lambda: credentials,
                    password=None,
                    client_keys=None,
                    known_hosts=([key], [], []),
                    config=None,
                    agent_path=None,
                    agent_forwarding=False,
                    proxy_command=None,
                    tunnel=None,
                    preferred_auth=["publickey"] if credentials.key else ["password"],
                    kbdint_auth=False,
                    host_based_auth=False,
                    keepalive_interval=self.settings.ssh_keepalive_interval,
                    keepalive_count_max=3,
                )
            session.status = SessionStatus.connected
            session.monitor = asyncio.create_task(self._monitor(session))
        except asyncssh.PermissionDenied:
            session.status, session.error = SessionStatus.failed, "SSH_AUTHENTICATION_FAILED"
        except asyncssh.HostKeyNotVerifiable:
            session.status, session.error = SessionStatus.failed, "HOST_KEY_CHANGED"
        except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError, ValueError):
            session.status, session.error = SessionStatus.failed, "INVALID_PRIVATE_KEY"
        except TimeoutError:
            session.status, session.error = SessionStatus.failed, "SSH_TIMEOUT"
        except (OSError, asyncssh.Error):
            session.status, session.error = SessionStatus.failed, "SSH_FAILURE"
        except asyncio.CancelledError:
            session.status, session.error = SessionStatus.failed, "SSH_CONNECT_CANCELLED"
            if session.connection:
                session.connection.close()
                await session.connection.wait_closed()
            raise
        finally:
            credentials.clear()
            request.password = request.private_key = request.passphrase = None
            session.lock.release()
        return session.view()

    async def _monitor(self, session: Session) -> None:
        if session.connection:
            await session.connection.wait_closed()
            async with session.lock:
                session.status = SessionStatus.disconnected
                session.sudo_enabled = False
                session.sudo_password = None
                session.sudo_version += 1
                if not session.disconnect_requested:
                    session.error = session.error or "SSH_DISCONNECTED"

    def get(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise ToolError("UNKNOWN_SESSION", "连接不存在", 404)
        return session

    def list(self) -> list[SessionView]:
        return [s.view() for s in self.sessions.values()]

    async def sudo_policy(self, session_id: str, enabled: bool) -> SessionView:
        s = self.get(session_id)
        async with s.lock:
            s.require_connection()
            s.sudo_enabled = enabled
            if not enabled:
                s.sudo_password = None
            s.sudo_version += 1
            return s.view()

    async def sudo_password(self, session_id: str, password: str | None) -> SessionView:
        s = self.get(session_id)
        async with s.lock:
            s.require_connection()
            if password is not None and not s.sudo_enabled:
                raise ToolError("SUDO_NOT_AUTHORIZED", "请先开启此连接的 sudo")
            s.sudo_password = password
            s.sudo_version += 1
            return s.view()

    async def disconnect(self, session_id: str) -> SessionView:
        s = self.get(session_id)
        async with s.lock:
            s.disconnect_requested = True
            s.status = SessionStatus.disconnected
            s.sudo_password = None
            s.sudo_enabled = False
            s.sudo_version += 1
            if s.connection:
                s.connection.close()
        if s.connection:
            await s.connection.wait_closed()
        if s.monitor:
            await s.monitor
        return s.view()

    async def forget(self, session_id: str) -> None:
        await self.disconnect(session_id)
        self.sessions.pop(session_id, None)

    async def close(self) -> None:
        for session_id in list(self.sessions):
            await self.disconnect(session_id)
        self.sessions.clear()
