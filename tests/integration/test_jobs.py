import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr

from ssh_mcp.config import Settings
from ssh_mcp.errors import ToolError
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.jobs.service import TaskService
from ssh_mcp.schemas import ConnectRequest, ExecuteRequest
from ssh_mcp.ssh.registry import SessionRegistry


async def setup_service(real_sshd, tmp_path: Path, **limits):
    settings = Settings(known_hosts=tmp_path / "known_hosts", **limits)
    trust = HostKeyTrust(settings.known_hosts)
    registry = SessionRegistry(settings, trust)
    sessions = {}
    for name in ("alice", "bob", "nopass"):
        probe = await trust.probe("127.0.0.1", real_sshd.port)
        trust.confirm(str(probe["probe_id"]))
        view = await registry.connect(
            ConnectRequest(
                alias=name,
                host="127.0.0.1",
                port=real_sshd.port,
                username=name,
                password=SecretStr(real_sshd.password),
                probe_id=str(probe["probe_id"]),
            )
        )
        assert view.status == "connected"
        sessions[name] = view.session_id
    tasks = TaskService(settings, registry)
    tasks.start()
    return registry, tasks, sessions


async def execute(tasks, session, command, **kwargs):
    r = await tasks.submit(ExecuteRequest(session_id=session, command=command, **kwargs))
    await asyncio.wait_for(r.done.wait(), 15)
    return r, tasks.read_output(r.run_id)


async def test_real_task_results_streams_and_limits(real_sshd, tmp_path: Path) -> None:
    registry, tasks, sessions = await setup_service(real_sshd, tmp_path, max_output_bytes=1024)
    try:
        for account in ("alice", "bob"):
            r, out = await execute(
                tasks, sessions[account], "id -un; printf 'error\\n' >&2; exit 7"
            )
            assert r.status == "finished" and r.exit_code == 7
            assert out.stdout == account + "\n" and out.stderr == "error\n"
        r, out = await execute(
            tasks, sessions["alice"], "yes x | head -c 20000; yes y | head -c 20000 >&2"
        )
        assert r.status == "finished"
        assert len(r.stdout.data) == len(r.stderr.data) == 1024
        assert out.stdout_truncated and out.stderr_truncated
        r, out = await execute(
            tasks,
            sessions["alice"],
            'pwd; printf "%s" "$VALUE"',
            cwd="/tmp",
            env={"VALUE": "value'; sudo id"},
        )
        assert out.stdout == "/tmp\nvalue'; sudo id"
        long = await tasks.submit(
            ExecuteRequest(
                session_id=sessions["alice"], command="printf first; sleep 5; printf last"
            )
        )
        fast = await tasks.quick_result(long)
        assert fast.status == "running" and tasks.read_output(long.run_id).stdout == "first"
        assert (await tasks.cancel(long.run_id)).status == "cancelled"
        r, _ = await execute(tasks, sessions["alice"], "sleep 5", timeout_seconds=1)
        assert r.status == "timed_out"
        request = ExecuteRequest(
            session_id=sessions["alice"], command="echo idempotent", idempotency_key="one"
        )
        first = await tasks.submit(request)
        assert await tasks.submit(request.model_copy()) is first
        with pytest.raises(ToolError) as e:
            await tasks.submit(request.model_copy(update={"command": "echo different"}))
        assert e.value.code == "IDEMPOTENCY_CONFLICT"
    finally:
        await tasks.close()
        await registry.close()
    assert tasks.running == 0


async def test_sudo_matrix_no_sentinel_leak(real_sshd, tmp_path: Path) -> None:
    registry, tasks, sessions = await setup_service(real_sshd, tmp_path)
    cases = [
        "echo ready && sudo id",
        "false || sudo id",
        "touch /tmp/side-effect; sudo id",
        "sudo id; sudo id",
        "sudo cat /etc/passwd | grep root",
        "printf 'data\\n' | sudo tee /tmp/pipe-result",
        "(sudo id)",
        'echo "$(sudo id)"',
        "/usr/bin/sudo id",
        "command sudo id",
        "env sudo id",
        "env FOO=bar sudo id",
        "sudo -u root id",
        "sudo id 2>/dev/null",
    ]
    try:
        for case in cases:
            with pytest.raises(ToolError) as e:
                await tasks.submit(ExecuteRequest(session_id=sessions["alice"], command=case))
            assert e.value.code == "SUDO_NOT_AUTHORIZED"
        conn = registry.get(sessions["alice"]).require_connection()
        assert (await conn.run("test ! -e /tmp/side-effect")).exit_status == 0
        await registry.sudo_policy(sessions["alice"], True)
        await registry.sudo_password(sessions["alice"], real_sshd.password)
        for case in cases:
            r, out = await execute(tasks, sessions["alice"], case)
            assert r.status == "finished" and r.exit_code == 0, (case, r.view(), out)
            assert real_sshd.password not in str(r.view()) + str(out)
            assert "__SSH_MCP_" not in out.stdout + out.stderr
            assert real_sshd.password not in r.plan.transformed
        file = await conn.run("cat /tmp/pipe-result")
        assert file.stdout == "data\n"
        with pytest.raises(ToolError):
            await tasks.submit(ExecuteRequest(session_id=sessions["bob"], command="sudo id"))
        await registry.sudo_password(sessions["alice"], None)
        r, _ = await execute(tasks, sessions["alice"], "sudo id")
        assert r.error_code == "SUDO_PASSWORD_NOT_CONFIGURED"
        await registry.sudo_password(sessions["alice"], "deliberately-wrong")
        r, _ = await execute(tasks, sessions["alice"], "sudo id")
        assert r.error_code == "SUDO_AUTHENTICATION_FAILED"
        await registry.sudo_policy(sessions["nopass"], True)
        r, out = await execute(
            tasks, sessions["nopass"], "printf 'data\\n' | sudo tee /tmp/nopass-result"
        )
        assert r.status == "finished" and out.stdout == "data\n"
    finally:
        await tasks.close()
        await registry.close()


async def test_queued_policy_recheck_and_running_snapshot(real_sshd, tmp_path: Path) -> None:
    registry, tasks, sessions = await setup_service(
        real_sshd, tmp_path, max_global_runs=1, max_session_runs=1, max_queue=2
    )
    sid = sessions["alice"]
    try:
        await registry.sudo_policy(sid, True)
        await registry.sudo_password(sid, real_sshd.password)
        blocker = await tasks.submit(ExecuteRequest(session_id=sid, command="sleep 2"))
        await asyncio.sleep(0.1)
        queued = await tasks.submit(
            ExecuteRequest(session_id=sid, command="touch /tmp/queued-side; sudo id")
        )
        await registry.sudo_policy(sid, False)
        await asyncio.wait_for(queued.done.wait(), 5)
        assert queued.error_code == "SUDO_NOT_AUTHORIZED"
        assert (
            await registry.get(sid).require_connection().run("test ! -e /tmp/queued-side")
        ).exit_status == 0
        await registry.sudo_policy(sid, True)
        await registry.sudo_password(sid, real_sshd.password)
        active = await tasks.submit(ExecuteRequest(session_id=sid, command="sleep 1; sudo id"))
        for _ in range(50):
            if active.status == "running":
                break
            await asyncio.sleep(0.02)
        version = active.policy_version
        await registry.sudo_policy(sid, False)
        await asyncio.wait_for(active.done.wait(), 5)
        assert active.status == "finished" and active.exit_code == 0
        assert active.policy_version == version
        assert registry.get(sid).sudo_password is None
        assert blocker.status == "finished"
    finally:
        await tasks.close()
        await registry.close()


async def test_queue_concurrency_record_and_retention_limits(real_sshd, tmp_path: Path) -> None:
    registry, tasks, sessions = await setup_service(
        real_sshd,
        tmp_path,
        max_global_runs=2,
        max_session_runs=1,
        max_queue=2,
        max_task_records=4,
        task_retention_seconds=1,
    )
    try:
        a = await tasks.submit(ExecuteRequest(session_id=sessions["alice"], command="sleep 5"))
        b = await tasks.submit(ExecuteRequest(session_id=sessions["bob"], command="sleep 5"))
        for _ in range(100):
            if a.status == b.status == "running":
                break
            await asyncio.sleep(0.01)
        assert tasks.running == 2 and max(tasks.running_by_session.values()) == 1
        q1 = await tasks.submit(
            ExecuteRequest(session_id=sessions["alice"], command="echo queued1")
        )
        q2 = await tasks.submit(ExecuteRequest(session_id=sessions["bob"], command="echo queued2"))
        with pytest.raises(ToolError) as e:
            await tasks.submit(ExecuteRequest(session_id=sessions["alice"], command="echo excess"))
        assert e.value.code == "QUEUE_LIMIT"
        await tasks.cancel(q1.run_id)
        replacement = await tasks.submit(
            ExecuteRequest(session_id=sessions["alice"], command="echo replacement")
        )
        assert a.run_id in tasks.runs and b.run_id in tasks.runs and q1.run_id not in tasks.runs
        await tasks.cancel(a.run_id)
        await tasks.cancel(b.run_id)
        await asyncio.wait_for(asyncio.gather(q2.done.wait(), replacement.done.wait()), 5)
        assert replacement.status == q2.status == "finished"
        await asyncio.sleep(1.1)
        with pytest.raises(ToolError) as e:
            tasks.read_output(replacement.run_id)
        assert e.value.code == "OUTPUT_EXPIRED"
        assert len(tasks.runs) == 0
    finally:
        await tasks.close()
        await registry.close()


async def test_drop_disconnect_and_shutdown_release_resources(real_sshd, tmp_path: Path) -> None:
    registry, tasks, sessions = await setup_service(real_sshd, tmp_path, max_session_runs=1)
    try:
        sid = sessions["alice"]
        active = await tasks.submit(
            ExecuteRequest(session_id=sid, command="printf started; sleep 20")
        )
        for _ in range(100):
            if active.stdout.data:
                break
            await asyncio.sleep(0.01)
        queued = await tasks.submit(ExecuteRequest(session_id=sid, command="echo not-replayed"))
        # Abrupt transport loss exercises monitor and channel state, without graceful web disconnect.
        registry.get(sid).require_connection().abort()
        await asyncio.wait_for(asyncio.gather(active.done.wait(), queued.done.wait()), 5)
        assert active.status == queued.status == "disconnected"
        assert not queued.stdout.data
        assert registry.get(sid).status == "disconnected"
        assert registry.get(sid).sudo_password is None
        running = await tasks.submit(ExecuteRequest(session_id=sessions["bob"], command="sleep 20"))
        await asyncio.sleep(0.1)
        await tasks.close()
        assert running.done.is_set() and running.status == "cancelled"
        assert tasks.running == 0 and not tasks.runs and not tasks.keys
    finally:
        await tasks.close()
        await registry.close()


async def test_real_invalid_bytes_and_signal(real_sshd, tmp_path: Path) -> None:
    registry, tasks, sessions = await setup_service(real_sshd, tmp_path)
    try:
        r, out = await execute(tasks, sessions["alice"], "printf '\\377\\342\\202\\254'")
        assert out.stdout == "�€" and out.next_stdout_cursor == 4
        r, _ = await execute(tasks, sessions["alice"], "kill -TERM $$")
        assert r.signal == "TERM" and r.status == "finished"
    finally:
        await tasks.close()
        await registry.close()


async def test_default_megabyte_caps_and_all_live_record_limit(real_sshd, tmp_path: Path):
    registry, tasks, sessions = await setup_service(real_sshd, tmp_path, max_task_records=1)
    try:
        r = await tasks.submit(
            ExecuteRequest(
                session_id=sessions["alice"],
                command="sleep 1; yes x | head -c 1500000; yes y | head -c 1500000 >&2",
            )
        )
        with pytest.raises(ToolError) as error:
            await tasks.submit(ExecuteRequest(session_id=sessions["bob"], command="echo excess"))
        assert error.value.code == "TASK_RECORD_LIMIT"
        await asyncio.wait_for(r.done.wait(), 10)
        assert r.status == "finished" and r.exit_code == 0
        assert len(r.stdout.data) == len(r.stderr.data) == 1048576
        assert r.stdout.truncated and r.stderr.truncated
        assert len(tasks.read_output(r.run_id, limit=65536).stdout) == 65536
    finally:
        await tasks.close()
        await registry.close()


async def test_sudo_zero_timestamp_policy_rejected_before_subcommand(real_sshd, tmp_path: Path):
    registry, tasks, _ = await setup_service(real_sshd, tmp_path)
    try:
        probe = await registry.trust.probe("127.0.0.1", real_sshd.port)
        registry.trust.confirm(str(probe["probe_id"]))
        view = await registry.connect(
            ConnectRequest(
                alias="zero",
                host="127.0.0.1",
                port=real_sshd.port,
                username="zerocache",
                password=SecretStr(real_sshd.password),
                probe_id=str(probe["probe_id"]),
            )
        )
        assert view.status == "connected"
        await registry.sudo_policy(view.session_id, True)
        await registry.sudo_password(view.session_id, real_sshd.password)
        run, out = await execute(tasks, view.session_id, "sudo touch /tmp/zero-policy-side-effect")
        assert run.error_code == "SUDO_POLICY_UNSUPPORTED"
        assert "__SSH_MCP_" not in out.stderr and real_sshd.password not in out.stderr
        assert (
            await registry.get(view.session_id)
            .require_connection()
            .run("test ! -e /tmp/zero-policy-side-effect")
        ).exit_status == 0
    finally:
        await tasks.close()
        await registry.close()
