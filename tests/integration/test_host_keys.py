import asyncio
from pathlib import Path

import asyncssh
import pytest
from pydantic import SecretStr

from ssh_mcp.config import Settings
from ssh_mcp.errors import ToolError
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.schemas import ConnectRequest
from ssh_mcp.ssh.registry import SessionRegistry


class NoAuthServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False


async def test_unknown_key_confirmation_and_change_between_probe_auth(tmp_path: Path) -> None:
    old_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=NoAuthServer, server_host_keys=[old_key]
    )
    port = server.get_port()
    trust = HostKeyTrust(tmp_path / "known_hosts")
    registry = SessionRegistry(Settings(known_hosts=trust.path), trust)
    try:
        probe = await trust.probe("127.0.0.1", port)
        assert not probe["trusted"] and str(probe["fingerprint"]).startswith("SHA256:")
        with pytest.raises(ToolError) as e:
            trust.consume(str(probe["probe_id"]), "127.0.0.1", port)
        assert e.value.code == "HOST_KEY_CONFIRMATION_REQUIRED"
        probe = await trust.probe("127.0.0.1", port)
        trust.confirm(str(probe["probe_id"]))
        assert trust.key_for("127.0.0.1", port) == old_key.convert_to_public()
        server.close()
        await server.wait_closed()
        server = await asyncssh.listen(
            "127.0.0.1",
            port,
            server_factory=NoAuthServer,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        )
        changed = await registry.connect(
            ConnectRequest(
                alias="changed",
                host="127.0.0.1",
                port=port,
                username="user",
                password=SecretStr("never-send"),
                probe_id=str(probe["probe_id"]),
            )
        )
        assert changed.status == "failed" and changed.error == "HOST_KEY_CHANGED"
        with pytest.raises(ToolError) as e:
            await trust.probe("127.0.0.1", port)
        assert e.value.code == "HOST_KEY_CHANGED"
    finally:
        server.close()
        await server.wait_closed()
        await registry.close()


async def test_probe_timeout(tmp_path: Path) -> None:
    clients = []

    async def silent(reader, writer):
        clients.append(writer)
        await reader.read()
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    try:
        trust = HostKeyTrust(tmp_path / "known_hosts", timeout=1)
        with pytest.raises(ToolError) as e:
            await trust.probe("127.0.0.1", server.sockets[0].getsockname()[1])
        assert e.value.code == "SSH_TIMEOUT"
    finally:
        for writer in clients:
            writer.close()
        server.close()
        await server.wait_closed()
