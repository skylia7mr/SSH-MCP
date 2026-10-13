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
}: {
  onClose: () => void;
  onConnected: (s: Session) => void;
}) {
  const [alias, setAlias] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState(22);
  const [username, setUsername] = useState("");
  const [mode, setMode] = useState("password");
  const [password, setPassword] = useState("");
  const [key, setKey] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [probe, setProbe] = useState<Probe | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
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
      const s = await api<Session>("/sessions", "POST", {
        alias,
        host,
        port,
        username,
        probe_id: probe.probe_id,
        password: mode === "password" ? password : null,
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
      title="新建 SSH 连接"
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
                value={alias}
                onChange={(e) => setAlias(e.target.value)}
              />
            </label>
            <label>
              主机
              <input
                required
                maxLength={255}
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
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
              />
            </label>
            <label>
              用户名
              <input
                required
                maxLength={128}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
          </div>
          <label>
            认证方式
            <select
              value={mode}
              onChange={(e) => {
                clearSecrets();
                setMode(e.target.value);
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
                required
                autoComplete="new-password"
                maxLength={4096}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
          ) : (
            <>
              <label>
                粘贴私钥
                <textarea
                  required
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
              <label>
                私钥口令（可选）
                <input
                  type="password"
                  autoComplete="new-password"
                  value={passphrase}
                  maxLength={4096}
                  onChange={(e) => setPassphrase(e.target.value)}
                />
              </label>
            </>
          )}
        </fieldset>
        {!probe && (
          <button className="primary" disabled={busy}>
            {busy ? "探测主机中…" : "检查主机指纹"}
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
