import { useEffect, useState } from "react";
import { api } from "./api";
import { Modal } from "./Modal";
type Info = {
  endpoint: string;
  token_revealed: boolean;
  configured_from_environment: boolean;
};
export function McpSetup({ onClose }: { onClose: () => void }) {
  const [info, setInfo] = useState<Info | null>(null);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    void api<Info>("/mcp/info")
      .then(setInfo)
      .catch((e) => setError(e.message));
  }, []);
  async function show(rotate: boolean) {
    setBusy(true);
    setError("");
    setToken("");
    try {
      const data = await api<{ token: string }>(
        `/mcp/token/${rotate ? "rotate" : "reveal"}`,
        "POST",
      );
      setToken(data.token);
      setInfo((old) => (old ? { ...old, token_revealed: true } : old));
    } catch (e) {
      setError(e instanceof Error ? e.message : "接入信息读取失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="Codex / MCP 接入"
      onClose={() => {
        setToken("");
        onClose();
      }}
    >
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <p>
        在同一台电脑启动兼容 MCP 的客户端。网页管理 SSH，Agent
        通过以下地址执行命令。
      </p>
      <code className="endpoint">{info?.endpoint ?? "正在读取…"}</code>
      <p className="muted">
        官方 MCP HTTP 与 Codex CLI 0.159.2
        已实测。客户端是否接入由客户端确认，此处不显示“已连接 Codex”。
      </p>
      <div className="actions">
        <button
          disabled={busy || !info || info.token_revealed}
          onClick={() => void show(false)}
        >
          首次显示 token
        </button>
        <button disabled={busy || !info} onClick={() => void show(true)}>
          轮换 token（撤销旧授权）
        </button>
      </div>
      {token && (
        <label>
          本次 token（关闭后不再回填）
          <textarea
            readOnly
            rows={2}
            value={`export SSH_MCP_CLIENT_TOKEN="${token}"`}
            spellCheck={false}
            data-testid="mcp-token"
          />
          <span className="muted">
            只保存在当前弹窗内存中。请在本机受控终端设置环境变量；不放 URL
            或持久浏览器存储。
          </span>
        </label>
      )}
      {info?.configured_from_environment && (
        <p className="muted">
          已使用环境变量固定 token。轮换只影响当前进程，重启会恢复环境变量值。
        </p>
      )}
      <p>
        在启动 Codex 的终端设置 <code>SSH_MCP_CLIENT_TOKEN</code>
        ，将以下配置加入 Codex 的 <code>config.toml</code>：
      </p>
      <pre>{`[mcp_servers.ssh_workbench]\nurl = "${info?.endpoint ?? "http://127.0.0.1:8000/mcp"}"\nbearer_token_env_var = "SSH_MCP_CLIENT_TOKEN"\nstartup_timeout_sec = 15\ntool_timeout_sec = 15\n\n[mcp_servers.ssh_workbench.tools.ssh_execute]\napproval_mode = "approve"\n\n[mcp_servers.ssh_workbench.tools.ssh_cancel_run]\napproval_mode = "approve"`}</pre>
      <p className="muted">
        先调用 ssh_list_sessions，再选 session_id 调用 ssh_execute。长任务用
        run_id 查询与分段读取。token 轮换后需更新客户端环境变量并重新接入。
      </p>
    </Modal>
  );
}
