import asyncio
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import asyncssh

from ssh_mcp.errors import ToolError
from ssh_mcp.ssh.registry import SessionRegistry


async def run_rsync(
    registry: SessionRegistry,
    source: str,
    target: str,
    session_id: str | None,
    progress: Callable[[int, int | None], None],
    *,
    overwrite: bool,
    recursive: bool,
) -> None:
    executable = shutil.which("rsync")
    if not executable:
        raise ToolError("RSYNC_UNAVAILABLE", "工作台未安装 rsync，请安装后重试")
    workers: set[asyncio.Task[None]] = set()
    remote_errors = bytearray()
    accepted = False

    async def bridge(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accepted
        remote: asyncssh.SSHClientProcess[bytes] | None = None
        pumps: list[asyncio.Task[None]] = []
        try:
            if accepted or session_id is None:
                return
            accepted = True
            arguments = json.loads(await reader.readline())
            if (
                not isinstance(arguments, list)
                or not all(
                    isinstance(argument, str) and "\x00" not in argument for argument in arguments
                )
                or arguments[:2] != ["rsync", "--server"]
            ):
                raise ToolError("RSYNC_PROTOCOL_ERROR", "无效的 rsync 服务端请求")
            session = registry.get(session_id)
            async with session.lock:
                registry.get(session_id)
                connection = session.require_connection()
                remote = await connection.create_process(shlex.join(arguments), encoding=None)

            async def upload() -> None:
                assert remote is not None
                while chunk := await reader.read(262144):
                    remote.stdin.write(chunk)
                    await remote.stdin.drain()
                remote.stdin.write_eof()

            async def download() -> None:
                assert remote is not None
                while chunk := await remote.stdout.read(262144):
                    writer.write(chunk)
                    await writer.drain()

            async def errors() -> None:
                assert remote is not None
                while chunk := await remote.stderr.read(4096):
                    remote_errors.extend(chunk)
                    del remote_errors[:-8192]

            pumps = [asyncio.create_task(operation()) for operation in (upload, download, errors)]
            await asyncio.gather(pumps[1], pumps[2])
            await remote.wait_closed()
        except (OSError, asyncssh.Error, ValueError, ToolError):
            remote_errors.extend(b" remote rsync channel failed")
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            if remote:
                remote.close()
                await remote.wait_closed()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        worker = asyncio.create_task(bridge(reader, writer))
        workers.add(worker)
        worker.add_done_callback(workers.discard)

    with tempfile.TemporaryDirectory(prefix="ssh-mcp-rsync-") as directory:
        socket_path = str(Path(directory) / "transport.sock")
        server = await asyncio.start_unix_server(accept, socket_path, limit=65536)
        process: asyncio.subprocess.Process | None = None
        readers: list[asyncio.Task[None]] = []
        try:
            arguments = [
                executable,
                "--times",
                "--checksum",
                "--protect-args",
                "--outbuf=L",
                "--info=progress2",
                "--no-human-readable",
                "--stats",
                "--no-inc-recursive",
            ]
            if recursive:
                arguments.extend(["--recursive", "--links", "--safe-links"])
            if not overwrite:
                arguments.append("--ignore-existing")
            if session_id:
                helper = Path(__file__).with_name("rsync_bridge.py")
                arguments.extend(["--rsh", shlex.join([sys.executable, str(helper), socket_path])])
            arguments.extend(["--", source, target])
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "LC_ALL": "C"},
            )
            assert process.stdout is not None and process.stderr is not None
            errors_local = bytearray()

            async def output() -> None:
                assert process is not None and process.stdout is not None
                pending = ""
                while chunk := await process.stdout.read(4096):
                    pending += chunk.decode("utf-8", errors="replace")
                    lines = re.split(r"[\r\n]", pending)
                    pending = lines.pop()
                    for line in lines:
                        match = re.match(r"\s*([\d,]+)\s+(\d+)%", line)
                        if match:
                            copied, percent = (
                                int(value.replace(",", "")) for value in match.groups()
                            )
                            progress(copied, copied * 100 // percent if percent else None)
                        total = re.match(r"Total file size: ([\d,]+) bytes", line)
                        if total:
                            progress(-1, int(total.group(1).replace(",", "")))

            async def errors() -> None:
                assert process is not None and process.stderr is not None
                while chunk := await process.stderr.read(4096):
                    errors_local.extend(chunk)
                    del errors_local[:-8192]

            readers = [asyncio.create_task(output()), asyncio.create_task(errors())]
            await asyncio.gather(*readers)
            code = await process.wait()
            if code:
                diagnostic = bytes(remote_errors + errors_local)
                if b"not found" in diagnostic or b"command not found" in diagnostic:
                    raise ToolError("RSYNC_UNAVAILABLE", "远程服务器未安装或无法运行 rsync")
                raise ToolError(
                    "RSYNC_FAILED",
                    f"rsync 传输失败（退出码 {code}），请检查路径、权限、空间及连接状态",
                )
        finally:
            if process and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    async with asyncio.timeout(2):
                        await process.wait()
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            server.close()
            await server.wait_closed()
            for worker in list(workers):
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
