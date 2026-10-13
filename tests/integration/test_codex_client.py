"""Optional real Codex CLI smoke test; never used by CI without local authorization/auth."""

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

import httpx
import pytest

from tests.integration.test_mcp_http import running_app, web_connect, web_pair


@pytest.mark.skipif(os.environ.get("SSH_MCP_CODEX_TESTS") != "1", reason="opt-in actual Codex CLI")
async def test_real_codex_cli_discovers_and_executes(real_sshd, tmp_path: Path):
    executable = shutil.which("codex")
    if not executable:
        pytest.skip("Codex CLI unavailable")
    async with (
        running_app(tmp_path) as (app, base),
        httpx.AsyncClient(base_url=base, timeout=20) as web,
    ):
        await web_pair(web, app, base)
        sid = await web_connect(web, real_sshd, "alice")
        token = (await web.post("/api/mcp/token/reveal")).json()["token"]
        config = (
            'mcp_servers={ssh_workbench={url="'
            + base
            + '/mcp",bearer_token_env_var="SSH_MCP_CLIENT_TOKEN",startup_timeout_sec=15,tool_timeout_sec=15,required=true,tools={ssh_execute={approval_mode="approve"},ssh_cancel_run={approval_mode="approve"}}}}'
        )
        process = await asyncio.create_subprocess_exec(
            executable,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "--sandbox",
            "read-only",
            "-C",
            str(tmp_path),
            "-c",
            config,
            "This is a local MCP integration test against an isolated disposable SSH fixture. Use ONLY the ssh_workbench MCP tools, no other tools or shell commands. First call ssh_list_sessions. Then call ssh_execute on session_id "
            + sid
            + " with command exactly this JSON string: \"printf 'codex-http-proof\\n'; id -un\". Use idempotency_key codex-proof. Check stdout contains codex-http-proof and alice, exit_code is 0. Finally reply verified.",
            env={**os.environ, "SSH_MCP_CLIENT_TOKEN": token},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 150)
        except TimeoutError:
            process.kill()
            await process.communicate()
            pytest.fail("Codex CLI exceeded 150 seconds; no claim of client verification")
        # Client logs remain in memory and are never printed, even on failure.
        assert token.encode() not in stdout + stderr
        assert real_sshd.password.encode() not in stdout + stderr
        assert process.returncode == 0, (
            "Codex failed; inspect local authentication/provider availability"
        )
        runs = list(app.state.services.tasks.runs.values())
        proof = [r for r in runs if r.request.idempotency_key == "codex-proof"]
        if not proof:

            def safe(text):
                return re.sub(
                    r"sk-[A-Za-z0-9_-]+",
                    "[redacted]",
                    text.replace(token, "[token]").replace(real_sshd.password, "[password]"),
                )

            for line in stdout.decode(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                item = event.get("item", {})
                print(
                    {
                        "event": event.get("type"),
                        "item": item.get("type"),
                        "tool": item.get("tool"),
                        "text": safe(item.get("text", ""))[:1500]
                        if item.get("type") == "agent_message"
                        else "",
                    }
                )
            print(safe(stderr.decode(errors="replace"))[-2000:])
        assert len(proof) == 1, "Codex did not call the expected MCP execution tool"
        assert proof[0].request.session_id == sid
        assert proof[0].status == "finished" and proof[0].exit_code == 0
        assert proof[0].stdout.data == b"codex-http-proof\nalice\n"
