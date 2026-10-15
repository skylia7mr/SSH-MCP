# 操作与排障

## 首次使用

1. 按 README 从 uv/npm 锁文件安装，构建前端，单服务启动。
2. 在同一台机器的浏览器输入启动终端配对码。成功后不可重用，刷新保留 cookie；过期或尝试耗尽需要重启。不要分享包含配对码的启动终端/容器日志。
3. 新建 SSH 连接，确认算法和 SHA256 指纹。未知主机必须显式确认；密钥变化直接拒绝。仅选用网页输入的密码或私钥（粘贴/文件）、口令，不能指定后端本机密钥路径。
4. 普通命令使用现有账号权限。需要 sudo 时开启该连接开关，自动复用 SSH 登录密码；私钥认证可在登录表单选填账户密码。无密码时需要远端 NOPASSWD。关闭开关撤销后续自动 sudo 授权，断开连接才清除账户密码。
5. 配置 MCP 客户端，发现 session 后执行。网页查看任务/输出，长任务通过 run_id 继续读取，可尝试取消。

密码、私钥口令提交后不回填。连接配置和私钥自动保存在权限 0600 的连接配置文件；重启后配对并点击“重连”，补充密码或私钥口令，复用原 session ID。浏览器关闭不会断开 SSH。主动断开清除账户密码并关闭连接，保留配置；删除连接同时删除配置并取消相关任务。编辑可补填或清除账户密码、修改别名及认证材料；每次保存生成新 ID，旧 ID 不再接受 MCP 调用，旧操作停止。客户端不得自动按别名寻找替代 ID 重试。修改服务器身份或认证材料会断开，需要重连。任务掉线显示 disconnected，不自动重放。

## 主机信任

`known_hosts` 是标准 OpenSSH 公钥行（非 22 端口为 `[host]:port`），新文件权限 0600。只由用户确认流程添加，不保存认证材料。既有记录变化禁止自动覆盖；若服务器合法换钥，先从可信渠道核对新指纹，停机备份文件并仅删除相应 host 的旧记录，再启动重新确认。不要删除整个信任库来绕过错误。

Docker 命名卷 `host-trust` 保存此文件及 `connections.json`，可在停止服务后维护。文件目录和中转缓存位于容器 `/tmp`，不使用持久卷。`docker compose down` 保留连接配置卷，同时移除容器内临时文件；`down -v` 会额外删除连接配置、私钥和主机信任。任务记录不持久化。备份连接配置时按私钥材料保护。

文件操作通过 MCP 进行，网页显示传输记录。服务器间中转缓存在 `/tmp/ssh-mcp-relay`，每次传输检查并保留最近 7 个批次；可用 `SSH_MCP_TRANSFER_CACHE_KEEP` 调整。活动批次结束后再清理，失败和取消的批次也计数。本地 MCP 文件目录 `/tmp/ssh-mcp-files` 与中转缓存独立，指定源文件和下载目标不参与此清理。

从旧版升级时，重建容器后旧的 `transfer-files` 卷不会再挂载；确认其中数据不再需要后，可用 `docker volume rm ssh-mcp_transfer-files` 删除旧卷。不要删除 `host-trust` 卷。

## 文件传输

通过 MCP 的 `ssh_list_files` 浏览目录，`ssh_transfer_file` 指定来源和目标；`null` 连接 ID 表示工作台本地文件目录。使用 `ssh_get_transfer` 查询状态、`ssh_cancel_transfer` 取消。目录设置 `recursive=true`，复制源目录内容并保留目标多余文件；已有单文件默认拒绝覆盖，目录内已有文件默认跳过，设置 `overwrite=true` 后更新。两端均需 rsync，浏览目录需 SFTP；Docker 已包含本机 rsync。服务器间通过工作台暂存和转发，远端无需互通，需预留相当于源内容的本地磁盘空间。复用已登录 SSH，无需额外输入传输密码，不自动提权。取消/失败时 rsync 临时文件清理，已完成文件保留；中转批次按缓存保留规则清理。本地源文件和下载目标应按需要自行清理。

网页选择连接后，在“文件传输记录”查看该连接作为来源或目标的传输，按新到旧排列，实时更新排队、传输中、完成、失败、超时或取消状态，以及来源/目标路径、字节进度、创建/结束时间和错误原因。服务器间传输会出现在两端连接的记录中，断开后仍可查看当前记录。默认读取最近 100 条，可加载更早记录，最多 1000 条。记录仅保留在进程内存中，完成后按 `SSH_MCP_TASK_RETENTION_SECONDS` 到期清除（默认 1800 秒），记录总数受 `SSH_MCP_MAX_TASK_RECORDS` 限制；重启后消失，与磁盘中转缓存独立。

## 关闭和资源边界

Ctrl-C 或 `docker compose down` 先关闭任务调度，取消活动任务并关闭 SSH，然后退出；所有内存记录消失。已脱离原 shell 的远端后代可能继续运行，取消/超时只能尽力终止；自行在远端核实重要后台工作。运行中 sudo 快照在任务结束时释放。Python 释放引用不保证物理内存擦除。

诊断不记录请求体、命令输出或底层含秘密的异常，Uvicorn access log 默认关闭。输出可能包含用户命令主动打印的秘密，无法通用脱敏。若需要日志共享，只分享错误码/状态，不复制密码、token 或完整输出。

## 常见故障

| 表现 / 错误码 | 处理 |
| --- | --- |
| 网页未构建 / 503 | `cd frontend && npm ci && npm run build`，重启后端挂载 assets |
| 网页会话失效 | 已打开的工作台检测到会话失效后自动刷新，再用新配对码；后端未重启时同浏览器刷新无需重配 |
| 部署后界面仍是旧版 | Docker 执行 docker compose up -d --build --wait 后刷新；仅 restart 不会更新镜像。本机运行须重新构建 frontend/dist |
| HOST_KEY_CHANGED | 核对真实主机身份，按上面流程维护对应记录 |
| SSH_AUTHENTICATION_FAILED | 检查账号、认证方式；不启用 keyboard-interactive/2FA |
| INVALID_PRIVATE_KEY | 使用可导入私钥，核对 passphrase；加密 OpenSSH 使用 bcrypt |
| SSH_TIMEOUT / SSH_FAILURE | 核对容器可达地址、sshd、端口、防火墙；不会自动重连 |
| SESSION_LIMIT | 断开并移除旧记录，包括失败记录 |
| SUDO_NOT_AUTHORIZED | 网页针对该连接开启；排队期间关闭也会拒绝整条命令 |
| SUDO_PASSWORD_NOT_CONFIGURED | 点击编辑连接补填账户密码；无需断开当前私钥连接 |
| SUDO_AUTHENTICATION_FAILED | 检查实际 sudo 密码、目标用户/组、sudoers；仅一次密码尝试 |
| SUDO_POLICY_UNSUPPORTED | 不支持零 timestamp 缓存等策略；不要通过落盘密码绕过 |
| UNSUPPORTED_COMMAND_SYNTAX | 自动 sudo 认证无法处理该写法，拆开普通命令和 sudo，或使用 sudo bash -c；普通命令直接交给远端 shell |
| FILE_LIST_FAILED / TRANSFER_FAILED | 检查 SFTP 子系统、源路径、目标父目录、账号权限和剩余空间 |
| TARGET_EXISTS | 改目标文件名，或明确允许覆盖 |
| LOCAL_PATH_DENIED | MCP 本地文件须位于 TRANSFER_ROOT 内；Docker 中“本地”指容器目录，按需通过 docker cp 放入或取出 |
| UNKNOWN_SESSION | 连接已编辑或删除，停止当前操作；操作者确认后重新选择连接，禁止按别名自动重试 |
| RSYNC_UNAVAILABLE | 工作台与远端都须安装 rsync；Docker 镜像已包含本机 rsync |
| RSYNC_FAILED | 检查源路径、目标父目录、权限、空间和连接状态 |
| QUEUE_LIMIT / TASK_RECORD_LIMIT | 等待/取消活动任务，或合理增加内存上限 |
| OUTPUT_EXPIRED | 保留期或容量已清理，无持久输出可恢复 |
| TIMEOUT / SSH_EXIT_STATUS_MISSING | 核实远端进程，调整超时或服务器 exec 行为 |
| MCP 401 / TOKEN_REVOKED | 更新并重连客户端 token，不能继续使用旧 SSE 授权 |

健康检查 `GET /healthz` 仅说明后端进程可响应，不代表远端 SSH 或 Codex 在线。生产容器未安装 sshd，不使用测试用户；测试镜像仅供 `SSH_MCP_REAL_TESTS=1`，随机密码由测试进程生成并经 stdin/env 注入，不接触用户服务器。
