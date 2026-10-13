import asyncio
import contextlib
import hashlib
import json
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import asyncssh

from ssh_mcp.config import Settings
from ssh_mcp.errors import ToolError
from ssh_mcp.jobs.output import OutputBuffer
from ssh_mcp.schemas import ExecuteRequest, OutputView, RunStatus, RunView
from ssh_mcp.ssh.registry import SessionRegistry, now
from ssh_mcp.sudo.channel import ControlFilter, ControlMarkers, wrap_plan
from ssh_mcp.sudo.syntax import CommandPlan, context_command, prepare

TERMINAL = {
    RunStatus.finished,
    RunStatus.failed,
    RunStatus.timed_out,
    RunStatus.cancelled,
    RunStatus.disconnected,
}


@dataclass
class Run:
    run_id: str
    request: ExecuteRequest
    plan: CommandPlan
    stdout: OutputBuffer
    stderr: OutputBuffer
    created_at: datetime = field(default_factory=now)
    status: RunStatus = RunStatus.queued
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    signal: str | None = None
    policy_version: int | None = None
    error_code: str | None = None
    error: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[None] | None = None
    process: asyncssh.SSHClientProcess[bytes] | None = None
    slot_allocated: bool = False

    def view(self) -> RunView:
        end = self.finished_at or now()
        duration = int((end - self.started_at).total_seconds() * 1000) if self.started_at else None
        return RunView(
            run_id=self.run_id,
            session_id=self.request.session_id,
            command=self.request.command,
            source="MCP",
            created_at=self.created_at,
            status=self.status,
            exit_code=self.exit_code,
            signal=self.signal,
            started_at=self.started_at,
            finished_at=self.finished_at,
            duration_ms=duration,
            stdout_cursor=len(self.stdout.data),
            stderr_cursor=len(self.stderr.data),
            stdout_truncated=self.stdout.truncated,
            stderr_truncated=self.stderr.truncated,
            sudo_used=self.plan.uses_sudo,
            policy_version=self.policy_version,
            error_code=self.error_code,
            error=self.error,
        )


class TaskService:
    def __init__(self, settings: Settings, registry: SessionRegistry) -> None:
        self.settings, self.registry = settings, registry
        self.runs: dict[str, Run] = {}
        self.queue: deque[str] = deque()
        self.keys: dict[tuple[str, str], tuple[str, str]] = {}
        self.expired: deque[str] = deque(maxlen=1000)
        self.running_by_session: dict[str, int] = {}
        self.running = 0
        self.wake = asyncio.Event()
        self.scheduler: asyncio.Task[None] | None = None
        self.closing = False

    def start(self) -> None:
        self.scheduler = asyncio.create_task(self._schedule())

    def _expire(self, run_id: str) -> None:
        self.runs.pop(run_id, None)
        self.expired.append(run_id)
        self.keys = {k: v for k, v in self.keys.items() if v[0] != run_id}

    def cleanup(self) -> None:
        for r in list(self.runs.values()):
            if (
                r.finished_at
                and (now() - r.finished_at).total_seconds() > self.settings.task_retention_seconds
            ):
                self._expire(r.run_id)

    def get(self, run_id: str) -> Run:
        self.cleanup()
        if run_id in self.expired:
            raise ToolError("OUTPUT_EXPIRED", "任务和输出已按保留/记录上限清理", 410)
        r = self.runs.get(run_id)
        if r is None:
            raise ToolError("UNKNOWN_RUN", "任务不存在", 404)
        return r

    async def submit(self, request: ExecuteRequest) -> Run:
        if self.closing:
            raise ToolError("SHUTTING_DOWN", "后端正在关闭", 503)
        self.cleanup()
        s = self.registry.get(request.session_id)
        s.require_connection()
        timeout = request.timeout_seconds or self.settings.default_timeout_seconds
        if timeout > self.settings.max_timeout_seconds:
            raise ToolError("INVALID_ARGUMENT", "timeout 超出配置最大值")
        request.timeout_seconds = timeout
        signature = hashlib.sha256(
            json.dumps(request.model_dump(exclude={"idempotency_key"}), sort_keys=True).encode()
        ).hexdigest()
        key = (request.session_id, request.idempotency_key) if request.idempotency_key else None
        if key and key in self.keys:
            old_id, old_signature = self.keys[key]
            if old_signature != signature:
                raise ToolError("IDEMPOTENCY_CONFLICT", "同一幂等键已用于不同命令/参数", 409)
            return self.get(old_id)
        plan = prepare(request.command)
        context_command(plan.command, request.cwd, request.env)
        if plan.uses_sudo and not s.sudo_enabled:
            raise ToolError("SUDO_NOT_AUTHORIZED", "此连接未启用 sudo；整条命令未执行", 403)
        if len(self.queue) >= self.settings.max_queue:
            raise ToolError("QUEUE_LIMIT", "等待队列已满，请稍后重试", 429)
        if len(self.runs) >= self.settings.max_task_records:
            completed = sorted(
                (r for r in self.runs.values() if r.finished_at),
                key=lambda r: r.finished_at or r.created_at,
            )
            if not completed:
                raise ToolError("TASK_RECORD_LIMIT", "所有记录均在等待/运行中，无法接收新任务", 429)
            self._expire(completed[0].run_id)
        r = Run(
            secrets.token_urlsafe(18),
            request,
            plan,
            OutputBuffer(self.settings.max_output_bytes),
            OutputBuffer(self.settings.max_output_bytes),
        )
        self.runs[r.run_id] = r
        if key:
            self.keys[key] = (r.run_id, signature)
        self.queue.append(r.run_id)
        self.wake.set()
        return r

    async def quick_result(self, r: Run) -> RunView:
        # HTTP/MCP request cancellation never cancels the background worker.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(2):
                await r.done.wait()
        return r.view()

    def read_output(
        self, run_id: str, stdout_cursor: int = 0, stderr_cursor: int = 0, limit: int = 65536
    ) -> OutputView:
        r = self.get(run_id)
        complete = r.status in TERMINAL
        stdout, next_stdout = r.stdout.read(stdout_cursor, limit, complete)
        stderr, next_stderr = r.stderr.read(stderr_cursor, limit, complete)
        return OutputView(
            run_id=run_id,
            stdout=stdout,
            stderr=stderr,
            next_stdout_cursor=next_stdout,
            next_stderr_cursor=next_stderr,
            complete=complete,
            stdout_truncated=r.stdout.truncated,
            stderr_truncated=r.stderr.truncated,
        )

    async def _schedule(self) -> None:
        while not self.closing:
            self.cleanup()
            self.wake.clear()
            for run_id in list(self.queue):
                r = self.runs[run_id]
                session_id = r.request.session_id
                if self.running >= self.settings.max_global_runs:
                    break
                if self.running_by_session.get(session_id, 0) >= self.settings.max_session_runs:
                    continue
                self.queue.remove(run_id)
                self.running += 1
                r.slot_allocated = True
                self.running_by_session[session_id] = self.running_by_session.get(session_id, 0) + 1
                r.worker = asyncio.create_task(self._run(r))
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(0.5):
                    await self.wake.wait()

    async def _run(self, r: Run) -> None:
        password: str | None = None
        try:
            async with asyncio.timeout(
                r.request.timeout_seconds or self.settings.default_timeout_seconds
            ):
                s = self.registry.get(r.request.session_id)
                # Policy mutation and channel creation share the same lock. Password is a task-local snapshot.
                async with s.lock:
                    conn = s.require_connection()
                    if r.plan.uses_sudo and not s.sudo_enabled:
                        raise ToolError(
                            "SUDO_NOT_AUTHORIZED", "排队期间 sudo 已关闭；整条命令未执行", 403
                        )
                    r.policy_version = s.sudo_version
                    password = s.sudo_password if r.plan.uses_sudo else None
                    markers = ControlMarkers(secrets.token_hex(16))
                    command = wrap_plan(r.plan, markers) if r.plan.uses_sudo else r.plan.command
                    command = context_command(command, r.request.cwd, r.request.env)
                    r.process = await conn.create_process(command, encoding=None, request_pty=False)
                    if not r.plan.uses_sudo:
                        r.process.stdin.write_eof()
                    r.status, r.started_at = RunStatus.running, now()
                    s.last_activity_at = now()
                process = r.process
                control = ControlFilter(markers)
                attempts = 0

                async def stdout() -> None:
                    while chunk := await process.stdout.read(16384):
                        r.stdout.append(chunk)

                async def stderr() -> None:
                    nonlocal attempts
                    while chunk := await process.stderr.read(16384):
                        if not r.plan.uses_sudo:
                            r.stderr.append(chunk)
                            continue
                        visible, events = control.events(chunk)
                        r.stderr.append(visible)
                        for event in events:
                            if event == "start":
                                attempts = 0
                            elif event == "prompt":
                                if password is None:
                                    raise ToolError(
                                        "SUDO_PASSWORD_NOT_CONFIGURED",
                                        "sudo 需要密码，请在网页配置",
                                    )
                                if attempts:
                                    raise ToolError(
                                        "SUDO_AUTHENTICATION_FAILED",
                                        "sudo 密码认证失败；只尝试一次",
                                    )
                                attempts += 1
                                process.stdin.write((password + "\n").encode())
                                await process.stdin.drain()
                            elif event == "failed":
                                raise ToolError(
                                    "SUDO_AUTHENTICATION_FAILED",
                                    "sudo 验证被服务器拒绝，请检查 sudoers/密码策略",
                                )
                            elif event == "policy":
                                raise ToolError(
                                    "SUDO_POLICY_UNSUPPORTED",
                                    "服务器未保留同一 shell 的 sudo 授权；请检查 timestamp_timeout=0 等 sudoers 策略",
                                )
                    if r.plan.uses_sudo:
                        visible, _ = control.events(b"", final=True)
                        r.stderr.append(visible)

                readers = [asyncio.create_task(stdout()), asyncio.create_task(stderr())]
                try:
                    await asyncio.gather(*readers, process.wait_closed())
                finally:
                    for reader in readers:
                        if not reader.done():
                            reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                r.exit_code = process.exit_status
                r.signal = process.exit_signal[0] if process.exit_signal else None
                if conn.is_closed():
                    r.status, r.error_code, r.error = (
                        RunStatus.disconnected,
                        "SSH_DISCONNECTED",
                        "SSH 已掉线",
                    )
                elif r.exit_code in {None, -1} and r.signal is None:
                    r.status, r.error_code, r.error = (
                        RunStatus.failed,
                        "SSH_EXIT_STATUS_MISSING",
                        "远端通道结束但未报告退出码或信号",
                    )
                else:
                    r.status = RunStatus.finished
        except TimeoutError:
            r.status, r.error_code, r.error = RunStatus.timed_out, "TIMEOUT", "远程任务超时"
        except asyncio.CancelledError:
            r.status = RunStatus.cancelled
        except ToolError as e:
            r.status, r.error_code, r.error = RunStatus.failed, e.code, e.message
            if e.code in {"SESSION_UNAVAILABLE", "UNKNOWN_SESSION"}:
                r.status = RunStatus.disconnected
        except (OSError, asyncssh.Error):
            r.status, r.error_code, r.error = RunStatus.disconnected, "SSH_FAILURE", "SSH 通道故障"
        except Exception:  # noqa: BLE001 -- Fail safely without leaking remote exception secrets.
            r.status, r.error_code, r.error = RunStatus.failed, "INTERNAL_ERROR", "任务服务错误"
        finally:
            password = None
            if r.process:
                if r.status != RunStatus.finished:
                    with contextlib.suppress(OSError, asyncssh.Error):
                        r.process.terminate()
                        r.process.kill()
                    r.process.close()
                with contextlib.suppress(TimeoutError, OSError, asyncssh.Error):
                    async with asyncio.timeout(1):
                        await r.process.wait_closed()
                r.process = None
            self._finish(r)

    def _finish(self, r: Run) -> None:
        if r.done.is_set():
            return
        r.finished_at = now()
        r.done.set()
        if r.slot_allocated:
            self.running -= 1
            session_id = r.request.session_id
            self.running_by_session[session_id] -= 1
            if not self.running_by_session[session_id]:
                self.running_by_session.pop(session_id)
            r.slot_allocated = False
        self.wake.set()

    async def cancel(self, run_id: str) -> RunView:
        r = self.get(run_id)
        if r.status in TERMINAL:
            return r.view()
        if r.run_id in self.queue:
            self.queue.remove(r.run_id)
            r.status = RunStatus.cancelled
            self._finish(r)
        elif r.worker:
            r.worker.cancel()
            await asyncio.gather(r.worker, return_exceptions=True)
            if not r.done.is_set():
                r.status = RunStatus.cancelled
                self._finish(r)
        self.wake.set()
        return r.view()

    async def close(self) -> None:
        self.closing = True
        if self.scheduler:
            self.scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.scheduler
        await asyncio.gather(
            *(self.cancel(r.run_id) for r in list(self.runs.values()) if r.status not in TERMINAL)
        )
        self.runs.clear()
        self.keys.clear()
