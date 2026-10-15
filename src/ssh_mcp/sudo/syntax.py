"""Preserve shell commands and rewrite identifiable sudo calls for password injection."""

import re
import secrets
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import bashlex

from ssh_mcp.errors import ToolError

ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def unsupported(message: str) -> ToolError:
    return ToolError("UNSUPPORTED_COMMAND_SYNTAX", message)


def children(node: Any) -> Iterator[Any]:
    seen: set[int] = set()
    for value in vars(node).values():
        if hasattr(value, "kind") and id(value) not in seen:
            seen.add(id(value))
            yield value
        elif isinstance(value, list):
            for item in value:
                if hasattr(item, "kind") and id(item) not in seen:
                    seen.add(id(item))
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
        if re.search(r"(?<![\w])sudo(?![\w])", command):
            raise unsupported(
                "此语法中的 sudo 无法自动注入密码；请拆开普通命令和 sudo 命令"
            ) from exc
        return CommandPlan(command, command, ())
    if not nodes:
        raise unsupported("请输入实际命令")
    calls: list[SudoCall] = []

    def literal(word: Any) -> bool:
        return not getattr(word, "parts", []) and not any(c in word.word for c in "*?[")

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 64:
            raise unsupported("命令语法嵌套过深")
        if node.kind == "command":
            words = [p for p in node.parts if p.kind == "word"]
            if words:
                prefix_start = words[0].pos[0]
                i = 0
                assignments: list[Any] = []
                while i < len(words):
                    if not literal(words[i]):
                        break
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
                            break
                        continue
                    break
                if i >= len(words):
                    for child in children(node):
                        visit(child, depth + 1)
                    return
                executable = words[i]
                name = executable.word.rsplit("/", 1)[-1]
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

    if not calls:
        return CommandPlan(command, command, ())

    def check_pipeline(node: Any) -> None:
        if node.kind == "operator" and node.op == "&":
            raise unsupported("自动 sudo 认证不支持后台并发；请单独提交任务")
        if node.kind == "processsubstitution":
            raise unsupported("自动 sudo 认证不支持进程替换；请拆成独立命令")
        if node.kind == "redirect":
            if getattr(node, "input", None) not in (None, 0, 1, 2):
                raise unsupported("自动 sudo 认证需要保留 FD 3 及以上")
            output = getattr(node, "output", None)
            if isinstance(output, int) and output > 2:
                raise unsupported("不可引用内部密码通道 FD")
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
