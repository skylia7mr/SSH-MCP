import asyncio
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ssh_mcp.app import create_app
from ssh_mcp.config import Settings
from ssh_mcp.http_server import WorkbenchServer


@asynccontextmanager
async def running_app(tmp_path: Path, **settings):
    settings.setdefault("transfer_root", tmp_path / "files")
    settings.setdefault("transfer_cache_root", tmp_path / "cache")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    app = create_app(Settings(port=port, known_hosts=tmp_path / "known_hosts", **settings))
    server = WorkbenchServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="error",
            timeout_graceful_shutdown=5,
        ),
        app.state.services,
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert server.started
        yield app, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
        # The SDK process watcher expects asyncio.run() to close the loop. pytest
        # reuses the loop, so dispose this SDK watcher after the test server exits.
        watchers = [
            t for t in asyncio.all_tasks() if t.get_coro().__qualname__ == "_shutdown_watcher"
        ]
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)


async def web_pair(client, app, base):
    headers = {"origin": base}
    response = await client.post(
        "/api/auth/pair", json={"code": app.state.services.auth.pairing.code}, headers=headers
    )
    assert response.status_code == 200
    client.headers.update({"origin": base, "x-csrf-token": response.json()["csrf_token"]})


async def web_connect(client, fixture, username):
    probe = (
        await client.post("/api/host-keys/probe", json={"host": "127.0.0.1", "port": fixture.port})
    ).json()
    assert (
        await client.post("/api/host-keys/confirm", json={"probe_id": probe["probe_id"]})
    ).status_code == 200
    view = (
        await client.post(
            "/api/sessions",
            json={
                "alias": username,
                "host": "127.0.0.1",
                "port": fixture.port,
                "username": username,
                "password": fixture.password,
                "probe_id": probe["probe_id"],
            },
        )
    ).json()
    assert view["status"] == "connected"
    return view["session_id"]


async def test_official_mcp_client_over_real_http_two_accounts(real_sshd, tmp_path: Path) -> None:
    async with (
        running_app(tmp_path) as (app, base),
        httpx.AsyncClient(base_url=base, timeout=20) as web,
    ):
        await web_pair(web, app, base)
        alice = await web_connect(web, real_sshd, "alice")
        bob = await web_connect(web, real_sshd, "bob")
        token = (await web.post("/api/mcp/token/reveal")).json()["token"]
        assert (await web.get("/mcp", headers={"authorization": "Bearer wrong"})).status_code == 401
        async with (
            httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}) as transport,
            streamable_http_client(base + "/mcp", http_client=transport) as streams,
            ClientSession(streams[0], streams[1]) as client,
        ):
            initialized = await client.initialize()
            assert initialized.server_info.name == "SSH MCP"
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == {
                "ssh_list_sessions",
                "ssh_get_session",
                "ssh_execute",
                "ssh_get_run",
                "ssh_read_output",
                "ssh_cancel_run",
                "ssh_list_files",
                "ssh_transfer_file",
                "ssh_get_transfer",
                "ssh_cancel_transfer",
            }
            listed = await client.call_tool("ssh_list_sessions")
            assert {s["session_id"] for s in listed.structured_content["sessions"]} == {
                alice,
                bob,
            }
            content = bytes(range(256)) * 1500
            local_root = app.state.services.transfers.root
            local_root.mkdir(parents=True, exist_ok=True)
            local_source = "mcp.bin"
            (local_root / local_source).write_bytes(content)
            for arguments in [
                {
                    "source_path": local_source,
                    "target_session_id": alice,
                    "target_path": "/home/alice/mcp.bin",
                },
                {
                    "source_session_id": alice,
                    "source_path": "/home/alice/mcp.bin",
                    "target_session_id": bob,
                    "target_path": "/home/bob/mcp.bin",
                },
                {
                    "source_session_id": bob,
                    "source_path": "/home/bob/mcp.bin",
                    "target_path": "downloads/mcp.bin",
                },
            ]:
                submitted = await client.call_tool("ssh_transfer_file", arguments)
                assert not submitted.is_error, submitted
                transfer_id = submitted.structured_content["transfer_id"]
                for _ in range(100):
                    status = await client.call_tool(
                        "ssh_get_transfer", {"transfer_id": transfer_id}
                    )
                    if status.structured_content["status"] not in {"queued", "running"}:
                        break
                    await asyncio.sleep(0.02)
                assert status.structured_content["status"] == "finished", status
                assert status.structured_content["copied_bytes"] == len(content)
            assert (local_root / "downloads/mcp.bin").read_bytes() == content
            listing = await client.call_tool(
                "ssh_list_files", {"path": "/home/bob", "session_id": bob}
            )
            assert any(entry["name"] == "mcp.bin" for entry in listing.structured_content["files"])
            for _ in range(app.state.services.settings.max_transfers):
                await app.state.services.transfers.slots.acquire()
            try:
                queued_transfer = await client.call_tool(
                    "ssh_transfer_file",
                    {
                        "source_path": local_source,
                        "target_path": "cancelled.bin",
                    },
                )
                cancelled_transfer = await client.call_tool(
                    "ssh_cancel_transfer",
                    {
                        "transfer_id": queued_transfer.structured_content["transfer_id"],
                    },
                )
                assert cancelled_transfer.structured_content["status"] == "cancelled"
            finally:
                for _ in range(app.state.services.settings.max_transfers):
                    app.state.services.transfers.slots.release()
            for sid, username in [(alice, "alice"), (bob, "bob")]:
                run = await client.call_tool(
                    "ssh_execute",
                    {
                        "session_id": sid,
                        "command": "id -un; printf 'stderr\\n' >&2; exit 9",
                    },
                )
                assert not run.is_error
                assert run.structured_content["status"] == "finished"
                assert run.structured_content["exit_code"] == 9
                assert run.structured_content["output"]["stdout"] == username + "\n"
                assert run.structured_content["output"]["stderr"] == "stderr\n"
            error = await client.call_tool("ssh_get_session", {"session_id": "unknown"})
            assert error.is_error and error.structured_content["error_code"] == "UNKNOWN_SESSION"
            long = await client.call_tool(
                "ssh_execute",
                {"session_id": alice, "command": "printf first; sleep 5; printf last"},
            )
            payload = long.structured_content
            assert payload["status"] == "running" and payload["output"]["stdout"] == "first"
            output = await client.call_tool("ssh_read_output", {"run_id": payload["run_id"]})
            assert output.structured_content["stdout"] == "first"
            cancelled = await client.call_tool("ssh_cancel_run", {"run_id": payload["run_id"]})
            assert cancelled.structured_content["status"] == "cancelled"
        new_token = (await web.post("/api/mcp/token/rotate")).json()["token"]
        assert new_token != token
        assert (
            await web.get("/mcp", headers={"authorization": "Bearer " + token})
        ).status_code == 401
        async with web.stream(
            "GET", "/mcp", headers={"authorization": "Bearer " + new_token}
        ) as persistent:
            assert persistent.status_code in {405, 200}
            if persistent.status_code == 200:
                await web.post("/api/mcp/token/rotate")
                await asyncio.wait_for(persistent.aread(), 2)


async def test_rotation_revokes_inflight_http_but_keeps_submitted_task(real_sshd, tmp_path: Path):
    async with (
        running_app(tmp_path) as (app, base),
        httpx.AsyncClient(base_url=base, timeout=10) as web,
    ):
        await web_pair(web, app, base)
        sid = await web_connect(web, real_sshd, "alice")
        token = (await web.post("/api/mcp/token/reveal")).json()["token"]
        headers = {
            "authorization": "Bearer " + token,
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        }
        call = asyncio.create_task(
            web.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 77,
                    "method": "tools/call",
                    "params": {
                        "name": "ssh_execute",
                        "arguments": {
                            "session_id": sid,
                            "command": "printf detached; sleep 1; printf survived",
                        },
                    },
                },
            )
        )
        for _ in range(100):
            if app.state.services.tasks.runs:
                break
            await asyncio.sleep(0.01)
        assert app.state.services.tasks.runs
        await web.post("/api/mcp/token/rotate")
        response = await call
        assert response.status_code == 401
        run = next(iter(app.state.services.tasks.runs.values()))
        await asyncio.wait_for(run.done.wait(), 3)
        assert run.status == "finished" and run.stdout.data == b"detachedsurvived"
