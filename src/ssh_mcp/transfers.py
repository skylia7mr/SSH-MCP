import asyncio
import os
import secrets
import stat
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import asyncssh

from ssh_mcp.config import Settings
from ssh_mcp.errors import ToolError
from ssh_mcp.rsync import run_rsync
from ssh_mcp.schemas import FileEntry, RunStatus, TransferRequest, TransferView
from ssh_mcp.ssh.registry import SessionRegistry
from ssh_mcp.transfer_cache import TransferCache

TERMINAL = {RunStatus.finished, RunStatus.failed, RunStatus.cancelled, RunStatus.timed_out}


class TransferService:
    def __init__(self, settings: Settings, registry: SessionRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.root = settings.transfer_root.resolve()
        self.cache = TransferCache(settings.transfer_cache_root, settings.transfer_cache_keep)
        self.records: dict[str, TransferView] = {}
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.slots = asyncio.Semaphore(settings.max_transfers)
        self.closing = False

    def local_path(self, path: str) -> Path:
        if not path or "\x00" in path:
            raise ToolError("INVALID_ARGUMENT", "文件路径无效")
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolError("LOCAL_PATH_DENIED", "本地路径必须位于工作台文件目录内", 403)
        return resolved

    def validate_endpoint(self, session_id: str | None, path: str) -> None:
        if session_id:
            self.registry.get(session_id).require_connection()
            if not PurePosixPath(path).is_absolute():
                raise ToolError("INVALID_ARGUMENT", "远程文件请使用绝对路径")
        else:
            self.local_path(path)

    @asynccontextmanager
    async def sftp(self, session_id: str | None) -> AsyncIterator[asyncssh.SFTPClient | None]:
        if session_id:
            connection = self.registry.get(session_id).require_connection()
            async with connection.start_sftp_client() as client:
                yield client
        else:
            yield None

    async def list_files(self, session_id: str | None, path: str) -> list[FileEntry]:
        self.validate_endpoint(session_id, path)
        entries: list[FileEntry] = []
        try:
            async with asyncio.timeout(30), self.sftp(session_id) as client:
                if client:
                    async for item in client.scandir(path):
                        name = str(item.filename)
                        if name in {".", ".."}:
                            continue
                        entries.append(
                            FileEntry(
                                name=name,
                                path=str(PurePosixPath(path) / name),
                                is_dir=item.attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY,
                                size=item.attrs.size,
                            )
                        )
                        if len(entries) >= 1000:
                            break
                else:
                    directory = self.local_path(path)
                    if directory == self.root:
                        directory.mkdir(parents=True, exist_ok=True)

                    def scan() -> list[FileEntry]:
                        result = []
                        with os.scandir(directory) as listing:
                            for item in listing:
                                resolved = Path(item.path).resolve()
                                if not resolved.is_relative_to(self.root):
                                    continue
                                result.append(
                                    FileEntry(
                                        name=item.name,
                                        path=str(Path(item.path).relative_to(self.root)),
                                        is_dir=item.is_dir(),
                                        size=item.stat().st_size,
                                    )
                                )
                                if len(result) >= 1000:
                                    break
                        return result

                    entries = await asyncio.to_thread(scan)
        except (OSError, asyncssh.Error, TimeoutError):
            raise ToolError(
                "FILE_LIST_FAILED", "无法读取目录，请检查路径、权限及 SFTP 支持", 409
            ) from None
        return sorted(entries, key=lambda entry: (not entry.is_dir, entry.name))

    def cleanup(self) -> None:
        current = datetime.now(UTC)
        for transfer_id, record in list(self.records.items()):
            if (
                record.finished_at
                and (current - record.finished_at).total_seconds()
                > self.settings.task_retention_seconds
            ):
                self.records.pop(transfer_id)

    def submit(self, request: TransferRequest) -> TransferView:
        if self.closing:
            raise ToolError("SHUTTING_DOWN", "服务正在退出", 503)
        self.validate_endpoint(request.source_session_id, request.source_path)
        self.validate_endpoint(request.target_session_id, request.target_path)
        if (
            not PurePosixPath(request.target_path).name
            or PurePosixPath(request.target_path).name == ".."
        ):
            raise ToolError("INVALID_ARGUMENT", "目标路径必须包含文件名")
        if request.source_session_id == request.target_session_id:
            source = (
                PurePosixPath(request.source_path)
                if request.source_session_id
                else self.local_path(request.source_path)
            )
            target = (
                PurePosixPath(request.target_path)
                if request.target_session_id
                else self.local_path(request.target_path)
            )
            if source == target:
                raise ToolError("INVALID_ARGUMENT", "源文件与目标文件不能相同")
            if request.recursive and source in target.parents:
                raise ToolError("INVALID_ARGUMENT", "目标目录不能位于源目录内部")
        self.cleanup()
        if len(self.workers) >= self.settings.max_queue:
            raise ToolError("TRANSFER_LIMIT", "待处理传输过多", 429)
        if len(self.records) >= self.settings.max_task_records:
            oldest = next(
                (key for key, record in self.records.items() if record.status in TERMINAL), None
            )
            if oldest is None:
                raise ToolError("TRANSFER_LIMIT", "传输记录已达上限", 429)
            self.records.pop(oldest)
        record = TransferView(
            **request.model_dump(),
            transfer_id=secrets.token_urlsafe(18),
            created_at=datetime.now(UTC),
        )
        self.records[record.transfer_id] = record
        worker = asyncio.create_task(self._run(record))
        self.workers[record.transfer_id] = worker
        worker.add_done_callback(lambda _: self.workers.pop(record.transfer_id, None))
        return record

    def get(self, transfer_id: str) -> TransferView:
        self.cleanup()
        record = self.records.get(transfer_id)
        if record is None:
            raise ToolError("UNKNOWN_TRANSFER", "传输记录不存在或已过期", 404)
        return record

    async def cancel(self, transfer_id: str) -> TransferView:
        record = self.get(transfer_id)
        worker = self.workers.get(transfer_id)
        if worker and record.status not in TERMINAL:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if record.status not in TERMINAL:
                record.status = RunStatus.cancelled
                record.finished_at = datetime.now(UTC)
        return record

    async def _run(self, record: TransferView) -> None:
        try:
            async with self.slots, asyncio.timeout(self.settings.transfer_timeout_seconds):
                record.status = RunStatus.running
                await self.cache.prune()
                await self._copy(record)
                record.status = RunStatus.finished
        except asyncio.CancelledError:
            record.status = RunStatus.cancelled
        except TimeoutError:
            record.status = RunStatus.timed_out
            record.error_code, record.error = "TRANSFER_TIMEOUT", "文件传输超时"
        except ToolError as exc:
            record.status = RunStatus.failed
            record.error_code, record.error = exc.code, exc.message
        except (OSError, asyncssh.Error):
            record.status = RunStatus.failed
            record.error_code, record.error = (
                "TRANSFER_FAILED",
                "传输失败，请检查文件路径、权限、剩余空间和连接状态",
            )
        finally:
            record.finished_at = datetime.now(UTC)

    async def _copy(self, record: TransferView) -> None:
        self.validate_endpoint(record.source_session_id, record.source_path)
        self.validate_endpoint(record.target_session_id, record.target_path)
        async with AsyncExitStack() as stack:
            source_client = await stack.enter_async_context(self.sftp(record.source_session_id))
            target_client = await stack.enter_async_context(self.sftp(record.target_session_id))
            if source_client:
                attrs = await source_client.stat(record.source_path)
                regular = attrs.type == asyncssh.FILEXFER_TYPE_REGULAR
                directory = attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY
                record.total_bytes = attrs.size
            else:
                local_stat = await asyncio.to_thread(self.local_path(record.source_path).stat)
                regular = stat.S_ISREG(local_stat.st_mode)
                directory = stat.S_ISDIR(local_stat.st_mode)
                record.total_bytes = local_stat.st_size
            if directory and not record.recursive:
                raise ToolError("DIRECTORY_REQUIRES_RECURSIVE", "传输目录需要开启递归复制")
            if not regular and not directory:
                raise ToolError("NOT_REGULAR_FILE", "源路径必须是普通文件或目录")
            if directory:
                record.total_bytes = None
            if not record.overwrite and not directory:
                if target_client:
                    try:
                        await target_client.lstat(record.target_path)
                    except asyncssh.SFTPNoSuchFile:
                        pass
                    else:
                        raise ToolError(
                            "TARGET_EXISTS", "目标文件已存在；请更改路径或明确允许覆盖", 409
                        )
                else:
                    destination = self.local_path(record.target_path)
                    if destination.exists() or destination.is_symlink():
                        raise ToolError(
                            "TARGET_EXISTS", "目标文件已存在；请更改路径或明确允许覆盖", 409
                        )
            if not target_client:
                await asyncio.to_thread(
                    self.local_path(record.target_path).parent.mkdir, parents=True, exist_ok=True
                )

        def progress(copied: int, total: int | None) -> None:
            if copied >= 0:
                record.copied_bytes = copied
            if directory and total is not None:
                record.total_bytes = total

        source_path = (
            record.source_path
            if record.source_session_id
            else str(self.local_path(record.source_path))
        )
        target_path = (
            record.target_path
            if record.target_session_id
            else str(self.local_path(record.target_path))
        )
        if directory:
            source_path = source_path.rstrip("/") + "/"
        if record.source_session_id and record.target_session_id:
            async with self.cache.stage(record.transfer_id) as relay:
                staged = str(relay / "payload")
                record.phase = "downloading"
                await run_rsync(
                    self.registry,
                    "workbench:" + source_path,
                    staged,
                    record.source_session_id,
                    progress,
                    overwrite=True,
                    recursive=directory,
                )
                self.validate_endpoint(record.source_session_id, record.source_path)
                self.validate_endpoint(record.target_session_id, record.target_path)
                record.phase, record.copied_bytes = "uploading", 0
                await run_rsync(
                    self.registry,
                    staged + ("/" if directory else ""),
                    "workbench:" + target_path,
                    record.target_session_id,
                    progress,
                    overwrite=record.overwrite,
                    recursive=directory,
                )
        else:
            record.phase = "downloading" if record.source_session_id else "uploading"
            await run_rsync(
                self.registry,
                ("workbench:" if record.source_session_id else "") + source_path,
                ("workbench:" if record.target_session_id else "") + target_path,
                record.source_session_id or record.target_session_id,
                progress,
                overwrite=record.overwrite,
                recursive=directory,
            )
        if not record.target_session_id and not directory:
            self.local_path(record.target_path).chmod(0o600)
        if record.total_bytes is not None:
            record.copied_bytes = record.total_bytes
        record.phase = "finished"

    async def close(self) -> None:
        self.closing = True
        for transfer_id in list(self.workers):
            await self.cancel(transfer_id)
