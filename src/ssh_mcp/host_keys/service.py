import asyncio
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from ssh_mcp.errors import ToolError


@dataclass
class Probe:
    host: str
    port: int
    key: asyncssh.SSHKey
    expires: float


class HostKeyTrust:
    """Persist only confirmed public keys in interoperable OpenSSH format."""

    def __init__(self, path: Path, timeout: int = 15) -> None:
        self.path = path
        self.timeout = timeout
        self.probes: dict[str, Probe] = {}

    @staticmethod
    def name(host: str, port: int) -> str:
        return host if port == 22 else f"[{host}]:{port}"

    def key_for(self, host: str, port: int) -> asyncssh.SSHKey | None:
        if not self.path.exists():
            return None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.startswith(self.name(host, port) + " "):
                return asyncssh.import_public_key(line.split(" ", 1)[1])
        return None

    async def probe(self, host: str, port: int) -> dict[str, object]:
        try:
            async with asyncio.timeout(self.timeout):
                key = await asyncssh.get_server_host_key(
                    host, port, config=None, proxy_command=None
                )
        except TimeoutError as exc:
            raise ToolError("SSH_TIMEOUT", "探测主机公钥超时", 504) from exc
        except (OSError, asyncssh.Error) as exc:
            raise ToolError("SSH_FAILURE", "无法探测 SSH 主机公钥", 502) from exc
        if key is None:
            raise ToolError("HOST_KEY_UNAVAILABLE", "服务器未提供可验证公钥")
        stored = self.key_for(host, port)
        if stored is not None and stored != key:
            raise ToolError(
                "HOST_KEY_CHANGED", "主机公钥已变化；连接被拒绝，请核实后手动管理 known_hosts", 409
            )
        self.probes = {k: p for k, p in self.probes.items() if p.expires > time.monotonic()}
        if len(self.probes) >= 64:
            raise ToolError("PROBE_LIMIT", "待确认的主机过多，请稍后重试", 429)
        probe_id = secrets.token_urlsafe(24)
        self.probes[probe_id] = Probe(host, port, key, time.monotonic() + 120)
        return {
            "probe_id": probe_id,
            "host": host,
            "port": port,
            "algorithm": key.get_algorithm(),
            "fingerprint": key.get_fingerprint("sha256"),
            "trusted": stored is not None,
        }

    def confirm(self, probe_id: str) -> None:
        p = self.probes.get(probe_id)
        if p is None or p.expires < time.monotonic():
            raise ToolError("PROBE_EXPIRED", "公钥探测已过期，请重新探测")
        old = self.key_for(p.host, p.port)
        if old is not None and old != p.key:
            raise ToolError("HOST_KEY_CHANGED", "主机公钥已变化", 409)
        if old is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(
                    self.name(p.host, p.port)
                    + " "
                    + p.key.export_public_key().decode().strip()
                    + "\n"
                )
            self.path.chmod(0o600)

    def consume(self, probe_id: str, host: str, port: int) -> asyncssh.SSHKey:
        p = self.probes.pop(probe_id, None)
        if p is None or p.expires < time.monotonic() or (p.host, p.port) != (host, port):
            raise ToolError("PROBE_EXPIRED", "请重新探测并确认主机公钥")
        if self.key_for(host, port) != p.key:
            raise ToolError("HOST_KEY_CONFIRMATION_REQUIRED", "请显式确认主机公钥")
        return p.key
