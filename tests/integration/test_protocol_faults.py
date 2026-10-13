import asyncio
from pathlib import Path

import asyncssh
from pydantic import SecretStr

from ssh_mcp.config import Settings
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.jobs.service import TaskService
from ssh_mcp.schemas import ConnectRequest, ExecuteRequest
from ssh_mcp.ssh.registry import SessionRegistry
from tests.integration.test_host_keys import NoAuthServer


class SlowAuth(asyncssh.SSHServer):
    def __init__(self):
        self.workers = set()

    async def begin_auth(self, username):
        worker = asyncio.current_task()
        self.workers.add(worker)
        try:
            await asyncio.sleep(10)
            return False
        finally:
            self.workers.discard(worker)

    def connection_lost(self, exc):
        for worker in list(self.workers):
            worker.cancel()


async def request_for(trust, port):
    probe = await trust.probe("127.0.0.1", port)
    trust.confirm(str(probe["probe_id"]))
    return ConnectRequest(
        alias="fixture",
        host="127.0.0.1",
        port=port,
        username="fixture",
        password=SecretStr("only-in-memory"),
        probe_id=str(probe["probe_id"]),
    )


async def test_auth_timeout_and_cancellation_release_inputs(tmp_path: Path):
    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=SlowAuth,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
    )
    trust = HostKeyTrust(tmp_path / "known_hosts")
    registry = SessionRegistry(
        Settings(known_hosts=trust.path, ssh_connect_timeout_seconds=1), trust
    )
    try:
        request = await request_for(trust, server.get_port())
        view = await registry.connect(request)
        assert view.error == "SSH_TIMEOUT" and request.password is None
        request = await request_for(trust, server.get_port())
        task = asyncio.create_task(registry.connect(request))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert request.password is None
        assert all(s.status == "failed" and not s.lock.locked() for s in registry.sessions.values())
    finally:
        await registry.close()
        server.close()
        await server.wait_closed()


async def test_channel_without_exit_status_is_not_success(tmp_path: Path):
    async def no_status(process):
        process.stdout.write("partial\n")
        process.close()

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=NoAuthServer,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        process_factory=no_status,
    )
    trust = HostKeyTrust(tmp_path / "known_hosts")
    registry = SessionRegistry(Settings(known_hosts=trust.path), trust)
    tasks = TaskService(registry.settings, registry)
    tasks.start()
    try:
        view = await registry.connect(await request_for(trust, server.get_port()))
        run = await tasks.submit(ExecuteRequest(session_id=view.session_id, command="echo fixture"))
        await asyncio.wait_for(run.done.wait(), 3)
        assert run.status == "failed" and run.error_code == "SSH_EXIT_STATUS_MISSING"
        assert tasks.read_output(run.run_id).stdout == "partial\n"
    finally:
        await tasks.close()
        await registry.close()
        server.close()
        await server.wait_closed()
