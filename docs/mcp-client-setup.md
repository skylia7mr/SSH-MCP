# MCP 客户端接入

先启动并在网页建立 SSH 连接，再从“Codex / MCP 接入”取得首次 token。独立 HTTP endpoint 为 `http://127.0.0.1:8000/mcp`，不是 Vite 5173；根据配置修改端口。token 只用于 Authorization Bearer，不能放 URL、localStorage 或普通日志。

## Codex 配置

以下格式已按 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 核对。客户端需继承 `SSH_MCP_CLIENT_TOKEN` 环境变量。

```bash
# 在本机受控终端读取，不把真实 token 写入命令历史
read -rs -p 'MCP token: ' SSH_MCP_CLIENT_TOKEN
export SSH_MCP_CLIENT_TOKEN
codex
```

在 `~/.codex/config.toml` 添加：

```toml
[mcp_servers.ssh_workbench]
url = "http://127.0.0.1:8000/mcp"
bearer_token_env_var = "SSH_MCP_CLIENT_TOKEN"
startup_timeout_sec = 15
tool_timeout_sec = 15

[mcp_servers.ssh_workbench.tools.ssh_execute]
approval_mode = "approve"

[mcp_servers.ssh_workbench.tools.ssh_cancel_run]
approval_mode = "approve"
```

两个 `approval_mode` 为本服务执行/取消工具提供客户端预授权，符合网页建连后普通命令无需逐条审批的使用方式；服务端仍以真实非只读、可能破坏的工具标注公开能力。实际 Codex CLI 0.159.2 在本机 read-only 本地沙箱模式下，使用此配置经 HTTP 完成发现与远端执行，验证 stdout 与退出码；未修改全局客户端权限。

适用于支持 Streamable HTTP 的其他 MCP 客户端：配置同一 URL，并以 header 传 token。后端不固定某个协议版本，使用 SDK 协商。实测官方 Python SDK 客户端通过 HTTP 协商 `2025-11-25`，完成 initialize、tools/list、tools/call。Codex 的实际验收状态以 [实施记录](implementation.md) 为准，不把仅协议通过描述为客户端通过。

## 六个工具

| 工具 | 关键参数 | 行为 |
| --- | --- | --- |
| ssh_list_sessions | 无 | 连接元数据、真实状态、sudo_enabled，无密码 |
| ssh_get_session | session_id | 当前连接与 sudo 配置状态 |
| ssh_execute | session_id, command, timeout_seconds?, cwd?, env?, idempotency_key? | 先完整预检查，约等待 2 秒；长任务返回 run_id |
| ssh_get_run | run_id | 状态、时间、退出码/信号、错误码、输出最终字节位置 |
| ssh_read_output | run_id, stdout_cursor?, stderr_cursor?, limit? | 每流增量输出及下一字节游标，完成与截断标志 |
| ssh_cancel_run | run_id | 尽力取消，返回实际本地终态 |

先发现并核对目标 `session_id`，不要仅按列表位置猜测账号。每次执行独立 channel。cwd 和 env 只影响本次，安全 shell 引用；env 最多 64 项、值最多 4096 字节。请求幂等键作用域是同连接、同进程、记录保留期；相同参数同键返回同任务，不同参数同键报 `IDEMPOTENCY_CONFLICT`。

游标是保留缓冲的原始字节偏移，首次 0，后续使用返回的 next 游标；limit 每流 4..65536。UTF-8 不从字符中间拆分，未完成尾部字符等待下一块，非法字节用替换字符。不得把字符数当字节游标。`complete=true` 表示任务已终止，仍需读到最终 cursor；截断时只有上限内输出可读。重复相同游标读取相同已保留范围。过期/容量淘汰返回 `OUTPUT_EXPIRED`，很旧的 ID 超出有限提示记录后为 `UNKNOWN_RUN`。

远端非零退出码是正常执行结果 `finished`，SDK `isError=false`。预检查/参数/权限错误和快速执行的系统失败使用 SDK `isError=true`；后台终态需通过 get_run 读取 error_code。长任务可返回 queued/running，不表示成功。MCP 请求取消或 token 轮换不取消已经提交的任务；需要显式 cancel_run 或网页取消。

## 轮换与排障

网页轮换后旧 token 立即失效，已有 SSE/请求会结束；更新客户端环境变量并重新启动/重连。旧 token 无法从网页回填。固定 token 轮换仅进程内有效，修改环境变量后重启才能保持新值。

401：检查 token 与重启/轮换；403：检查 sudo 网页策略；无连接：先网页登录；语法错误：参见 [sudo 支持矩阵](sudo-support.md)。GET `/mcp` 可为持续 SSE，不能把等待整个响应体当成初始化失败；应使用官方 MCP 客户端完成协议。CLI 启动本工具时不能另建一份 stdio 连接状态。
