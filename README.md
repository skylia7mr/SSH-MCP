# SSH MCP 本机工作台

在中文网页中建立多个真实 SSH 连接，由 Codex 或兼容 MCP 客户端选择 `session_id` 执行命令，网页实时查看任务、stdout、stderr 和退出状态。网页管理连接与 sudo，不提供命令输入框。

只支持本机单操作者、单后端进程。密码、私钥、口令、连接、任务、输出仅存内存；重启后重新配对、登录。唯一持久业务数据是显式确认的主机公钥 `known_hosts`。远端不安装本项目程序。项目使用 GPL-3.0，包含 GPL 兼容的 bashlex 解析器。

## 本地安装与启动

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 22 和 npm。远端需要 Linux sshd、POSIX shell；sudo 功能需要 `/usr/bin/sudo` 和支持的 sudoers 策略。实测 Bash 5.2、dash、OpenSSH 和 sudo 1.9.13；详见 [sudo 支持范围](docs/sudo-support.md)。

```bash
uv sync --locked --group dev
cd frontend
npm ci
npm run build
cd ..
uv run python -m ssh_mcp
```

最后一条命令同时提供已构建网页与后端，不需要 Vite。打开 **http://127.0.0.1:8000/**，输入启动终端的一次性配对码（默认 5 分钟、最多 5 次尝试）。点击“新建连接”，填写认证材料，核对并确认 SHA256 主机指纹。sudo 默认关闭，需要逐连接开启并单独配置密码。

需要调整配置时复制 `.env.example` 为 `.env`；不要提交实际 token。默认 MCP 地址为 **http://127.0.0.1:8000/mcp**，健康检查为 `/healthz`。

## Codex / MCP 接入

网页“Codex / MCP 接入”首次显示 token；关闭后不会回填，可轮换。先在启动 Codex 的同一受控终端设置 `SSH_MCP_CLIENT_TOKEN`，再将以下配置加入 `~/.codex/config.toml`：

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

让客户端先调用 `ssh_list_sessions`，选择连接后调用 `ssh_execute`。约 2 秒内未结束返回 `run_id`，再调用 `ssh_get_run` / `ssh_read_output`，需要取消时调用 `ssh_cancel_run`。完整工具、token 轮换与实测状态见 [客户端接入](docs/mcp-client-setup.md) 和 [验收记录](docs/implementation.md)。

## 开发与 Docker

开发分别运行 `uv run python -m ssh_mcp` 和 `cd frontend && npm run dev`；打开 http://127.0.0.1:5173/。Vite 只代理 API 与健康检查，MCP 客户端仍连接后端 8000 端口。生产请使用上述前端构建方式。

```bash
docker compose up --build --wait
# 从受控启动日志读取一次性配对码
docker compose logs --no-log-prefix workbench
docker compose down
```

Compose 只向宿主机 `127.0.0.1` 发布端口；生产容器以非 root 用户运行，不包含测试 sshd。命名卷只存主机信任公钥，停机不恢复连接或任务。不要使用多个 worker 或公网反向代理。容器中连接宿主机 SSH 时，主机地址需是容器可达地址，不能把容器自身的 `127.0.0.1` 当作宿主机。

## 验证

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
cd frontend && npm run typecheck && npm run build && cd ..
uv run pytest -q                 # 默认跳过 Docker 真实环境用例
# 完整集成和浏览器验收，仅使用测试容器，不接触生产服务器
uv run playwright install --with-deps chromium
docker build -t ssh-mcp-test-sshd:local tests/sshd
SSH_MCP_REAL_TESTS=1 uv run pytest -q
```

[架构](docs/architecture.md) · [配置默认值](docs/configuration.md) · [操作与排障](docs/operations.md) · [sudo 语法与边界](docs/sudo-support.md)。实际通过的检查与阶段提交记录在 [交付报告](docs/acceptance-report.md) 和 [实施记录](docs/implementation.md)。
