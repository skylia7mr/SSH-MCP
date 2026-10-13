# 交付报告（2026-10-13）

系统已完成真实实现与验收，支持中文网页管理多账号 SSH，Codex/兼容 MCP 客户端执行远端命令，网页持续查看真实任务输出。六阶段代码/部署/文档提交均在当前本地 Git 历史中；未 push。

## 启动与接入

```bash
uv sync --locked --group dev
cd frontend && npm ci && npm run build && cd ..
uv run python -m ssh_mcp
```

网页：http://127.0.0.1:8000/ 。MCP：http://127.0.0.1:8000/mcp 。健康：`/healthz`。替代运行路径为 `docker compose up --build --wait`，宿主只发布 loopback；停止为 `docker compose down`。

启动终端给出一次性配对码，网页登录后确认主机指纹并连接。SSH/sudo 默认分开，sudo 每连接默认关闭。网页“Codex / MCP 接入”首次显示/轮换 token；[实际验证的配置](mcp-client-setup.md) 使用 token 环境变量及两个工具的限定预授权。真实 Codex CLI 0.159.2 已完成发现、精确 session 的执行与输出/退出码验证。验收服务均已停止，启动时会生成自己的配对码和内存状态。

## 实际检查

| 检查 | 结果 |
| --- | --- |
| uv 锁文件离线恢复 | 成功，61 resolved / 58 当前平台包 |
| Ruff check / format check | 通过，42 Python 文件 |
| mypy | 通过，27 源文件 |
| 前端类型检查与生产构建 | 通过，34 modules；JS 241.56 kB |
| 最终全量：单元、真实 OpenSSH/sudo、官方 MCP HTTP、协议故障、浏览器和 Codex | **63 passed，161.35 秒，无跳过** |
| 真实 Codex CLI 修正 stdin/超时清理后的复核 | 1 passed，44.43 秒 |
| 独立 sudo 输入分离实验 | 24/24，Bash/dash、普通密码/NOPASSWD |
| Docker Compose 构建、启动、healthy、生产网页 | 通过，最终代码镜像 |
| 生产容器配对与官方 MCP 六工具发现/调用 | 通过 |
| Web SSE 打开时生产 Compose 关闭 | 通过，2.17 秒，无需先关浏览器 |
| 精确本地 uv 单服务启动/健康/网页/认证/退出 | 通过 |
| GitHub 托管 CI | 已提供工作流；未 push，尚未在 GitHub 执行 |

所有 SSH/sudo 测试仅使用随机密码的隔离本地测试容器和协议 fixture，不接触用户服务器。两账号并行、加密私钥、错误凭据/口令、公钥变化及探测认证竞态、超时、掉线、真实 stdout/stderr、非零退出、长任务、UTF-8 游标、大输出每流 1 MiB、队列/记录/保留限额、取消和关闭均已覆盖。

sudo 验证默认整条拒绝、连接隔离、正确/错误/无密码、NOPASSWD、缓存、原位置管道/重定向、多个 sudo、括号和命令替换、command/env 前缀及排队/运行中策略切换。核心 tee 测试文件只有 pipe 数据，sentinel 不在命令/结果/浏览器持久存储/sshd 日志中。

两项真实浏览器 E2E 覆盖完整操作链、刷新、加密私钥、实时输出和保持滚动视角、390px 深色，以及浏览器保持打开时停机/重启重新配对。实际截图在 [桌面](previews/workbench-actual-desktop.png) 与 [窄屏](previews/workbench-actual-mobile-dark.png)。

试验过程的失败已保留在 [实施记录](implementation.md)：早期 sshd readiness 重试修复、Codex 默认工具审批与配置修正、CLI 一次超过旧整体时限后关闭 stdin/修复清理并复测通过、浏览器下载续传、持续 SSE 停机修复、uv Ctrl-C 正常退出以及畸形认证输入正常拒绝。没有将失败或未运行项算为通过。

## 已明确的限制

- 单本机操作者、单进程；不支持公网、多 worker、交互终端、Windows、跳板机或文件管理。
- 只有用户确认的主机公钥持久保存；重启后凭据、连接、策略、任务/输出消失，不自动恢复或重放。释放引用不保证物理内存擦除。
- sudo 使用保守 POSIX AST 子集，实测 Bash/dash，不声称完整 Bash。并行管道内多个 sudo、动态命令、shell -c、交互选项及不兼容 sudoers 策略明确拒绝。完整 [支持矩阵](sudo-support.md)。
- 开关不构成主机权限隔离；脚本间接提权与 root 权限仍由远端账号/sudoers 决定。取消尽力终止，无法保证脱离 shell 的后代停止。
- 输出硬上限/保留期之外不可恢复；用户命令可能主动输出秘密，不能通用脱敏。客户端模型调用依赖实际账号/网络，CLI 验证是可选独立验收，不作为 CI 的账户依赖。

[架构](architecture.md)、[默认配置](configuration.md)、[操作排障](operations.md)、[客户端接入](mcp-client-setup.md)、[阶段提交与日志摘要](implementation.md) 已补齐。
