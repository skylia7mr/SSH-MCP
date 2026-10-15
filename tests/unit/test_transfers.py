import asyncio
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ssh_mcp.app import create_app
from ssh_mcp.config import Settings
from ssh_mcp.errors import ToolError
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.schemas import TransferRequest, TransferView
from ssh_mcp.ssh.registry import SessionRegistry
from ssh_mcp.transfer_cache import TransferCache
from ssh_mcp.transfers import TransferService


def service(tmp_path: Path, **kwargs) -> TransferService:
    settings = Settings(
        known_hosts=tmp_path / "known_hosts",
        transfer_root=tmp_path / "files",
        transfer_cache_root=tmp_path / "cache",
        **kwargs,
    )
    return TransferService(settings, SessionRegistry(settings, HostKeyTrust(settings.known_hosts)))


async def finish(transfers: TransferService, record):
    await asyncio.wait_for(transfers.workers[record.transfer_id], 5)
    return transfers.get(record.transfer_id)


async def test_local_copy_overwrite_and_directory_escape(tmp_path: Path) -> None:
    transfers = service(tmp_path)
    transfers.root.mkdir()
    data = bytes(range(256)) * 3000
    (transfers.root / "source").write_bytes(data)
    request = TransferRequest(source_path="source", target_path="nested/target")
    record = await finish(transfers, transfers.submit(request))
    assert record.status == "finished" and record.copied_bytes == len(data)
    assert (transfers.root / "nested/target").read_bytes() == data
    assert (transfers.root / "nested/target").stat().st_mode & 0o777 == 0o600
    (transfers.root / "source").write_bytes(b"replacement")
    record = await finish(transfers, transfers.submit(request))
    assert record.error_code == "TARGET_EXISTS"
    assert (transfers.root / "nested/target").read_bytes() == data
    record = await finish(
        transfers, transfers.submit(request.model_copy(update={"overwrite": True}))
    )
    assert record.status == "finished"
    assert (transfers.root / "nested/target").read_bytes() == b"replacement"
    (transfers.root / "outside").symlink_to(tmp_path)
    for path in ["../secret", str(tmp_path / "secret"), "outside/secret"]:
        with pytest.raises(ToolError, match="本地路径"):
            transfers.local_path(path)
    assert not list(transfers.root.rglob("*.part"))
    await transfers.close()


async def test_cancel_and_timeout_preserve_target(tmp_path: Path, monkeypatch) -> None:
    transfers = service(tmp_path, transfer_timeout_seconds=1)
    transfers.root.mkdir()
    (transfers.root / "source").write_bytes(b"new data")
    (transfers.root / "target").write_bytes(b"keep me")
    entered = asyncio.Event()

    async def slow_rsync(*args, **kwargs):
        entered.set()
        await asyncio.sleep(10)

    monkeypatch.setattr("ssh_mcp.transfers.run_rsync", slow_rsync)
    request = TransferRequest(source_path="source", target_path="target", overwrite=True)
    record = transfers.submit(request)
    await asyncio.wait_for(entered.wait(), 3)
    assert (await transfers.cancel(record.transfer_id)).status == "cancelled"
    record = await finish(transfers, transfers.submit(request))
    assert record.status == "timed_out"
    assert (transfers.root / "target").read_bytes() == b"keep me"
    assert not list(transfers.root.glob("*.part"))
    await transfers.close()


async def test_cancel_actual_rsync_preserves_existing_file(tmp_path: Path, monkeypatch) -> None:
    executable = shutil.which("rsync")
    assert executable
    wrapper = tmp_path / "slow-rsync"
    wrapper.write_text(f'#!/bin/sh\nexec "{executable}" --bwlimit=128 "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setattr("ssh_mcp.rsync.shutil.which", lambda _: str(wrapper))
    transfers = service(tmp_path)
    transfers.root.mkdir()
    (transfers.root / "source").write_bytes(os.urandom(2 * 1024 * 1024))
    (transfers.root / "target").write_bytes(b"existing")
    record = transfers.submit(
        TransferRequest(source_path="source", target_path="target", overwrite=True)
    )
    for _ in range(100):
        if list(transfers.root.glob(".target.*")):
            break
        await asyncio.sleep(0.01)
    assert list(transfers.root.glob(".target.*"))
    await asyncio.wait_for(transfers.cancel(record.transfer_id), 5)
    assert record.status == "cancelled"
    assert (transfers.root / "target").read_bytes() == b"existing"
    assert not list(transfers.root.glob(".target.*"))
    await transfers.close()


def test_browser_file_mutation_routes_removed(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            port=8000,
            known_hosts=tmp_path / "known_hosts",
            transfer_root=tmp_path / "files",
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        paired = client.post(
            "/api/auth/pair",
            json={"code": app.state.services.auth.pairing.code},
            headers={"origin": "http://127.0.0.1:8000"},
        )
        headers = {
            "origin": "http://127.0.0.1:8000",
            "x-csrf-token": paired.json()["csrf_token"],
        }
        for method, path in [
            ("GET", "/api/files"),
            ("POST", "/api/files/upload?name=test"),
            ("GET", "/api/files/download?path=test"),
            ("POST", "/api/transfers"),
            ("GET", "/api/transfers/test"),
            ("POST", "/api/transfers/test/cancel"),
        ]:
            expected = 404 if method == "GET" else 405
            assert client.request(method, path, headers=headers).status_code == expected
        assert (
            client.post(
                "/api/files/upload?name=large", content=b"x" * (256 * 1024 + 1), headers=headers
            ).status_code
            == 413
        )
        assert not (tmp_path / "files").exists()


def test_browser_transfer_history_filters_orders_and_expires_records(tmp_path: Path) -> None:
    app = create_app(Settings(port=8000, known_hosts=tmp_path / "known_hosts"))
    transfers = app.state.services.transfers
    current = datetime.now(UTC)
    for index, status in enumerate(["finished", "failed", "cancelled", "timed_out", "running"]):
        record = TransferView(
            transfer_id=f"transfer-{index}",
            source_session_id="alice" if index % 2 else None,
            target_session_id=None if index % 2 else "alice",
            source_path="/source",
            target_path="/target",
            status=status,
            copied_bytes=128,
            total_bytes=256,
            created_at=current + timedelta(seconds=index),
            finished_at=current if status != "running" else None,
            error_code="TRANSFER_FAILED" if status == "failed" else None,
            error="传输失败" if status == "failed" else None,
        )
        transfers.records[record.transfer_id] = record
    expired = transfers.records["transfer-0"].model_copy(
        update={
            "transfer_id": "expired",
            "finished_at": current
            - timedelta(seconds=transfers.settings.task_retention_seconds + 1),
        }
    )
    transfers.records["expired"] = expired
    transfers.records["other"] = expired.model_copy(
        update={
            "transfer_id": "other",
            "source_session_id": "bob",
            "target_session_id": "charlie",
            "finished_at": current,
        }
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/transfers?session_id=alice").status_code == 401
        response = client.post(
            "/api/auth/pair",
            json={"code": app.state.services.auth.pairing.code},
            headers={"origin": "http://127.0.0.1:8000"},
        )
        assert response.status_code == 200
        records = client.get("/api/transfers?session_id=alice").json()
        assert [record["transfer_id"] for record in records] == [
            f"transfer-{index}" for index in reversed(range(5))
        ]
        assert {record["status"] for record in records} == {
            "finished",
            "failed",
            "cancelled",
            "timed_out",
            "running",
        }
        assert records[0]["copied_bytes"] == 128 and records[0]["total_bytes"] == 256
        assert records[3]["error_code"] == "TRANSFER_FAILED"
        assert records[3]["error"] == "传输失败"
        assert "expired" not in transfers.records
        assert len(client.get("/api/transfers?session_id=alice&limit=2").json()) == 2
        assert len(client.get("/api/transfers?session_id=bob").json()) == 1
        assert client.get("/api/transfers?session_id=unknown").json() == []
        for query in ("", "?session_id=alice&limit=0", "?session_id=alice&limit=1001"):
            assert client.get("/api/transfers" + query).status_code == 422


async def test_cache_keeps_latest_seven_batches_and_recovers_from_disk(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    assert settings.transfer_cache_keep == 7
    assert settings.transfer_cache_root.is_relative_to(Path("/tmp"))
    cache = TransferCache(tmp_path / "cache", settings.transfer_cache_keep)
    batches = []
    for index in range(10):
        async with cache.stage(f"{index:024d}") as directory:
            (directory / "nested").mkdir()
            (directory / "nested/payload").write_bytes(str(index).encode())
            batches.append(directory)
        assert len(list(cache.root.iterdir())) == min(index + 1, 7)
    assert set(cache.root.iterdir()) == set(batches[-7:])
    assert not any(directory.exists() for directory in batches[:-7])
    for index, directory in enumerate(batches[-7:], start=3):
        assert (directory / "nested/payload").read_bytes() == str(index).encode()
    unmanaged = cache.root / "leave-me"
    unmanaged.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"untouched")
    (cache.root / f"relay-{'0' * 20}-{'0' * 24}").symlink_to(outside)
    recovered = TransferCache(cache.root, 2)
    await recovered.prune()
    assert unmanaged.exists()
    assert (outside / "secret").read_bytes() == b"untouched"
    assert all(directory.exists() == (directory in batches[-2:]) for directory in batches)


async def test_cache_protects_active_and_counts_cancelled_batches(tmp_path: Path) -> None:
    cache = TransferCache(tmp_path / "cache", 1)
    async with cache.stage("a" * 24) as active:
        (active / "payload").write_bytes(b"in progress")
        for index in range(3):
            async with cache.stage(f"{index:024d}") as completed:
                (completed / "payload").write_bytes(b"complete")
            assert (active / "payload").read_bytes() == b"in progress"
            assert completed.exists()
            assert len(list(cache.root.iterdir())) == 2
    assert not active.exists()
    assert list(cache.root.iterdir()) == [completed]
    entered = asyncio.Event()

    async def cancelled_stage():
        async with cache.stage("b" * 24) as directory:
            (directory / "payload").write_bytes(b"partial")
            entered.set()
            await asyncio.Event().wait()

    worker = asyncio.create_task(cancelled_stage())
    await entered.wait()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    assert not cache.active
    assert not completed.exists()
    assert len(list(cache.root.iterdir())) == 1


async def test_local_transfer_triggers_cache_cleanup_without_deleting_local_files(
    tmp_path: Path,
) -> None:
    transfers = service(tmp_path, transfer_cache_keep=1)
    previous = TransferCache(transfers.cache.root, 7)
    async with previous.stage("a" * 24) as oldest:
        (oldest / "payload").write_bytes(b"old")
    async with previous.stage("b" * 24) as newest:
        (newest / "payload").write_bytes(b"new")
    transfers.root.mkdir()
    (transfers.root / "source").write_bytes(b"local source")
    record = await finish(
        transfers, transfers.submit(TransferRequest(source_path="source", target_path="target"))
    )
    assert record.status == "finished"
    assert not oldest.exists()
    assert newest.exists()
    assert (transfers.root / "source").read_bytes() == b"local source"
    assert (transfers.root / "target").read_bytes() == b"local source"
    await transfers.close()
