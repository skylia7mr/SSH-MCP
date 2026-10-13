# 操作与排障

## 首次使用

1. 按 README 从 uv/npm 锁文件安装，构建前端，单服务启动。
2. 在同一台机器的浏览器输入启动终端配对码。成功后不可重用，刷新保留 cookie；过期或尝试耗尽需要重启。不要分享包含配对码的启动终端/容器日志。
3. 新建 SSH 连接，确认算法和 SHA256 指纹。未知主机必须显式确认；密钥变化直接拒绝。仅选用网页输入的密码或私钥（粘贴/文件）、口令，不能指定后端本机密钥路径。
4. 普通命令使用现有账号权限。需要 sudo 时开启该连接开关并独立填写实际 sudo 策略要求的密码；关闭默认清除密码。可随时清除或更新，网页仅返回“已配置”。
5. 配置 MCP 客户端，发现 session 后执行。网页查看任务/输出，长任务通过 run_id 继续读取，可尝试取消。

密码/密钥提交后不回填；失败需重输，新连接会取得新 session ID。浏览器关闭不会断开 SSH。没有自动空闲断开；主动断开释放 sudo 密码并关闭连接，移除记录释放连接名额。任务掉线显示 disconnected，不自动重放。

## 主机信任

`known_hosts` 是标准 OpenSSH 公钥行（非 22 端口为 `[host]:port`），新文件权限 0600。只由用户确认流程添加，不保存认证材料。既有记录变化禁止自动覆盖；若服务器合法换钥，先从可信渠道核对新指纹，停机备份文件并仅删除相应 host 的旧记录，再启动重新确认。不要删除整个信任库来绕过错误。

Docker 命名卷 `host-trust` 中只有此文件，可在停止服务后维护。`docker compose down` 保留公钥卷，`down -v` 会清除确认记录，应由操作者主动选择。工具本身不持久化任务或配置；环境文件由操作者管理，不存远程登录材料。

## 关闭和资源边界

Ctrl-C 或 `docker compose down` 先关闭任务调度，取消活动任务并关闭 SSH，然后退出；所有内存记录消失。已脱离原 shell 的远端后代可能继续运行，取消/超时只能尽力终止；自行在远端核实重要后台工作。运行中 sudo 快照在任务结束时释放。Python 释放引用不保证物理内存擦除。

诊断不记录请求体、命令输出或底层含秘密的异常，Uvicorn access log 默认关闭。输出可能包含用户命令主动打印的秘密，无法通用脱敏。若需要日志共享，只分享错误码/状态，不复制密码、token 或完整输出。

## 常见故障

| 表现 / 错误码 | 处理 |
| --- | --- |
| 网页未构建 / 503 | `cd frontend && npm ci && npm run build`，重启后端挂载 assets |
| 网页会话失效 | 后端重启后用新配对码；同浏览器刷新无需重配 |
| HOST_KEY_CHANGED | 核对真实主机身份，按上面流程维护对应记录 |
| SSH_AUTHENTICATION_FAILED | 检查账号、认证方式；不启用 keyboard-interactive/2FA |
| INVALID_PRIVATE_KEY | 使用可导入私钥，核对 passphrase；加密 OpenSSH 使用 bcrypt |
| SSH_TIMEOUT / SSH_FAILURE | 核对容器可达地址、sshd、端口、防火墙；不会自动重连 |
| SESSION_LIMIT | 断开并移除旧记录，包括失败记录 |
| SUDO_NOT_AUTHORIZED | 网页针对该连接开启；排队期间关闭也会拒绝整条命令 |
| SUDO_PASSWORD_NOT_CONFIGURED | 远端要求密码，在网页单独填写 |
| SUDO_AUTHENTICATION_FAILED | 检查实际 sudo 密码、目标用户/组、sudoers；仅一次密码尝试 |
| SUDO_POLICY_UNSUPPORTED | 不支持零 timestamp 缓存等策略；不要通过落盘密码绕过 |
| UNSUPPORTED_COMMAND_SYNTAX | 简化为支持的 POSIX 子集，拆开并发 sudo；不回退原样执行 |
| QUEUE_LIMIT / TASK_RECORD_LIMIT | 等待/取消活动任务，或合理增加内存上限 |
| OUTPUT_EXPIRED | 保留期或容量已清理，无持久输出可恢复 |
| TIMEOUT / SSH_EXIT_STATUS_MISSING | 核实远端进程，调整超时或服务器 exec 行为 |
| MCP 401 / TOKEN_REVOKED | 更新并重连客户端 token，不能继续使用旧 SSE 授权 |

健康检查 `GET /healthz` 仅说明后端进程可响应，不代表远端 SSH 或 Codex 在线。生产容器未安装 sshd，不使用测试用户；测试镜像仅供 `SSH_MCP_REAL_TESTS=1`，随机密码由测试进程生成并经 stdin/env 注入，不接触用户服务器。
