from pathlib import Path

from pydantic import SecretStr

from ssh_mcp.config import Settings
from ssh_mcp.errors import ToolError
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.schemas import ConnectRequest
from ssh_mcp.ssh.registry import SessionRegistry


async def test_password_encrypted_key_and_no_secret_metadata(real_sshd, tmp_path: Path) -> None:
    trust = HostKeyTrust(tmp_path / "known_hosts")
    settings = Settings(known_hosts=trust.path)
    registry = SessionRegistry(settings, trust)

    async def connect(
        username: str, *, key: bool = False, wrong: bool = False, bad_passphrase: bool = False
    ):
        probe = await trust.probe("127.0.0.1", real_sshd.port)
        trust.confirm(str(probe["probe_id"]))
        request = ConnectRequest(
            alias=username,
            host="127.0.0.1",
            port=real_sshd.port,
            username=username,
            probe_id=str(probe["probe_id"]),
            password=None if key else SecretStr("wrong" if wrong else real_sshd.password),
            private_key=SecretStr(real_sshd.private_key) if key else None,
            passphrase=SecretStr("wrong-passphrase" if bad_passphrase else real_sshd.passphrase)
            if key
            else None,
        )
        view = await registry.connect(request)
        assert request.password is request.private_key is request.passphrase is None
        return view

    try:
        alice = await connect("alice")
        bob = await connect("bob")
        assert alice.status == bob.status == "connected"
        assert alice.session_id != bob.session_id
        for view, expected in [(alice, "alice"), (bob, "bob")]:
            result = (
                await registry.get(view.session_id).require_connection().run("id -un", check=True)
            )
            assert result.stdout == expected + "\n"
        encrypted = await connect("alice", key=True)
        assert encrypted.status == "connected"
        bad_key = await connect("alice", key=True, bad_passphrase=True)
        assert bad_key.status == "failed" and bad_key.error == "INVALID_PRIVATE_KEY"
        failed = await connect("alice", wrong=True)
        assert failed.status == "failed" and failed.error == "SSH_AUTHENTICATION_FAILED"
        metadata = str(registry.list())
        assert real_sshd.password not in metadata and real_sshd.private_key not in metadata
        assert not alice.sudo_enabled and not bob.sudo_enabled
        await registry.sudo_policy(alice.session_id, True)
        assert registry.get(alice.session_id).view().sudo_password_configured
        assert registry.get(alice.session_id).sudo_password == real_sshd.password
        assert not registry.get(bob.session_id).sudo_enabled
        await registry.sudo_policy(alice.session_id, False)
        assert registry.get(alice.session_id).sudo_password is None
        await registry.sudo_policy(alice.session_id, True)
        assert registry.get(alice.session_id).sudo_password == real_sshd.password
        await registry.disconnect(bob.session_id)
        assert registry.get(bob.session_id).status == "disconnected"
        assert registry.get(bob.session_id).error is None
        try:
            registry.get(bob.session_id).require_connection()
        except ToolError as e:
            assert e.code == "SESSION_UNAVAILABLE"
        else:
            raise AssertionError("disconnected session allowed execution")
    finally:
        await registry.close()
    assert registry.sessions == {}
