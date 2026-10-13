"""Standalone real SSH experiment. Test sentinel is generated in memory, never printed."""

import asyncio
import os
import secrets
import subprocess
import time

import asyncssh

from ssh_mcp.sudo.channel import ControlFilter, ControlMarkers, wrap_sudo


async def experiment(password: str) -> None:
    for username in ("alice", "bob", "nopass"):
        async with asyncssh.connect(
            "127.0.0.1",
            port=22222,
            username=username,
            password=password,
            known_hosts=None,
            config=None,
            client_keys=[],
            agent_path=None,
        ) as conn:
            cases = [
                "echo ready && _controlled id",
                "false || _controlled id",
                "_controlled id; _controlled id",
                "_controlled cat /etc/passwd | grep root",
                "printf 'data\\n' | _controlled tee /tmp/ssh-mcp-pipe",
                "(_controlled id)",
                'echo "$(_controlled id)"',
                "_controlled id 2>/dev/null",
            ]
            for case in cases:
                markers = ControlMarkers(secrets.token_hex(16))
                script = wrap_sudo(case, "_controlled", markers)
                assert password not in script
                p = await conn.create_process(script, encoding=None, request_pty=False)
                control = ControlFilter(markers)
                out, err = bytearray(), bytearray()
                prompts = 0

                async def stderr() -> None:
                    nonlocal prompts
                    while chunk := await p.stderr.read(7):
                        visible, count, failed = control.feed(chunk)
                        err.extend(visible)
                        prompts += count
                        if failed:
                            raise AssertionError("sudo auth failed")
                        for _ in range(count):
                            p.stdin.write((password + "\n").encode())
                            await p.stdin.drain()
                    visible, _, _ = control.feed(b"", final=True)
                    err.extend(visible)

                async def stdout() -> None:
                    while chunk := await p.stdout.read(4096):
                        out.extend(chunk)

                async with asyncio.timeout(10):
                    await asyncio.gather(stdout(), stderr(), p.wait_closed())
                assert p.exit_status == 0, (username, case, p.exit_status, bytes(err))
                assert password.encode() not in out and password.encode() not in err
                if username == "nopass":
                    assert prompts == 0
                if "tee" in case:
                    result = await conn.run("cat /tmp/ssh-mcp-pipe", encoding=None)
                    assert result.stdout == b"data\n"
                    assert bytes(out) == b"data\n"
                print(f"PASS shell={username} command={case} prompts={prompts}")


def main() -> None:
    password = "sentinel-" + secrets.token_hex(18)
    started = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            "ssh-mcp-sudo-experiment",
            "-p",
            "127.0.0.1:22222:22",
            "-e",
            "TEST_PASSWORD",
            "ssh-mcp-test-sshd:local",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "TEST_PASSWORD": password},
    )
    container = started.stdout.decode().strip()
    try:
        for _ in range(40):
            try:
                asyncio.run(experiment(password))
                break
            except (ConnectionRefusedError, ConnectionResetError, asyncssh.ConnectionLost):
                time.sleep(0.25)
        else:
            raise RuntimeError("test sshd did not become ready")
    finally:
        subprocess.run(["docker", "stop", container], check=True, capture_output=True)


if __name__ == "__main__":
    main()
