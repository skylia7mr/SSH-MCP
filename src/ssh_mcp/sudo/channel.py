"""Ephemeral POSIX shell wrapper. No remote file, service, or password in command text."""

import shlex
from dataclasses import dataclass

from ssh_mcp.errors import ToolError
from ssh_mcp.sudo.syntax import CommandPlan


@dataclass(frozen=True)
class ControlMarkers:
    nonce: str

    def token(self, kind: str) -> bytes:
        return f"__SSH_MCP_{self.nonce}_{kind.upper()}__".encode()

    @property
    def prompt(self) -> bytes:
        return self.token("prompt")

    @property
    def auth_failed(self) -> bytes:
        return self.token("failed")


def definition(function: str, markers: ControlMarkers, options: tuple[str, ...] = ()) -> str:
    auth_options = " ".join(shlex.quote(x) for x in options)
    return f"""{function}() {{
    printf '%s' {shlex.quote(markers.token("start").decode())} >&8
    /usr/bin/sudo -S -p {shlex.quote(markers.prompt.decode())} {auth_options} -v <&9 >&8 2>&8
    _ssh_mcp_auth=$?
    if [ "$_ssh_mcp_auth" -ne 0 ]; then
        printf '%s' {shlex.quote(markers.auth_failed.decode())} >&8
        return 125
    fi
    /usr/bin/sudo -n {auth_options} -v <&9 >&8 2>&8
    if [ "$?" -ne 0 ]; then
        printf '%s' {shlex.quote(markers.token("policy").decode())} >&8
        return 125
    fi
    printf '%s' {shlex.quote(markers.token("done").decode())} >&8
    /usr/bin/sudo -n "$@" 9<&- 8>&-
    _ssh_mcp_result=$?
    return "$_ssh_mcp_result"
}}
"""


def wrap_sudo(command: str, function: str, markers: ControlMarkers) -> str:
    return "exec 9<&0 8>&2\nexec 0</dev/null\n" + definition(function, markers) + command + "\n"


def wrap_plan(plan: CommandPlan, markers: ControlMarkers) -> str:
    definitions = "".join(definition(c.function, markers, c.auth_options) for c in plan.calls)
    return "exec 9<&0 8>&2\nexec 0</dev/null\n" + definitions + plan.transformed + "\n"


class ControlFilter:
    """Match random markers across arbitrary chunks without buffering whole stderr."""

    def __init__(self, markers: ControlMarkers) -> None:
        self.markers = markers
        self.pending = b""
        self.prompt_count = 0

    def events(self, chunk: bytes, final: bool = False) -> tuple[bytes, list[str]]:
        data = self.pending + chunk
        self.pending = b""
        output = bytearray()
        events: list[str] = []
        tokens = [
            (self.markers.token(kind), kind)
            for kind in ("start", "prompt", "done", "failed", "policy")
        ]
        while data:
            found = [(data.find(token), token, kind) for token, kind in tokens if token in data]
            if found:
                i, token, kind = min(found, key=lambda x: x[0])
                output.extend(data[:i])
                data = data[i + len(token) :]
                events.append(kind)
                continue
            keep = 0
            if not final:
                for token, _ in tokens:
                    for length in range(1, min(len(data), len(token) - 1) + 1):
                        if data.endswith(token[:length]):
                            keep = max(keep, length)
            output.extend(data[:-keep] if keep else data)
            self.pending = data[-keep:] if keep else b""
            break
        self.prompt_count += events.count("prompt")
        return bytes(output), events

    def feed(self, chunk: bytes, final: bool = False) -> tuple[bytes, int, bool]:
        visible, events = self.events(chunk, final)
        return visible, events.count("prompt"), "failed" in events


def check_password(password: str | None) -> None:
    if password is not None and any(c in password for c in "\x00\r\n"):
        raise ToolError("INVALID_SUDO_PASSWORD", "sudo 密码不可包含换行或 NUL")
