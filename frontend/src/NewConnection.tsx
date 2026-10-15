import { useState, type FormEvent } from "react";
import { api, type Session } from "./api";
import { Modal } from "./Modal";
type Probe = {
  probe_id: string;
  algorithm: string;
  fingerprint: string;
  trusted: boolean;
};
export function NewConnection({
  onClose,
  onConnected,
  existing,
  editing = false,
}: {
  onClose: () => void;
  onConnected: (s: Session) => void;
  existing?: Session;
  editing?: boolean;
}) {
  const [alias, setAlias] = useState(existing?.alias ?? "");
  const [host, setHost] = useState(existing?.host ?? "");
  const [port, setPort] = useState(existing?.port ?? 22);
  const [username, setUsername] = useState(existing?.username ?? "");
  const [mode, setMode] = useState(existing?.auth_type ?? "password");
  const [password, setPassword] = useState("");
  const [key, setKey] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [probe, setProbe] = useState<Probe | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [clearPassword, setClearPassword] = useState(false);
  const clearSecrets = () => {
    setPassword("");
    setKey("");
    setPassphrase("");
  };
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (editing && existing) {
        const updated = await api<Session>(`/sessions/${existing.session_id}`, "PUT", {
          alias, host, port, username, auth_type: mode,
          password: password || null, private_key: key || null, clear_password: clearPassword,
        });
        clearSecrets();
        onConnected(updated);
        onClose();
        return;
      }
      setProbe(await api<Probe>("/host-keys/probe", "POST", { host, port }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "探测失败");
    } finally {
      setBusy(false);
    }
  }
  async function confirm() {
    if (!probe) return;
    setBusy(true);
    setError("");
    try {
      await api("/host-keys/confirm", "POST", { probe_id: probe.probe_id });
      const s = await api<Session>(existing ? `/sessions/${existing.session_id}/reconnect` : "/sessions", "POST", {
        alias,
        host,
        port,
        username,
        probe_id: probe.probe_id,
        password: password || null,
        private_key: mode === "key" ? key : null,
        passphrase: mode === "key" && passphrase ? passphrase : null,
      });
      clearSecrets();
      onConnected(s);
      if (s.status === "connected") onClose();
      else {
        setProbe(null);
        setError(`连接失败：${s.error ?? "SSH 故障"}。请重新填写凭据并重试。`);
      }
    } catch (e) {
      clearSecrets();
      setProbe(null);
      setError(e instanceof Error ? e.message : "连接失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={editing ? `编辑连接 ${existing?.alias}` : existing ? `重连 ${existing.alias}` : "新建 SSH 连接"}
      onClose={() => {
        clearSecrets();
        onClose();
      }}
    >
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <form onSubmit={submit} autoComplete="off">
        <fieldset disabled={busy || !!probe}>
          <div className="form-grid">
            <label>
              连接别名
              <input
                required
                maxLength={64}
                readOnly={!!existing && !editing}
                value={alias}
                onChange={(e) => setAlias(e.target.value)}
              />
            </label>
            <label>
              主机
              <input
                required
                maxLength={255}
                readOnly={!!existing && !editing}
                placeholder="例如 192.168.1.10"
                value={host}
                onChange={(e) => setHost(e.target.value)}
              />
            </label>
            <label>
              端口
              <input
                required
                type="number"
                min={1}
                max={65535}
                readOnly={!!existing && !editing}
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
              />
            </label>
            <label>
              用户名
              <input
                required
                maxLength={128}
                readOnly={!!existing && !editing}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
          </div>
          <label>
            认证方式
            <select
              value={mode}
              disabled={!!existing && !editing}
              onChange={(e) => {
                clearSecrets();
                setMode(e.target.value as "password" | "key");
              }}
            >
              <option value="password">SSH 密码</option>
              <option value="key">私钥（支持加密）</option>
            </select>
          </label>
          {mode === "password" ? (
            <label>
              SSH 密码
              <input
                type="password"
                required={!editing}
                autoComplete="new-password"
                maxLength={4096}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
          ) : (
            <>
              {(!existing?.private_key_saved || editing) && <>
              <label>
                粘贴私钥
                <textarea
                  required={!existing?.private_key_saved}
                  rows={6}
                  maxLength={131072}
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  spellCheck={false}
                />
              </label>
              <label>
                选择私钥文件
                <input
                  type="file"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (f) {
                      if (f.size > 131072) {
                        setError("私钥文件不可超过 128 KiB");
                        return;
                      }
                      setKey(await f.text());
                    }
                    e.target.value = "";
                  }}
                />
              </label>
              </>}
              {existing?.private_key_saved && <p className="muted">使用已保存的私钥，无需重新上传。</p>}
              {!editing && <label>
                私钥口令（可选）
                <input
                  type="password"
                  autoComplete="new-password"
                  value={passphrase}
                  maxLength={4096}
                  onChange={(e) => setPassphrase(e.target.value)}
                />
              </label>}
              <label>
                账户密码（sudo 使用，可选）
                <input type="password" autoComplete="new-password" maxLength={4096}
                  value={password} onChange={(e) => setPassword(e.target.value)} />
              </label>
            </>
          )}
        </fieldset>
        {editing && <>
          <p className="muted">密码留空保留当前内存值；密钥留空保留已保存私钥。可在这里补填账户密码供 sudo 使用，密码不落盘。</p>
          <label className="switch"><input type="checkbox" checked={clearPassword} onChange={(event) => {
            setClearPassword(event.target.checked);
            if (event.target.checked) setPassword("");
          }} />清除当前账户密码</label>
          <p className="boundary">保存后连接 ID 会更新，旧 ID 立即失效，旧 ID 的命令和传输会取消。修改服务器、账号或认证方式会断开连接，需要重新连接。</p>
        </>}
        <p className="muted">保存连接配置及私钥；密码、私钥口令只保留在本次连接的内存中。sudo 开关开启后复用账户密码。</p>
        {!probe && (
          <button className="primary" disabled={busy}>
            {busy ? "处理中…" : editing ? "保存连接" : "检查主机指纹"}
          </button>
        )}
      </form>
      {probe && (
        <section className="fingerprint">
          <h3>{probe.trusted ? "已信任的主机" : "请确认主机身份"}</h3>
          <p>
            {host}:{port}
          </p>
          <p>
            算法：<code>{probe.algorithm}</code>
          </p>
          <p>
            SHA256 指纹：
            <code data-testid="fingerprint">{probe.fingerprint}</code>
          </p>
          <p className="muted">
            请与服务器管理员提供的指纹核对。确认后只保存主机公钥，认证连接会再次校验同一公钥。
          </p>
          <div className="actions">
            <button className="primary" disabled={busy} onClick={confirm}>
              {busy ? "连接中…" : "确认指纹并连接"}
            </button>
            <button disabled={busy} onClick={() => setProbe(null)}>
              返回修改
            </button>
          </div>
        </section>
      )}
    </Modal>
  );
}
