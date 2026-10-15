import pytest

from ssh_mcp.errors import ToolError
from ssh_mcp.sudo.syntax import context_command, prepare


@pytest.mark.parametrize(
    "command",
    [
        "echo ready && sudo id",
        "false || sudo id",
        "touch /tmp/test; sudo id",
        "sudo id; sudo id",
        "sudo cat /etc/passwd | grep root",
        "printf 'data\\n' | sudo tee /tmp/file",
        "(sudo id)",
        'echo "$(sudo id)"',
        "/usr/bin/sudo id",
        "command sudo id",
        "env sudo id",
        "env FOO=bar sudo id",
        "sudo -u root id",
    ],
)
def test_detects_actual_sudo(command: str) -> None:
    plan = prepare(command)
    assert plan.uses_sudo
    assert len(plan.calls) == (2 if command == "sudo id; sudo id" else 1)
    assert "_ssh_mcp_sudo_" in plan.transformed


@pytest.mark.parametrize(
    "command",
    ["echo 'sudo id'", "echo sudo # sudo id", "cat <<EOF\nsudo id\nEOF", "command -v sudo"],
)
def test_literal_data_is_not_sudo(command: str) -> None:
    assert not prepare(command).uses_sudo


@pytest.mark.parametrize(
    "command",
    [
        "sudo -S id",
        "sudo -b id",
        "sudo -p hi id",
        "sudo -i",
        "sudo id &",
        "cat <(sudo id)",
        "sudo id 9<&0",
    ],
)
def test_unsupported_is_explicit(command: str) -> None:
    with pytest.raises(ToolError) as e:
        prepare(command)
    assert e.value.code == "UNSUPPORTED_COMMAND_SYNTAX"


def test_context_quotes_shell_injections() -> None:
    c = context_command("pwd", "a' b; touch /evil", {"VALUE": "$(sudo id)'"})
    assert "cd -- 'a'\"'\"' b; touch /evil'" in c
    with pytest.raises(ToolError):
        context_command("id", None, {"FOO;id": "x"})


def test_discovery_command_still_visits_substitutions() -> None:
    assert prepare('command -v "$(sudo id)"').uses_sudo


def test_parallel_sudo_authentication_is_rejected() -> None:
    with pytest.raises(ToolError):
        prepare("sudo cat /etc/passwd | sudo tee /tmp/file")


@pytest.mark.parametrize(
    "command",
    [
        "for item in a b; do echo $item; done",
        "if true; then echo ready; fi",
        "hello() { echo ok; }; hello",
        "echo $((1+2))",
        "echo ${value:-default}",
        "sh -c 'echo hello'",
        "eval 'echo hello'",
        "echo x 9<&0",
        "sleep 1 & wait",
        "echo x &&",
        "x=sudo; $x id",
        "sh -c 'sudo id'",
    ],
)
def test_ordinary_shell_commands_are_preserved(command: str) -> None:
    plan = prepare(command)
    assert plan.command == plan.transformed == command
    assert not plan.uses_sudo


@pytest.mark.parametrize(
    "command",
    [
        "sudo bash -c 'id; printf ok'",
        "for item in a b; do sudo id; done",
        "if true; then sudo id; fi",
        "hello() { sudo id; }; hello",
    ],
)
def test_sudo_in_sequential_shell_constructs(command: str) -> None:
    plan = prepare(command)
    assert plan.uses_sudo and len(plan.calls) == 1
