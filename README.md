# SSH MCP 本机工作台

本机单操作者通过中文网页连接多个 Linux SSH 账号，MCP 客户端在同一后端进程执行命令并读取真实结果。凭据、连接配置、任务和输出仅驻留内存；仅确认过的公钥保存在 OpenSSH known_hosts。

当前处于实施中。可验收清单及证据见 [实施记录](docs/implementation.md)。尚未验证的功能不会声明完成。

依赖：Python 3.12（uv 管理）、Node.js 22、npm；测试和容器路径需要 Docker。
