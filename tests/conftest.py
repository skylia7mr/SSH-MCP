import asyncio
import os
import secrets
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass

import asyncssh
import pytest


@dataclass(repr=False)
class SSHFixture:
    container: str
    port: int
    password: str
    private_key: str
    passphrase: str


@pytest.fixture(scope="session")
def real_sshd() -> Iterator[SSHFixture]:
    """Opt-in real Docker fixture. Never contacts a user/production SSH server."""
    if os.environ.get("SSH_MCP_REAL_TESTS") != "1":
        pytest.skip("set SSH_MCP_REAL_TESTS=1 after building tests/sshd image")
    password = "sentinel-" + secrets.token_hex(20)
    container = (
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-p",
                "127.0.0.1::22",
                "-e",
                "TEST_PASSWORD",
                "ssh-mcp-test-sshd:local",
            ],
            check=True,
            capture_output=True,
            env={**os.environ, "TEST_PASSWORD": password},
        )
        .stdout.decode()
        .strip()
    )
    try:
        mapping = (
            subprocess.run(["docker", "port", container, "22"], check=True, capture_output=True)
            .stdout.decode()
            .strip()
        )
        port = int(mapping.rsplit(":", 1)[1])
        key = asyncssh.generate_private_key("ssh-ed25519")
        passphrase = secrets.token_urlsafe(20)
        public_key = key.export_public_key()
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "sh",
                "-c",
                (
                    "mkdir -p /home/alice/.ssh; cat > /home/alice/.ssh/authorized_keys; "
                    "chown -R alice:alice /home/alice/.ssh; chmod 700 /home/alice/.ssh; "
                    "chmod 600 /home/alice/.ssh/authorized_keys"
                ),
            ],
            input=public_key,
            check=True,
            capture_output=True,
        )

        async def ready() -> None:
            conn = await asyncssh.connect(
                "127.0.0.1",
                port=port,
                username="alice",
                password=password,
                known_hosts=None,
                client_keys=None,
                agent_path=None,
                config=None,
                connect_timeout=2,
            )
            conn.close()
            await conn.wait_closed()

        for _ in range(60):
            try:
                asyncio.run(ready())
                break
            except (OSError, asyncssh.Error):
                time.sleep(0.25)
        else:
            raise AssertionError("isolated sshd failed readiness")
        yield SSHFixture(
            container,
            port,
            password,
            key.export_private_key("openssh", passphrase=passphrase).decode(),
            passphrase,
        )
    finally:
        try:
            logs = subprocess.run(["docker", "logs", container], check=True, capture_output=True)
            assert password.encode() not in logs.stdout + logs.stderr
        finally:
            subprocess.run(["docker", "stop", container], check=True, capture_output=True)
