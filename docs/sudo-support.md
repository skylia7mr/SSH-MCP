# sudo 与 shell 支持矩阵

远端最低环境为 Linux sshd、可运行非 PTY exec 的 POSIX shell、基础 `printf`/重定向/函数能力。使用 sudo 时需要 `/usr/bin/sudo` 支持 `-S -p -v -n`，sudoers 允许验证并在同一父 shell 保留授权；实际验证 Debian Bookworm 的 OpenSSH、sudo 1.9.13、Bash 5.2 和 dash。不承诺 Windows、网络设备、任意 Bash/zsh 语法或交互程序。

解析器为锁定的 bashlex 0.18，GPL-3.0-or-later，项目 GPL-3.0。它不完整支持 Bash；本项目额外限制为保守子集。所有实际 command node（包括子 shell/命令替换）先遍历，不能仅靠命令前缀、split 或关键词。整条语法失败或不支持会执行前返回 `UNSUPPORTED_COMMAND_SYNTAX`，不回退原样执行。

| 写法 | 行为 / 真实测试 |
| --- | --- |
| `sudo id`, `/usr/bin/sudo id`, `/bin/sudo id` | 只替换原 sudo 位置；默认关闭则整条拒绝 |
| `command sudo id`, `command -- sudo id`, `env sudo id`, `env FOO=bar sudo id` | 支持 command 前缀及字面量 env 赋值；env 选项拒绝 |
| `echo ready && sudo id`, `false || sudo id`, `touch path; sudo id` | 保留原连接符/退出语义；关闭时 touch 也不执行 |
| `sudo id; sudo id` | 顺序多 sudo，同一父 shell 的缓存命中不再发密码 |
| `sudo cat file \| grep pattern` | 原 sudo 子命令提权，后续 grep 保持普通账号 |
| `printf 'data\n' \| sudo tee path` | 实测文件和输出只有 data，密码与 pipe stdin 分离 |
| `(sudo id)`, `echo "$(sudo id)"` | AST 遍历并保留位置，Bash/dash 实测 |
| `sudo id 2>/dev/null`, `<file`, `>file`, `>>file` | 原命令 stdin/stdout/stderr 重定向保留；认证通过独立 FD |
| 字符串、注释、引用分隔符的 heredoc 中 `sudo` | 作为数据，不误提权 |
| `command -v sudo` | 查询而非执行；仍检查其中的命令替换 |
| `sudo -u root id`, `-g group`, `--user=...`, `--group=...` | 用户/组必须字面量，验证与执行使用相同目标选项 |
| `-H`, `--set-home`, `-E`, `--preserve-env` | 支持，仍服从远端 sudoers |
| 一条管道中多个 sudo，如 `sudo cat x \| sudo tee y` | 可能并行竞争密码 FD，执行前拒绝；拆成顺序命令 |
| shell `-c`、sudo 包裹 shell `-c`、嵌套 sudo | 明确拒绝；把可识别的字面量命令展开至顶层 |
| 动态可执行名 `$cmd`、eval/exec/source/alias/trap/set | 明确拒绝，不能可靠管理执行策略 |
| 后台 `&`、循环/函数/其他复合语法、进程替换 | 当前不支持；长任务通过 MCP 管理 |
| 算术展开、复杂 `${...}`、反引号、展开的 heredoc | 保守拒绝；普通参数和 `$()` 可用 |
| FD 3 及以上、引用内部 FD | 拒绝，避免占用密码控制通道 |
| sudo `-S`, `-p`, `-b`, `-i`, `-s`, `-k`, `-n` 等未列选项 | 执行前拒绝，密码/提示/后台/交互行为由工具控制 |

## 密码输入分离：真实实验决定的实现

每任务独立非 PTY exec，已有 shell 保存 SSH stdin 为 FD 9、原 stderr 为 FD 8，再把普通命令 stdin 设置为 `/dev/null`。AST 的每个 sudo 被替换为随机名称的临时 shell 函数，函数只存在该 channel 内存，不安装远端 helper、不写文件。

函数在**当前父 shell**先运行 `sudo -S -p <随机标记> <目标选项> -v`，从 FD 9 认证、向 FD 8 发控制信息。服务仅在看到随机无换行密码提示后写入一次密码；NOPASSWD 或缓存命中不预先写入。认证后用 `sudo -n ... -v` 确认同一 shell 的缓存可用，再执行原 `sudo -n "$@"` 子命令并关闭内部 FD。子命令 stdin 仍是原 pipe 或文件，原 stdout/stderr 仍服从重定向，不把整个复合命令提升为 root。

随机提示/start/done/失败标记可跨块识别并过滤。错密码第二次提示立即失败，缺密码立即返回未配置，不无限等待。初步独立实验覆盖三个账号、Bash/dash、24 项真实执行，stderr 每次只读 7 字节；完整任务测试继续覆盖选项、默认拒绝、政策切换和 1 MiB 输出。详见实施记录。

没有假定另一 SSH channel 的 `sudo -v` 会授权本 channel。`timestamp_timeout=0` 实测在子命令前返回 `SUDO_POLICY_UNSUPPORTED`，不执行该 sudo 子命令；requiretty、禁止验证模式、额外交互认证不支持，服务器拒绝验证时报告认证/策略错误。复合命令中已经运行的普通前缀在运行期认证失败时不会回滚；仅语法/授权预检查保证整条未执行。

密码按服务器实际 sudoers 策略填写，工具不假定是 SSH 登录密码，也不自动复用；`rootpw`、`targetpw` 等要求必须由操作者核对，未宣称这些变体均已实测。已授权运行任务持有快照，关闭开关清除连接保存的密码但不中断该任务；结束、断开、退出释放引用。

## 安全边界

开关是可解析 sudo 调用的本地策略，不是远端权限隔离。任意脚本、Python 或动态程序可能间接执行 sudo，root 登录不会降权，远端可执行文件本身可做任何现有账号获准的操作。要绝对禁止提权必须依靠远端账号权限和 sudoers。命令输出可能主动包含其他秘密；工具只保证自身认证输入与内部控制标记不混入可见输出，不承诺通用脱敏。
