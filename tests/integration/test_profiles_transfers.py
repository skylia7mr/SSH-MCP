import asyncio
import json
from pathlib import Path

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from ssh_mcp.config import Settings
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.schemas import ConnectRequest, ExecuteRequest, ReconnectRequest, TransferRequest
from ssh_mcp.ssh.registry import SessionRegistry
from ssh_mcp.transfers import TransferService
from tests.integration.test_jobs import execute, setup_service
from tests.integration.test_mcp_http import running_app, web_pair


async def probe_for(trust, port):
    probe = await trust.probe("127.0.0.1", port)
    trust.confirm(str(probe["probe_id"]))
    return str(probe["probe_id"])


async def test_edit_key_password_revokes_mcp_id_and_cancels_work(real_sshd, tmp_path: Path) -> None:
    async with (
        running_app(tmp_path, max_global_runs=1) as (app, base),
        httpx.AsyncClient(base_url=base) as web,
    ):
        await web_pair(web, app, base)
        services = app.state.services
        view = await services.registry.connect(
            ConnectRequest(
                alias="key host",
                host="127.0.0.1",
                port=real_sshd.port,
                username="alice",
                private_key=SecretStr(real_sshd.private_key),
                passphrase=SecretStr(real_sshd.passphrase),
                probe_id=await probe_for(services.trust, real_sshd.port),
            )
        )
        old_id = view.session_id
        assert len(old_id) == 64 and not view.account_password_configured
        await services.registry.sudo_policy(old_id, True)
        record, _ = await execute(services.tasks, old_id, "sudo id -u")
        assert record.error_code == "SUDO_PASSWORD_NOT_CONFIGURED"
        active = await services.tasks.submit(
            ExecuteRequest(session_id=old_id, command="sleep 20; touch /tmp/old-active")
        )
        for _ in range(100):
            if active.status == "running":
                break
            await asyncio.sleep(0.01)
        queued = await services.tasks.submit(
            ExecuteRequest(session_id=old_id, command="touch /tmp/old-queued")
        )
        for _ in range(services.settings.max_transfers):
            await services.transfers.slots.acquire()
        transfer = services.transfers.submit(
            TransferRequest(
                source_session_id=old_id, source_path="/etc/passwd", target_path="old.txt"
            )
        )
        body = {
            "alias": "renamed",
            "host": view.host,
            "port": view.port,
            "username": view.username,
            "auth_type": "key",
            "password": real_sshd.password,
        }
        response = await web.put(f"/api/sessions/{old_id}", json=body)
        assert response.status_code == 200
        updated = response.json()
        new_id = updated["session_id"]
        assert new_id != old_id and len(new_id) == 64
        assert updated["status"] == "connected" and updated["account_password_configured"]
        assert active.status == transfer.status == "cancelled"
        assert queued.status in {"cancelled", "disconnected"}
        for _ in range(services.settings.max_transfers):
            services.transfers.slots.release()
        token = services.auth.tokens.current()
        async with (
            httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}) as transport,
            streamable_http_client(base + "/mcp", http_client=transport) as streams,
            ClientSession(streams[0], streams[1]) as client,
        ):
            await client.initialize()
            for name, arguments in [
                ("ssh_execute", {"session_id": old_id, "command": "touch /tmp/stale-mcp"}),
                ("ssh_get_session", {"session_id": old_id}),
                ("ssh_list_files", {"session_id": old_id, "path": "/"}),
                (
                    "ssh_transfer_file",
                    {
                        "source_session_id": old_id,
                        "source_path": "/etc/passwd",
                        "target_path": "stale.txt",
                    },
                ),
            ]:
                result = await client.call_tool(name, arguments)
                assert (
                    result.is_error and result.structured_content["error_code"] == "UNKNOWN_SESSION"
                )
            record, output = await execute(services.tasks, new_id, "sudo id -u")
            assert record.exit_code == 0 and output.stdout == "0\n"
            connection = services.registry.get(new_id).require_connection()
            assert (
                await connection.run(
                    "test ! -e /tmp/old-active && test ! -e /tmp/old-queued && test ! -e /tmp/stale-mcp"
                )
            ).exit_status == 0
            assert real_sshd.password not in services.registry.store.path.read_text()
            body.pop("password")
            body["clear_password"] = True
            response = await web.put(f"/api/sessions/{new_id}", json=body)
            cleared = response.json()
            assert (
                not cleared["account_password_configured"]
                and not cleared["sudo_password_configured"]
            )
            duplicate = await services.registry.connect(
                ConnectRequest(
                    alias="renamed",
                    host="127.0.0.1",
                    port=real_sshd.port,
                    username="alice",
                    password=SecretStr(real_sshd.password),
                    probe_id=await probe_for(services.trust, real_sshd.port),
                )
            )
            assert duplicate.session_id != cleared["session_id"]
            deleted = services.registry.get(duplicate.session_id)
            assert (await web.delete(f"/api/sessions/{duplicate.session_id}")).status_code == 200
            assert deleted.connection.is_closed() and deleted.login_password is None
            body["host"] = "localhost"
            moved = (await web.put(f"/api/sessions/{cleared['session_id']}", json=body)).json()
            assert moved["session_id"] != cleared["session_id"]
            assert moved["status"] == "disconnected" and not moved["sudo_enabled"]
            assert services.registry.get(moved["session_id"]).connection is None
            assert (await web.delete(f"/api/sessions/{moved['session_id']}")).status_code == 200
            assert services.registry.list() == []
            assert json.loads(services.registry.store.path.read_text()) == []


async def test_restart_restores_profiles_and_reuses_login_password(
    real_sshd, tmp_path: Path
) -> None:
    trust = HostKeyTrust(tmp_path / "known_hosts")
    settings = Settings(known_hosts=trust.path)
    registry = SessionRegistry(settings, trust)
    password = await registry.connect(
        ConnectRequest(
            alias="password",
            host="127.0.0.1",
            port=real_sshd.port,
            username="alice",
            password=SecretStr(real_sshd.password),
            probe_id=await probe_for(trust, real_sshd.port),
        )
    )
    key = await registry.connect(
        ConnectRequest(
            alias="encrypted key",
            host="127.0.0.1",
            port=real_sshd.port,
            username="alice",
            private_key=SecretStr(real_sshd.private_key),
            passphrase=SecretStr(real_sshd.passphrase),
            probe_id=await probe_for(trust, real_sshd.port),
        )
    )
    assert password.status == key.status == "connected"
    await registry.sudo_policy(password.session_id, True)
    assert registry.get(password.session_id).sudo_password == real_sshd.password
    await registry.close()
    store = tmp_path / "connections.json"
    assert store.stat().st_mode & 0o777 == 0o600
    content = store.read_text()
    assert real_sshd.password not in content and real_sshd.passphrase not in content
    assert all("password" not in entry for entry in json.loads(content))
    registry = SessionRegistry(settings, trust)
    try:
        assert len(registry.list()) == 2
        assert all(
            view.status == "disconnected" and not view.sudo_enabled for view in registry.list()
        )
        for previous in (password, key):
            request = ReconnectRequest(
                probe_id=await probe_for(trust, real_sshd.port),
                password=SecretStr(real_sshd.password),
                passphrase=SecretStr(real_sshd.passphrase) if previous is key else None,
            )
            current = await registry.reconnect(previous.session_id, request)
            assert current.status == "connected" and current.session_id == previous.session_id
            assert request.password is request.passphrase is None
            await registry.sudo_policy(current.session_id, True)
            assert registry.get(current.session_id).sudo_password == real_sshd.password
        await registry.forget(password.session_id)
    finally:
        await registry.close()
    reloaded = SessionRegistry(settings, trust)
    assert [view.session_id for view in reloaded.list()] == [key.session_id]
    await reloaded.close()


async def test_real_rsync_all_directions_and_overwrite(real_sshd, tmp_path: Path) -> None:
    registry, tasks, sessions = await setup_service(
        real_sshd,
        tmp_path,
        transfer_root=tmp_path / "files",
        transfer_cache_root=tmp_path / "cache",
    )
    transfers = TransferService(registry.settings, registry)
    transfers.root.mkdir()
    content = bytes(range(256)) * 3000 + "二进制文件".encode()
    (transfers.root / "input.bin").write_bytes(content)

    async def copy(**kwargs):
        record = transfers.submit(TransferRequest(**kwargs))
        await asyncio.wait_for(transfers.workers[record.transfer_id], 15)
        assert record.status == "finished", record
        assert record.copied_bytes == len(content)
        return record

    try:
        await copy(
            source_path="input.bin",
            target_session_id=sessions["alice"],
            target_path="/home/alice/中文文件.bin",
        )
        listing = await transfers.list_files(sessions["alice"], "/home/alice")
        assert any(entry.name == "中文文件.bin" and entry.size == len(content) for entry in listing)
        await copy(
            source_session_id=sessions["alice"],
            source_path="/home/alice/中文文件.bin",
            target_session_id=sessions["bob"],
            target_path="/home/bob/relay.bin",
        )
        await copy(
            source_session_id=sessions["bob"],
            source_path="/home/bob/relay.bin",
            target_path="downloads/output.bin",
        )
        assert (transfers.root / "downloads/output.bin").read_bytes() == content
        duplicate = transfers.submit(
            TransferRequest(
                source_path="input.bin",
                target_session_id=sessions["alice"],
                target_path="/home/alice/中文文件.bin",
            )
        )
        await asyncio.wait_for(transfers.workers[duplicate.transfer_id], 10)
        assert duplicate.status == "failed" and duplicate.error_code == "TARGET_EXISTS"
        await copy(
            source_path="input.bin",
            target_session_id=sessions["alice"],
            target_path="/home/alice/中文文件.bin",
            overwrite=True,
        )
        tree = transfers.root / "tree" / "nested"
        tree.mkdir(parents=True)
        name = "空 格;$(touch injected).txt"
        (tree / name).write_bytes(content)
        for arguments in [
            {
                "source_path": "tree",
                "target_session_id": sessions["alice"],
                "target_path": "/home/alice/tree",
            },
            {
                "source_session_id": sessions["alice"],
                "source_path": "/home/alice/tree",
                "target_session_id": sessions["bob"],
                "target_path": "/home/bob/tree",
            },
            {
                "source_session_id": sessions["bob"],
                "source_path": "/home/bob/tree",
                "target_path": "tree-downloaded",
            },
        ]:
            directory = transfers.submit(TransferRequest(**arguments, recursive=True))
            await asyncio.wait_for(transfers.workers[directory.transfer_id], 15)
            assert directory.status == "finished", directory
        assert (transfers.root / "tree-downloaded/nested" / name).read_bytes() == content
        cached = list(transfers.cache.root.iterdir())
        assert len(cached) == 2
        assert any((directory / "payload").is_file() for directory in cached)
        assert any((directory / "payload/nested" / name).is_file() for directory in cached)
        assert not transfers.cache.root.is_relative_to(transfers.root)
        assert not (transfers.root / "injected").exists()
        for sid in (sessions["alice"], sessions["bob"]):
            assert (
                await registry.get(sid).require_connection().run("test ! -e injected")
            ).exit_status == 0
        encrypted = await registry.connect(
            ConnectRequest(
                alias="key-only",
                host="127.0.0.1",
                port=real_sshd.port,
                username="alice",
                private_key=SecretStr(real_sshd.private_key),
                passphrase=SecretStr(real_sshd.passphrase),
                probe_id=await probe_for(registry.trust, real_sshd.port),
            )
        )
        assert not encrypted.account_password_configured
        await copy(
            source_session_id=encrypted.session_id,
            source_path="/home/alice/中文文件.bin",
            target_path="key-only.bin",
        )
        assert (transfers.root / "key-only.bin").read_bytes() == content
    finally:
        await transfers.close()
        await tasks.close()
        await registry.close()


async def test_extended_shell_syntax_and_sudo(real_sshd, tmp_path: Path) -> None:
    registry, tasks, sessions = await setup_service(real_sshd, tmp_path)
    try:
        for account in ("alice", "bob"):
            sid = sessions[account]
            await registry.sudo_policy(sid, True)
            for command, expected in [
                ("for item in a b; do printf '%s' \"$item\"; done", "ab"),
                ("f() { printf hello; }; f; printf '%s' $((1+2))", "hello3"),
                ("if true; then sudo id -u; fi", "0\n"),
                ("for item in a b; do sudo id -u; done", "0\n0\n"),
                ("f() { sudo id -u; }; f", "0\n"),
                ("sudo bash -c 'id -u; printf done'", "0\ndone"),
            ]:
                record, output = await execute(tasks, sid, command)
                assert record.exit_code == 0, (command, record.view(), output)
                assert output.stdout == expected
                assert real_sshd.password not in str(output)
        record = await tasks.submit(
            ExecuteRequest(session_id=sessions["alice"], command="echo x &&")
        )
        await asyncio.wait_for(record.done.wait(), 5)
        assert record.exit_code != 0
    finally:
        await tasks.close()
        await registry.close()
