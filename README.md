# SSH MCP 本机工作台

在中文网页中管理 SSH 连接，由 Codex 或兼容 MCP 客户端执行命令、传输文件，网页实时查看命令输出。支持通过 MCP 在本地与远程、远程与远程传文件；服务器之间无需互通或共享密钥。网页管理连接与 sudo，不提供命令输入框或文件传输界面。

只支持本机单操作者、单后端进程。连接别名、地址、端口、用户名、认证方式和私钥保存在本机 `connections.json`（权限 0600）；密码、私钥口令、活动连接、任务和输出仅存内存。重启后连接列表仍在，点击“重连”只需补充密码或私钥口令。主机公钥保存到 `known_hosts`；文件目录和中转缓存默认位于临时目录，Docker 不使用文件持久卷。远端不安装本项目程序；文件浏览需要 SFTP 子系统，文件传输需要两端已有 rsync。项目使用 GPL-3.0，包含 GPL 兼容的 bashlex 解析器。

## 本地安装与启动

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 22、npm 和本机 rsync（Debian/Ubuntu 可运行 `sudo apt-get install rsync`；Docker 镜像已包含）。远端需要 Linux sshd、POSIX shell 和用于传输的 rsync；sudo 功能需要 `/usr/bin/sudo` 和支持的 sudoers 策略。实测 Bash 5.2、dash、OpenSSH 和 sudo 1.9.13；详见 [sudo 支持范围](docs/sudo-support.md)。

```bash
uv sync --locked --group dev
cd frontend
npm ci
npm run build
cd ..
uv run python -m ssh_mcp
```

最后一条命令同时提供已构建网页与后端，不需要 Vite。打开 **http://127.0.0.1:8000/**，输入启动终端的一次性配对码（默认 5 分钟、最多 5 次尝试）。点击“新建连接”，填写认证材料，核对并确认 SHA256 主机指纹。sudo 默认关闭，逐连接开启后复用登录密码；私钥登录可选填账户密码用于 sudo，无密码时仅适用于远端 NOPASSWD 策略。断开或重启后 sudo 恢复关闭。

点击“编辑连接”可以修改别名、地址、账号和认证方式，替换私钥，补填或清除账户密码；私钥登录后也可在这里补填 sudo 使用的账户密码。密码留空保留当前内存值。保存后生成基于别名、纳秒时间和随机量的 SHA256 连接 ID，旧 ID 立即失效，关联命令与传输取消；改地址、账号或认证材料会断开连接。断开保留配置，可重连；删除会断开并移除保存的配置。MCP 只接受 session_id，不能按别名执行；遇到 UNKNOWN_SESSION 必须停止，不能自动换 ID 重试。

文件操作仅通过 MCP 的 `ssh_list_files`、`ssh_transfer_file`、`ssh_get_transfer`、`ssh_cancel_transfer` 完成，网页没有浏览、上传、下载或传输接口。使用 rsync，支持文件、递归目录、进度查询和取消；目录复制源目录内容，不删除目标多余文件，默认跳过已有文件，明确允许覆盖后才更新。复用已认证 SSH channel，不另设传输密码，也不把密码放进命令行。服务器之间通过容器内 `/tmp/ssh-mcp-relay` 分两段传输，按一次传输一个目录保留最近 7 次缓存（失败或取消的批次也计数）；每次传输开始检查，批次建立和结束时也检查，超出的直接删除，正在使用的目录保留至传输结束再检查。可设置 `SSH_MCP_TRANSFER_CACHE_KEEP` 调整数量。缓存不挂载 Docker 卷，重建容器即清空；本地源文件和指定下载目标不参与中转缓存淘汰。普通 shell 命令保留远端语法，支持循环、函数、算术展开；可解析的 sudo 调用自动认证，详见支持矩阵。

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

[mcp_servers.ssh_workbench.tools.ssh_transfer_file]
approval_mode = "approve"

[mcp_servers.ssh_workbench.tools.ssh_cancel_transfer]
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

更新代码后需运行 `docker compose up -d --build --wait` 重建并替换容器，再刷新网页；`docker compose restart` 只会重启旧镜像。使用本机 uv 启动时，前端代码修改后需重新 `npm run build`。

Compose 只向宿主机 `127.0.0.1` 发布端口；生产容器以非 root 用户运行，不包含测试 sshd。唯一的 `host-trust` 卷保存主机公钥和连接配置；MCP 本地文件目录为 `/tmp/ssh-mcp-files`，中转缓存为 `/tmp/ssh-mcp-relay`，均不持久化到卷。停机不恢复活动连接或任务。不要使用多个 worker 或公网反向代理。容器中连接宿主机 SSH 时，主机地址需是容器可达地址，不能把容器自身的 `127.0.0.1` 当作宿主机。

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
