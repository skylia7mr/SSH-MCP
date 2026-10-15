import os
import re
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from playwright.async_api import async_playwright, expect
from pydantic import SecretStr

from ssh_mcp.schemas import ConnectRequest
from tests.integration.test_mcp_http import running_app
from tests.integration.test_profiles_transfers import probe_for


async def test_browser_reconnect_without_file_transfer_ui(real_sshd, tmp_path: Path):
    async with running_app(tmp_path) as (app, base), async_playwright() as pw:
        services = app.state.services
        connections = {}
        for username in ("alice", "bob"):
            connections[username] = await services.registry.connect(
                ConnectRequest(
                    alias=username,
                    host="127.0.0.1",
                    port=real_sshd.port,
                    username=username,
                    password=SecretStr(real_sshd.password),
                    probe_id=await probe_for(services.trust, real_sshd.port),
                )
            )
        await services.registry.disconnect(connections["alice"].session_id)
        browser = await pw.chromium.launch(
            executable_path=os.environ.get("SSH_MCP_CHROMIUM_EXECUTABLE")
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(base)
            await page.get_by_label("本机配对码").fill(services.auth.pairing.code)
            await page.get_by_role("button", name="配对并进入").click()
            await page.get_by_role("button", name="重连", exact=True).click()
            await expect(page.get_by_label("连接别名")).to_have_value("alice")
            await page.get_by_label("SSH 密码", exact=True).fill(real_sshd.password)
            await page.get_by_role("button", name="检查主机指纹").click()
            await page.get_by_role("button", name="确认指纹并连接").click()
            await expect(page.get_by_role("button", name="断开连接", exact=True)).to_be_enabled()
            assert services.registry.get(connections["alice"].session_id).status == "connected"
            await expect(page.locator(".file-panel")).to_have_count(0)
            await expect(page.get_by_label("从电脑选择文件")).to_have_count(0)
            await expect(page.get_by_role("button", name="开始传输", exact=True)).to_have_count(0)
            await expect(page.get_by_label("文件传输记录")).to_have_count(0)
            assert not errors
        finally:
            await browser.close()


@pytest.mark.skipif(os.environ.get("SSH_MCP_REAL_TESTS") != "1", reason="set SSH_MCP_REAL_TESTS=1")
async def test_real_browser_token_export_and_reload_after_session_reset(tmp_path: Path):
    async with running_app(tmp_path) as (app, base), async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=os.environ.get("SSH_MCP_CHROMIUM_EXECUTABLE")
        )
        page = await browser.new_page()
        try:
            await page.goto(base)
            await page.get_by_label("本机配对码").fill(app.state.services.auth.pairing.code)
            await page.get_by_role("button", name="配对并进入").click()
            await page.get_by_role("button", name="Codex / MCP 接入", exact=True).click()
            async with page.expect_response("**/api/mcp/token/reveal") as revealed:
                await page.get_by_role("button", name="首次显示 token").click()
            token = (await (await revealed.value).json())["token"]
            await expect(page.get_by_test_id("mcp-token")).to_have_value(
                f'export SSH_MCP_CLIENT_TOKEN="{token}"'
            )
            async with page.expect_navigation():
                app.state.services.auth.browser = None
            await expect(page.get_by_role("heading", name="连接本机工作台")).to_be_visible()
            await expect(page.get_by_test_id("mcp-token")).to_have_count(0)
        finally:
            await browser.close()


async def test_real_browser_pair_connect_sudo_mcp_output_and_disconnect(real_sshd, tmp_path: Path):
    async with running_app(tmp_path) as (app, base), async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=os.environ.get("SSH_MCP_CHROMIUM_EXECUTABLE")
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(base)
            await page.get_by_label("本机配对码").fill(app.state.services.auth.pairing.code)
            await page.get_by_role("button", name="配对并进入").click()
            await expect(page.get_by_text("● 后端在线", exact=True)).to_be_visible()
            await page.get_by_role("button", name="＋ 新建连接").click()
            await page.get_by_label("连接别名").fill("真实密码连接")
            await page.get_by_label("主机", exact=True).fill("127.0.0.1")
            await page.get_by_label("端口", exact=True).fill(str(real_sshd.port))
            await page.get_by_label("用户名", exact=True).fill("alice")
            await page.get_by_label("SSH 密码", exact=True).fill("wrong-test-password")
            await page.get_by_role("button", name="检查主机指纹").click()
            await expect(page.get_by_test_id("fingerprint")).to_contain_text("SHA256:")
            await page.get_by_role("button", name="确认指纹并连接").click()
            await expect(page.get_by_role("alert")).to_contain_text("SSH_AUTHENTICATION_FAILED")
            await expect(page.get_by_label("SSH 密码", exact=True)).to_have_value("")
            await page.get_by_label("SSH 密码", exact=True).fill(real_sshd.password)
            await page.get_by_role("button", name="检查主机指纹").click()
            await page.get_by_role("button", name="确认指纹并连接").click()
            await expect(page.get_by_role("heading", name="真实密码连接")).to_be_visible()
            await page.get_by_label("允许 sudo", exact=True).click()
            await expect(page.get_by_label("允许 sudo", exact=True)).to_be_checked()
            await expect(page.get_by_text("使用登录密码", exact=False)).to_be_visible()
            await page.get_by_role("button", name="Codex / MCP 接入", exact=True).click()
            await page.get_by_role("button", name="首次显示 token").click()
            token_command = await page.get_by_test_id("mcp-token").input_value()
            token_match = re.fullmatch(r'export SSH_MCP_CLIENT_TOKEN="([^"]+)"', token_command)
            assert token_match
            token = token_match.group(1)
            await page.get_by_role("button", name="关闭弹窗").click()
            await expect(page.get_by_test_id("mcp-token")).to_have_count(0)
            async with (
                httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}) as transport,
                streamable_http_client(base + "/mcp", http_client=transport) as streams,
                ClientSession(streams[0], streams[1]) as client,
            ):
                await client.initialize()
                listed = await client.call_tool("ssh_list_sessions")
                sid = next(
                    s["session_id"]
                    for s in listed.structured_content["sessions"]
                    if s["status"] == "connected" and s["alias"] == "真实密码连接"
                )
                result = await client.call_tool(
                    "ssh_execute",
                    {
                        "session_id": sid,
                        "command": "printf 'data\\n' | sudo tee /tmp/e2e-pipe; id -un; printf 'web-stderr\\n' >&2",
                    },
                )
                assert not result.is_error and result.structured_content["exit_code"] == 0
                await expect(page.get_by_test_id("command-output")).to_contain_text("data\nalice")
                await page.get_by_role("button", name="标准错误 stderr").click()
                await expect(page.get_by_test_id("command-output")).to_have_text("web-stderr\n")
                storage = await page.evaluate(
                    "JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})"
                )
                assert real_sshd.password not in storage and token not in storage
                assert storage == '{"local":{},"session":{}}'
                assert real_sshd.password not in await page.content()
                file = (
                    await app.state.services.registry.get(sid)
                    .require_connection()
                    .run("cat /tmp/e2e-pipe")
                )
                assert file.stdout == "data\n"
                await page.screenshot(path=str(tmp_path / "workbench-desktop.png"), full_page=True)
                scrolling = await client.call_tool(
                    "ssh_execute",
                    {
                        "session_id": sid,
                        "command": "seq 1 300; sleep 1; seq 301 600; sleep 3; seq 601 900",
                    },
                )
                assert scrolling.structured_content["status"] == "running"
                await page.get_by_role("button", name=re.compile("seq 1 300.*")).click()
                panel = page.get_by_test_id("command-output")
                await expect(panel).to_contain_text("600\n")
                await panel.evaluate(
                    "el => {el.scrollTop = 0; el.dispatchEvent(new Event('scroll'))}"
                )
                await expect(panel).to_contain_text("900\n")
                assert await panel.evaluate("el => el.scrollTop") == 0
                await page.get_by_role("button", name="跟随最新输出").click()
                assert await panel.evaluate("el => el.scrollTop > 0")
                await page.reload()
                await expect(page.get_by_role("heading", name="真实密码连接")).to_be_visible()
                await page.get_by_role("button", name="＋ 新建连接").click()
                await page.get_by_label("连接别名").fill("加密私钥连接")
                await page.get_by_label("主机", exact=True).fill("127.0.0.1")
                await page.get_by_label("端口", exact=True).fill(str(real_sshd.port))
                await page.get_by_label("用户名", exact=True).fill("alice")
                await page.get_by_label("认证方式").select_option("key")
                await page.get_by_label("粘贴私钥").fill(real_sshd.private_key)
                await page.get_by_label("私钥口令（可选）").fill(real_sshd.passphrase)
                await page.get_by_role("button", name="检查主机指纹").click()
                await page.get_by_role("button", name="确认指纹并连接").click()
                await expect(page.get_by_role("heading", name="加密私钥连接")).to_be_visible()
                assert real_sshd.private_key not in await page.content()
                saved_id = next(
                    view.session_id
                    for view in app.state.services.registry.list()
                    if view.alias == "加密私钥连接"
                )
                await page.get_by_label("允许 sudo", exact=True).click()
                await page.get_by_role("button", name="编辑连接", exact=True).click()
                await expect(page.get_by_label("账户密码（sudo 使用，可选）")).to_have_value("")
                await page.get_by_label("连接别名").fill("密钥连接已编辑")
                await page.get_by_label("账户密码（sudo 使用，可选）").fill(real_sshd.password)
                await page.get_by_role("button", name="保存连接", exact=True).click()
                await expect(page.get_by_role("heading", name="密钥连接已编辑")).to_be_visible()
                await expect(page.get_by_text("使用登录密码", exact=False)).to_be_visible()
                assert saved_id not in app.state.services.registry.sessions
                edited = next(
                    view
                    for view in app.state.services.registry.list()
                    if view.alias == "密钥连接已编辑"
                )
                assert edited.status == "connected" and edited.account_password_configured
                await page.get_by_role("button", name="断开连接", exact=True).click()
                await expect(page.get_by_role("button", name="删除连接")).to_be_visible()
                await page.get_by_role("button", name="重连", exact=True).click()
                await expect(page.get_by_label("粘贴私钥")).to_have_count(0)
                await page.get_by_label("私钥口令（可选）").fill(real_sshd.passphrase)
                await page.get_by_role("button", name="检查主机指纹").click()
                await page.get_by_role("button", name="确认指纹并连接").click()
                await expect(
                    page.get_by_role("button", name="断开连接", exact=True)
                ).to_be_enabled()
                await page.get_by_role("button", name="断开连接", exact=True).click()
                await page.get_by_role("button", name="删除连接", exact=True).click()
                await expect(page.get_by_role("heading", name="密钥连接已编辑")).to_have_count(0)
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.emulate_media(color_scheme="dark")
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.get_by_role("button", name=re.compile("真实密码连接.*已连接")).click()
                await expect(page.get_by_role("heading", name="真实密码连接")).to_be_visible()
                await page.get_by_role("button", name="断开连接", exact=True).click()
                await expect(page.get_by_role("button", name="删除连接")).to_be_visible()
                await page.screenshot(
                    path=str(tmp_path / "workbench-mobile-dark.png"), full_page=True
                )
                assert not errors
        finally:
            await browser.close()


async def test_browser_open_during_shutdown_and_restart_requires_pairing(real_sshd, tmp_path: Path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=os.environ.get("SSH_MCP_CHROMIUM_EXECUTABLE")
        )
        context = await browser.new_context()
        page = await context.new_page()
        try:
            async with running_app(tmp_path / "first") as (app, base):
                await page.goto(base)
                await page.get_by_label("本机配对码").fill(app.state.services.auth.pairing.code)
                await page.get_by_role("button", name="配对并进入").click()
                await expect(page.get_by_role("button", name="＋ 新建连接")).to_be_visible()
                # Keep EventSource open as the real server shuts down; no browser-dependent stop.
            async with running_app(tmp_path / "restarted") as (app, base):
                await page.goto(base)
                await expect(page.get_by_role("heading", name="连接本机工作台")).to_be_visible()
                await expect(page.get_by_role("button", name="＋ 新建连接")).to_have_count(0)
                await page.get_by_label("本机配对码").fill(app.state.services.auth.pairing.code)
                await page.get_by_role("button", name="配对并进入").click()
                await expect(page.get_by_role("button", name="＋ 新建连接")).to_be_visible()
        finally:
            await browser.close()
