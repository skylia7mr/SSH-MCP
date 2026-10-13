import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, ValidationError

from ssh_mcp.errors import ToolError
from ssh_mcp.schemas import ExecuteRequest
from ssh_mcp.services import Services


def result(payload: dict[str, Any], error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=error,
    )


async def guarded(
    operation: Callable[[], Awaitable[dict[str, Any]]], execution: bool = False
) -> CallToolResult:
    try:
        payload = await operation()
        return result(
            payload,
            error=execution and payload.get("status") in {"failed", "timed_out", "disconnected"},
        )
    except ToolError as e:
        return result({"error_code": e.code, "error": e.message}, error=True)
    except ValidationError:
        return result({"error_code": "INVALID_ARGUMENT", "error": "参数校验失败"}, error=True)


def build_mcp(s: Services) -> MCPServer:
    mcp = MCPServer(
        "SSH MCP",
        version="0.1.0",
        log_level="ERROR",
        subscriptions=False,
        instructions="发现网页已连接的 SSH 会话，使用 session_id 执行命令。sudo 只能由网页操作者授权。长任务用 run_id 查询、读取输出。",
    )
    readonly = ToolAnnotations(read_only_hint=True, destructive_hint=False)

    @mcp.tool(annotations=readonly, structured_output=False)
    async def ssh_list_sessions() -> CallToolResult:
        """列出当前 SSH 连接元数据和每连接 sudo 状态；不返回凭据，不建立连接。"""
        return result({"sessions": [v.model_dump(mode="json") for v in s.registry.list()]})

    @mcp.tool(annotations=readonly, structured_output=False)
    async def ssh_get_session(
        session_id: Annotated[
            str, Field(description="网页连接列表中的 session_id", min_length=1, max_length=128)
        ],
    ) -> CallToolResult:
        """查询指定 SSH 连接的实时状态和 sudo 配置状态，不返回密码。"""

        async def operation() -> dict[str, Any]:
            return s.registry.get(session_id).view().model_dump(mode="json")

        return await guarded(operation)

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
        structured_output=False,
    )
    async def ssh_execute(
        session_id: Annotated[
            str, Field(description="目标连接 session_id", min_length=1, max_length=128)
        ],
        command: Annotated[
            str,
            Field(
                description="远端 POSIX shell 命令；独立 exec channel。支持子集见 sudo-support 文档",
                min_length=1,
                max_length=131072,
            ),
        ],
        timeout_seconds: Annotated[
            int | None,
            Field(description="运行超时，默认60秒，最大3600（可配置更低上限）", ge=1, le=3600),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(description="本次命令工作目录，安全引用；不会延续到下次", max_length=4096),
        ] = None,
        env: Annotated[
            dict[str, str] | None,
            Field(description="本次命令环境变量，最多64项，名称为合法shell变量名"),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                description="同一连接、当前进程和记录保留期内去重；不同参数同键报冲突",
                min_length=1,
                max_length=128,
            ),
        ] = None,
    ) -> CallToolResult:
        """提交远端命令，约等待2秒。长任务返回run_id和queued/running；继续用读取工具。请求取消不取消后台任务。非零退出是正常结果。sudo 未授权时整条命令不执行。"""

        async def operation() -> dict[str, Any]:
            run = await s.tasks.submit(
                ExecuteRequest(
                    session_id=session_id,
                    command=command,
                    timeout_seconds=timeout_seconds,
                    cwd=cwd,
                    env=env,
                    idempotency_key=idempotency_key,
                )
            )
            view = await s.tasks.quick_result(run)
            payload = view.model_dump(mode="json")
            payload["output"] = s.tasks.read_output(run.run_id).model_dump(mode="json")
            return payload

        return await guarded(operation, execution=True)

    @mcp.tool(annotations=readonly, structured_output=False)
    async def ssh_get_run(
        run_id: Annotated[
            str, Field(description="ssh_execute 返回的 run_id", min_length=1, max_length=128)
        ],
    ) -> CallToolResult:
        """查询queued/running/finished/failed/timed_out/cancelled/disconnected、退出码/信号及时间。输出独立读取。"""

        async def operation() -> dict[str, Any]:
            return s.tasks.get(run_id).view().model_dump(mode="json")

        return await guarded(operation)

    @mcp.tool(annotations=readonly, structured_output=False)
    async def ssh_read_output(
        run_id: Annotated[str, Field(description="任务 run_id", min_length=1, max_length=128)],
        stdout_cursor: Annotated[
            int, Field(description="stdout原始字节游标，首次为0；后续使用返回值", ge=0)
        ] = 0,
        stderr_cursor: Annotated[
            int, Field(description="stderr原始字节游标，首次为0；后续使用返回值", ge=0)
        ] = 0,
        limit: Annotated[
            int,
            Field(
                description="每个流本次最多读取的原始字节数（4..65536）；UTF-8不拆字符，非法字节替换",
                ge=4,
                le=65536,
            ),
        ] = 65536,
    ) -> CallToolResult:
        """读取分段stdout/stderr、下一游标、完成与硬截断标记。重复游标读取相同保留范围；过期返回OUTPUT_EXPIRED。complete表示任务终止，仍须读至最后游标。"""

        async def operation() -> dict[str, Any]:
            return s.tasks.read_output(run_id, stdout_cursor, stderr_cursor, limit).model_dump(
                mode="json"
            )

        return await guarded(operation)

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
        structured_output=False,
    )
    async def ssh_cancel_run(
        run_id: Annotated[str, Field(description="欲取消的 run_id", min_length=1, max_length=128)],
    ) -> CallToolResult:
        """尽力发送信号、关闭SSH channel并回收本地任务。返回实际状态；不保证所有脱离shell的远端后代进程停止。"""

        async def operation() -> dict[str, Any]:
            return (await s.tasks.cancel(run_id)).model_dump(mode="json")

        return await guarded(operation)

    return mcp
