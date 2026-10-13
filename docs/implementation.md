# 实施清单与验证记录

完整需求来源：`docs/codex-goal-prompt.md` 与本次用户粘贴的实施规格。`project-plan.md` 的“本轮不实现”属于原规划轮次，本轮已授权实施。

## 阶段

- [x] 1. 工具链、锁文件、实施方案、第一阶段提交
- [x] 2. 配对、Web/MCP 独立认证、host key、真实 SSH、阶段提交
- [ ] 3. 真实 sshd/sudo 可行性、AST 策略、行为矩阵、阶段提交
- [ ] 4. 有界任务、输出、官方 MCP HTTP 集成、阶段提交
- [ ] 5. 中文 React 工作台、订阅、真实浏览器 E2E、阶段提交
- [ ] 6. Docker Compose、CI、文档、全量验收、交付报告、阶段提交

## 待验收项

- 密码、加密私钥、两账号、错误凭据、未知/改变公钥、探测认证竞态、超时、掉线。
- MCP 六工具，经实际 HTTP 建立官方客户端协议；真实 Codex 客户端配置与调用（CLI 已发现版本 0.159.2）。
- 独立 channel、cwd/env 引用、UTF-8 游标、持续硬截断、队列/并发/记录保留、幂等冲突、取消/超时/关闭。
- sudo 默认拒绝与连接隔离、任务启动原子授权、排队关闭/运行关闭、正确/错误/无密码、NOPASSWD、缓存、多 sudo、管道/重定向、子 shell/命令替换、动态语法明确拒绝。
- sentinel 密码不进入命令、输出、日志、远端文件和浏览器存储；pipe 数据不含密码。
- 一次性配对/有效期/限次、HttpOnly cookie、CSRF/Host/Origin/大小限制、token 一次显示与轮换撤销。
- Web 完整状态、输出订阅、刷新保留连接、后端重启重新配对、真实 E2E。
- Ruff、mypy、pytest、前端类型/构建、锁文件恢复、Compose 构建/启动/健康检查。
- README 与五份操作/架构/配置/sudo/MCP 文档、部署文件与 CI、实际阶段 Git 提交。

## 2026-10-13 环境与依赖核对

- 工作树最初只有规划文件；适用路径未发现 AGENTS.md。受保护空 `.git` 经正常升级权限流程 `git init` 成功；不移动/删除 Git 元数据。
- uv 缓存及 Python 安装目录需改为 `/tmp`；Python 3.12.13 已真实下载。Docker daemon 经正常权限流程可访问。
- 官方 MCP 稳定版本核对：2.3.0，采用同一 v2 API，不混用 v1 FastMCP。官方发布页：https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0 。
- Codex 官方 Streamable HTTP 配置 `url` + `bearer_token_env_var` 已核对：https://learn.chatgpt.com/docs/extend/mcp?surface=cli 。CLI 帮助同时确认支持。尚未真实连接。
- AsyncSSH 2.24.1；host probe 用 `get_server_host_key(config=None)`，认证对已确认公钥进行直接 pin，禁止读取用户 SSH 配置和隐式私钥/agent。
- bashlex 0.18 为 GPL-3.0-or-later；已核对其不完整 Bash 支持，尤其算术展开/复杂参数展开。产品许可将保持兼容，明确子集，不宣称全 Bash。来源：https://github.com/idank/bashlex 。

## sudo 最小实验方案（尚待实测）

不安装远端 helper、不写远端密码文件。单任务非 PTY exec channel，已有 POSIX shell 保存 SSH stdin 到保留 FD，命令默认 stdin 改为 `/dev/null`。AST 中 sudo 改为临时内存 shell 函数：同一父 shell 先 `sudo -S -v` 从保留 FD 认证，随后 `sudo -n` 子命令仍继承原 pipe/重定向 stdin，关闭保留 FD。仅识别到随机无换行提示时提交一次密码；缓存/NOPASSWD 不提交。

必须实测同一父进程 timestamp、管道子 shell、多个 sudo、输出过滤与提示跨块。其他 SSH channel 的 sudo -v 不作为授权依据。timestamp_timeout=0、requiretty 或禁止验证模式的 sudoers 必须及时报告具体不支持策略，不能挂起/泄漏密码。

## 已运行验证（第一阶段）

- `uv sync --locked --offline --group dev`：成功，安装恢复 57 项解析依赖。
- `ruff check src` 与 `mypy src`：通过（后续变更仍需重跑）。
- `pytest -q tests/unit`：41 passed，包含 AST、安全拒绝、配对限次/有效期、token 轮换、UTF-8 分块/非法游标/硬输出限额。
- Docker `ssh-mcp-test-sshd:local` 构建成功；独立实验 `tests/integration/sudo_experiment.py` 在真实 Debian OpenSSH + sudo 1.9.13 上 24 项成功。
- 实测 shell：alice/nopass 为 Bash 5.2；bob 为 dash。普通 sudo 与 NOPASSWD 分别测试。
- 核心 pipe 文件和 stdout 均为 `data\n`；密码仅见到无换行随机提示后从受控 FD 发送，NOPASSWD 提示次数为 0。实验 stderr 按 7 字节分块，覆盖提示跨块；多个 sudo 只一次认证，验证同一父 shell 缓存；每条案例独立 exec channel。
- 第一轮实验因 sshd 就绪阶段 ConnectionResetError 失败，已增强就绪重试；后续整轮通过。测试容器正常清理。
- 前端依赖安装成功、npm audit 0 vulnerabilities；静态页面目前仍只是工具链占位，不代表产品 UI 完成。
- `npm run build`：TypeScript 与 Vite 生产构建成功。第一阶段只验证工具链，尚未实现产品页面。

## 第二阶段验证

- Web 配对、防重放、CSRF、Host/Origin、请求大小、错误体不包含输入、cookie 重启失效、token 一次展示与轮换：真实 FastAPI/TestClient 测试通过。
- 真正 Docker OpenSSH：双账号并行连接、密码、加密 OpenSSH 私钥、错误凭据、每连接 sudo 状态、断开与关闭清理通过。
- AsyncSSH 协议 fixture：未知公钥显式确认、持久化公钥与 OpenSSH 格式、确认后认证前换钥拒绝、既有信任换钥拒绝、探测超时通过。
- 阶段验证命令：`SSH_MCP_REAL_TESTS=1 uv run --no-sync pytest -q tests/unit/test_auth.py tests/unit/test_web_auth.py tests/integration/test_ssh.py tests/integration/test_host_keys.py` → 8 passed；Ruff 与 mypy 通过。
- 发现并修复：RSA 公钥算法不等于安全签名协商算法，不强制 ssh-rsa；AsyncSSH `client_keys=[]` 会加载默认私钥，改为 None + 一次性回调；添加 bcrypt 支持加密 OpenSSH 私钥。
- 原沙箱内 TestClient 线程唤醒卡住，正常升级权限后测试通过；用户改为 unrestricted 环境后复核正常。确认旧卡住进程归属此工作区后终止，未影响其他任务。
