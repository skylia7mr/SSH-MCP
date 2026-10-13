# 配置

配置读取环境变量和仓库根目录 `.env`，统一前缀 `SSH_MCP_`，环境变量优先。`.env.example` 给出完整默认值；空 `SSH_MCP_MCP_TOKEN` 等同未配置。数值必须为正，输出限额至少 4 字节、端口 1..65535、默认超时不得超过最大超时（最大 3600 秒）。

| 环境变量（均加 SSH_MCP_ 前缀） | 默认 | 含义 |
| --- | --- | --- |
| HOST | 127.0.0.1 | uv 监听地址；Compose 容器内覆盖为 0.0.0.0，宿主仅发布 loopback |
| PORT | 8000 | 网页/API/MCP 同一端口 |
| KNOWN_HOSTS | ./known_hosts | 仅确认公钥；Compose 为 /app/trust/known_hosts |
| MCP_TOKEN | 随机 | 固定 Bearer token；勿提交或记录实际值 |
| MAX_SESSIONS | 32 | 注册表全部连接记录，包括失败/断开；网页移除后释放名额 |
| PAIRING_TTL_SECONDS | 300 | 一次性配对码有效期 |
| PAIRING_MAX_ATTEMPTS | 5 | 最多配对尝试数 |
| MAX_GLOBAL_RUNS | 16 | 全局同时占用 exec 槽位数 |
| MAX_SESSION_RUNS | 4 | 单 SSH 连接同时占用槽位数 |
| MAX_QUEUE | 64 | 等待队列最大长度；满时拒绝新任务 |
| MAX_TASK_RECORDS | 1000 | 任务记录总数；优先淘汰最早完成任务，全部活动时拒绝 |
| MAX_OUTPUT_BYTES | 1048576 | 每任务每流 raw byte 硬上限；超限继续排空 |
| DEFAULT_TIMEOUT_SECONDS | 60 | 从任务取得槽位开始计时，含认证/channel 创建，不含排队 |
| MAX_TIMEOUT_SECONDS | 3600 | 客户端可请求的最大运行超时 |
| TASK_RETENTION_SECONDS | 1800 | 完成后保留任务/输出时间，约每 0.5 秒与读取时清理 |
| SSH_CONNECT_TIMEOUT_SECONDS | 15 | 主机公钥探测与 SSH 建连超时 |
| SSH_KEEPALIVE_INTERVAL | 30 | keepalive 周期，连续 3 次失败判掉线 |

无自动空闲断开策略。上限配置应结合内存：默认输出最坏约 1000 × 2 MiB，此外还有命令和连接对象。实际按输出增长分配；可降低记录或每流上限。队列限额和记录限额独立，运行中记录不会被淘汰。

token 轮换只影响当前进程；若配置固定 token，重启恢复环境变量的值。配对码、cookie 和随机 token 每次启动重新生成。不能使用多个 Uvicorn worker、reload 自动恢复或多实例负载均衡共享此内存状态。

开发 Vite 固定默认 5173 端口，代理默认 8000；修改后端端口需同时修改 `frontend/vite.config.ts` 的代理目标。生产只需构建后单服务启动，可信 Origin 随配置的后端端口设置。
