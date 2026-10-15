import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncssh
from pydantic import SecretStr

from ssh_mcp.config import Settings
from ssh_mcp.errors import ToolError
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.schemas import (
    ConnectRequest,
    EditConnectionRequest,
    ReconnectRequest,
    SessionStatus,
    SessionView,
)
from ssh_mcp.ssh.credentials import OneShotCredentials
from ssh_mcp.ssh.profiles import ConnectionProfile, ProfileStore


def now() -> datetime:
    return datetime.now(UTC)


def connection_id(alias: str) -> str:
    material = f"{alias}\x00{time.time_ns()}\x00{secrets.token_hex(16)}"
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass
class Session:
    session_id: str
    alias: str
    host: str
    port: int
    username: str
    profile: ConnectionProfile | None = field(default=None, repr=False)
    created_at: datetime = field(default_factory=now)
    connection: asyncssh.SSHClientConnection | None = None
    last_activity_at: datetime = field(default_factory=now)
    status: SessionStatus = SessionStatus.connecting
    error: str | None = None
    sudo_enabled: bool = False
    sudo_password: str | None = field(default=None, repr=False)
    login_password: str | None = field(default=None, repr=False)
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
            auth_type=self.profile.auth_type if self.profile else "password",
            private_key_saved=bool(self.profile and self.profile.private_key),
            account_password_configured=self.login_password is not None,
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
        self.store = ProfileStore(
            settings.connections_file or settings.known_hosts.parent / "connections.json"
        )
        for profile in self.store.load():
            self.sessions[profile.session_id] = Session(
                profile.session_id,
                profile.alias,
                profile.host,
                profile.port,
                profile.username,
                profile=profile,
                created_at=profile.created_at,
                status=SessionStatus.disconnected,
            )

    def _save(self) -> None:
        self.store.save([s.profile for s in self.sessions.values() if s.profile])

    async def connect(self, request: ConnectRequest) -> SessionView:
        if len(self.sessions) >= self.settings.max_sessions:
            raise ToolError("SESSION_LIMIT", "连接总数已达上限，请先移除连接", 429)
        session = Session(
            connection_id(request.alias),
            request.alias,
            request.host,
            request.port,
            request.username,
        )
        session.profile = ConnectionProfile(
            session_id=session.session_id,
            alias=session.alias,
            host=session.host,
            port=session.port,
            username=session.username,
            created_at=session.created_at,
            auth_type="key" if request.private_key else "password",
            private_key=request.private_key,
        )
        self.sessions[session.session_id] = session
        try:
            self._save()
        except ToolError:
            self.sessions.pop(session.session_id)
            raise
        return await self._connect(session, request, session.session_id)

    async def edit(self, session_id: str, request: EditConnectionRequest) -> SessionView:
        session = self.get(session_id)
        try:
            async with session.lock:
                self.get(session_id)
                previous = session.profile
                key = request.private_key
                if request.auth_type == "key" and not key and previous:
                    key = previous.private_key
                if request.auth_type == "key" and not key:
                    raise ToolError("PRIVATE_KEY_REQUIRED", "密钥认证需要提供私钥")
                if request.auth_type == "password":
                    key = None
                identity_changed = (
                    (session.host, session.port, session.username)
                    != (request.host, request.port, request.username)
                    or (previous is not None and previous.auth_type != request.auth_type)
                    or key != (previous.private_key if previous else None)
                )
                profile = ConnectionProfile(
                    session_id=connection_id(request.alias),
                    alias=request.alias,
                    host=request.host,
                    port=request.port,
                    username=request.username,
                    created_at=session.created_at,
                    auth_type=request.auth_type,
                    private_key=key,
                )
                self.store.save(
                    [
                        profile if existing is session else existing.profile
                        for existing in self.sessions.values()
                        if existing.profile
                    ]
                )
                self.sessions.pop(session_id)
                session.session_id, session.alias = profile.session_id, profile.alias
                session.host, session.port, session.username = (
                    profile.host,
                    profile.port,
                    profile.username,
                )
                session.profile = profile
                self.sessions[profile.session_id] = session
                session.sudo_version += 1
                session.error = None
                if identity_changed:
                    session.disconnect_requested = True
                    session.status = SessionStatus.disconnected
                    session.sudo_enabled = False
                    session.sudo_password = session.login_password = None
                    if session.connection:
                        session.connection.close()
                    session.connection = None
                if request.clear_password:
                    session.login_password = None
                elif request.password is not None:
                    session.login_password = request.password.get_secret_value()
                session.sudo_password = session.login_password if session.sudo_enabled else None
                return session.view()
        finally:
            request.password = request.passphrase = request.private_key = None

    async def reconnect(self, session_id: str, request: ReconnectRequest) -> SessionView:
        session = self.get(session_id)
        try:
            body = ConnectRequest(
                alias=session.alias,
                host=session.host,
                port=session.port,
                username=session.username,
                probe_id=request.probe_id,
                password=request.password
                or (SecretStr(session.login_password) if session.login_password else None),
                private_key=session.profile.private_key if session.profile else None,
                passphrase=request.passphrase,
            )
            return await self._connect(session, body, session_id)
        finally:
            request.password = request.passphrase = None

    async def _connect(
        self, session: Session, request: ConnectRequest, expected_id: str
    ) -> SessionView:
        key = self.trust.consume(request.probe_id, request.host, request.port)
        credentials = OneShotCredentials(
            request.password.get_secret_value() if request.password else None, None
        )
        await session.lock.acquire()
        try:
            self.get(expected_id)
            if session.connection and not session.connection.is_closed():
                raise ToolError("SESSION_ALREADY_CONNECTED", "连接已建立，请先断开", 409)
            session.status = SessionStatus.connecting
            session.error = None
            session.disconnect_requested = False
            session.sudo_enabled = False
            session.sudo_password = session.login_password = None
            session.sudo_version += 1
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
            session.login_password = (
                request.password.get_secret_value() if request.password else None
            )
            session.monitor = asyncio.create_task(self._monitor(session, session.connection))
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

    async def _monitor(self, session: Session, connection: asyncssh.SSHClientConnection) -> None:
        if connection:
            await connection.wait_closed()
            async with session.lock:
                if session.connection is not connection:
                    return
                session.status = SessionStatus.disconnected
                session.sudo_enabled = False
                session.sudo_password = None
                session.login_password = None
                session.sudo_version += 1
                if not session.disconnect_requested:
                    session.error = session.error or "SSH_DISCONNECTED"

    def get(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise ToolError(
                "UNKNOWN_SESSION", "连接已删除或编辑，旧 ID 已失效；请停止当前操作并联系操作者", 404
            )
        return session

    def list(self) -> list[SessionView]:
        return [s.view() for s in self.sessions.values()]

    async def sudo_policy(self, session_id: str, enabled: bool) -> SessionView:
        s = self.get(session_id)
        async with s.lock:
            self.get(session_id)
            s.require_connection()
            s.sudo_enabled = enabled
            s.sudo_password = s.login_password if enabled else None
            s.sudo_version += 1
            return s.view()

    async def disconnect(self, session_id: str) -> SessionView:
        s = self.get(session_id)
        async with s.lock:
            self.get(session_id)
            s.disconnect_requested = True
            s.status = SessionStatus.disconnected
            s.sudo_password = None
            s.login_password = None
            s.sudo_enabled = False
            s.sudo_version += 1
            connection, monitor = s.connection, s.monitor
            if connection:
                connection.close()
        if connection:
            await connection.wait_closed()
        if monitor:
            await monitor
        return s.view()

    async def forget(self, session_id: str) -> None:
        session = self.get(session_id)
        async with session.lock:
            self.get(session_id)
            self.store.save(
                [
                    existing.profile
                    for existing in self.sessions.values()
                    if existing is not session and existing.profile
                ]
            )
            self.sessions.pop(session_id)
            session.disconnect_requested = True
            session.status = SessionStatus.disconnected
            session.login_password = session.sudo_password = None
            session.sudo_enabled = False
            session.sudo_version += 1
            connection, monitor = session.connection, session.monitor
            if connection:
                connection.close()
        if connection:
            await connection.wait_closed()
        if monitor:
            await monitor

    async def close(self) -> None:
        for session_id in list(self.sessions):
            await self.disconnect(session_id)
        self.sessions.clear()
