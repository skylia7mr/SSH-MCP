"""Conservative bashlex AST policy; supported POSIX subset, never an execution fallback."""

import re
import secrets
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import bashlex

from ssh_mcp.errors import ToolError

ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash"}


def unsupported(message: str) -> ToolError:
    return ToolError("UNSUPPORTED_COMMAND_SYNTAX", message)


def children(node: Any) -> Iterator[Any]:
    for value in vars(node).values():
        if hasattr(value, "kind"):
            yield value
        elif isinstance(value, list):
            for item in value:
                if hasattr(item, "kind"):
                    yield item


@dataclass(frozen=True)
class SudoCall:
    start: int
    end: int
    replacement: str
    function: str
    auth_options: tuple[str, ...]


@dataclass(frozen=True)
class CommandPlan:
    command: str
    transformed: str
    calls: tuple[SudoCall, ...]

    @property
    def uses_sudo(self) -> bool:
        return bool(self.calls)


def prepare(command: str) -> CommandPlan:
    if "\x00" in command or len(command.encode()) > 128 * 1024:
        raise unsupported("命令包含 NUL 或过长")
    try:
        nodes = bashlex.parse(command)
    except (
        NotImplementedError,
        bashlex.errors.ParsingError,
        ValueError,
        IndexError,
        RecursionError,
    ) as exc:
        raise unsupported("语法无法可靠解析；请使用文档列出的 POSIX 命令子集") from exc
    if not nodes:
        raise unsupported("请输入实际命令")
    calls: list[SudoCall] = []

    def literal(word: Any) -> bool:
        return not getattr(word, "parts", []) and not any(c in word.word for c in "*?[")

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 64:
            raise unsupported("命令语法嵌套过深")
        allowed = {
            "command",
            "word",
            "assignment",
            "list",
            "operator",
            "pipeline",
            "pipe",
            "compound",
            "reservedword",
            "commandsubstitution",
            "redirect",
            "heredoc",
            "parameter",
        }
        if node.kind not in allowed:
            raise unsupported(f"暂不支持语法节点：{node.kind}")
        if node.kind == "operator" and node.op == "&":
            raise unsupported("暂不支持 shell 后台任务；请使用 MCP 长任务")
        if node.kind == "redirect":
            if node.type not in {">", ">>", "<", "<<", ">&", "<&", "<>"}:
                raise unsupported("暂不支持此重定向")
            if getattr(node, "input", None) not in (None, 0, 1, 2):
                raise unsupported("FD 3 及以上保留给内部密码通道")
            output = getattr(node, "output", None)
            if isinstance(output, int) and output > 2:
                raise unsupported("不可引用内部密码通道 FD")
            if getattr(node, "heredoc", None) is not None:
                text = command[node.output.pos[0] : node.output.pos[1]]
                if not text.startswith(("'", '"')) and (
                    "$" in node.heredoc.value or "`" in node.heredoc.value
                ):
                    raise unsupported("有展开的 heredoc 暂不支持；请引用 heredoc 分隔符")
        if node.kind in {"word", "assignment"}:
            raw = command[node.pos[0] : node.pos[1]]
            # bashlex treats some complex expansions as opaque: reject all such forms.
            if getattr(node, "parts", []) and ("${" in raw or "`" in raw or "$((" in raw):
                raise unsupported("复杂参数/算术展开及反引号暂不支持；请使用普通参数或 $()")
        if node.kind == "reservedword" and node.word not in {"(", ")"}:
            raise unsupported("当前支持括号子 shell；不支持循环、函数或其他复合语法")
        if node.kind == "command":
            words = [p for p in node.parts if p.kind == "word"]
            if words:
                prefix_start = words[0].pos[0]
                i = 0
                assignments: list[Any] = []
                while i < len(words):
                    if not literal(words[i]):
                        raise unsupported("动态可执行命令无法可靠管理 sudo")
                    name = words[i].word.rsplit("/", 1)[-1]
                    if name == "command":
                        i += 1
                        if i < len(words) and words[i].word in {"-v", "-V"}:
                            for child in children(node):
                                visit(child, depth + 1)
                            return
                        if i < len(words) and words[i].word == "--":
                            i += 1
                        continue
                    if name == "env":
                        i += 1
                        while i < len(words) and ASSIGNMENT.match(words[i].word):
                            if not literal(words[i]):
                                raise unsupported("env 前缀仅支持字面量环境变量")
                            assignments.append(words[i])
                            i += 1
                        if i < len(words) and words[i].word.startswith("-"):
                            raise unsupported("env 前缀不支持选项；使用 NAME=value")
                        continue
                    break
                if i >= len(words):
                    for child in children(node):
                        visit(child, depth + 1)
                    return
                executable = words[i]
                name = executable.word.rsplit("/", 1)[-1]
                if name in {"eval", "exec", "source", ".", "alias", "unalias", "trap", "set"}:
                    raise unsupported("动态求值、exec 或修改 shell 执行策略暂不支持")
                if name in SHELLS and any(
                    w.word.startswith("-") and "c" in w.word for w in words[i + 1 :]
                ):
                    raise unsupported("嵌套 shell -c 暂不支持；将字面量命令展开到顶层")
                if name == "sudo":
                    if executable.word not in {"sudo", "/usr/bin/sudo", "/bin/sudo"}:
                        raise unsupported("仅管理 sudo、/usr/bin/sudo 和 /bin/sudo")
                    auth_options: list[str] = []
                    j = i + 1
                    while j < len(words) and words[j].word.startswith("-"):
                        w = words[j]
                        if not literal(w):
                            raise unsupported("sudo 选项必须是字面量")
                        option = w.word
                        if option == "--":
                            j += 1
                            break
                        if option in {"-u", "--user", "-g", "--group"}:
                            if j + 1 >= len(words) or not literal(words[j + 1]):
                                raise unsupported("sudo 用户/组须为字面量")
                            auth_options.extend([option, words[j + 1].word])
                            j += 2
                            continue
                        if option.startswith(("--user=", "--group=")):
                            auth_options.append(option)
                        elif option not in {"-H", "--set-home", "-E", "--preserve-env"}:
                            raise unsupported(
                                "不支持 sudo 选项 " + option + "；密码/提示/后台/交互选项由工具控制"
                            )
                        j += 1
                    if j >= len(words) or not literal(words[j]):
                        raise unsupported("sudo 子命令必须是字面量")
                    target = words[j].word.rsplit("/", 1)[-1]
                    if target == "sudo" or (
                        target in SHELLS
                        and any(w.word.startswith("-") and "c" in w.word for w in words[j + 1 :])
                    ):
                        raise unsupported("嵌套 sudo/shell -c 暂不支持")
                    function = "_ssh_mcp_sudo_" + secrets.token_hex(8)
                    replacement = " ".join(command[w.pos[0] : w.pos[1]] for w in assignments)
                    replacement = (replacement + " " if replacement else "") + function
                    calls.append(
                        SudoCall(
                            prefix_start,
                            executable.pos[1],
                            replacement,
                            function,
                            tuple(auth_options),
                        )
                    )
        for child in children(node):
            if node.kind == "heredoc":
                continue
            visit(child, depth + 1)

    for node in nodes:
        visit(node)

    def check_pipeline(node: Any) -> None:
        if (
            node.kind == "pipeline"
            and sum(node.pos[0] <= call.start < node.pos[1] for call in calls) > 1
        ):
            raise unsupported("同一管道内多个 sudo 可能并发竞争密码输入；请拆为顺序命令")
        for child in children(node):
            check_pipeline(child)

    for node in nodes:
        check_pipeline(node)
    transformed = command
    for call in sorted(calls, key=lambda c: c.start, reverse=True):
        transformed = transformed[: call.start] + call.replacement + transformed[call.end :]
    return CommandPlan(command, transformed, tuple(calls))


def context_command(command: str, cwd: str | None, env: dict[str, str] | None) -> str:
    parts: list[str] = []
    if cwd is not None:
        if "\x00" in cwd:
            raise ToolError("INVALID_ARGUMENT", "cwd 含 NUL")
        parts.append("cd -- " + shlex.quote(cwd) + " || exit 126")
    for key, value in (env or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\x00" in value:
            raise ToolError("INVALID_ARGUMENT", "无效环境变量")
        if len(value.encode()) > 4096 or len(env or {}) > 64:
            raise ToolError("INVALID_ARGUMENT", "环境变量超出大小限制")
        parts.append("export " + key + "=" + shlex.quote(value))
    return "\n".join(parts + [command])
