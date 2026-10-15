import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  api,
  ApiError,
  setCsrf,
  stateName,
  type Session,
  type Run,
} from "./api";
import { NewConnection } from "./NewConnection";
import { McpSetup } from "./McpSetup";
import { RunDetail } from "./RunDetail";
export default function App() {
  const [auth, setAuth] = useState(false);
  const [checking, setChecking] = useState(true);
  const [online, setOnline] = useState(false);
  const [code, setCode] = useState("");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState("");
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runId, setRunId] = useState("");
  const [limit, setLimit] = useState(100);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<"new" | "mcp" | "reconnect" | "edit" | null>(null);
  const [busy, setBusy] = useState(false);
  const refreshing = useRef(false);
  const sessionRevision = useRef(0);
  const handleError = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) {
      sessionRevision.current += 1;
      setAuth(false);
      setCsrf("");
      setSessions([]);
      setRuns([]);
      setDialog(null);
      setError(
        "后端会话已失效，正在刷新页面。请用启动终端中的新配对码重新配对。",
      );
      window.location.reload();
    } else setError(e instanceof Error ? e.message : "请求失败");
  }, []);
  const refresh = useCallback(async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    const revision = sessionRevision.current;
    try {
      const list = await api<Session[]>("/sessions");
      if (revision !== sessionRevision.current) return;
      setOnline(true);
      setSessions(list);
      let id = selectedRef.current;
      if (!list.some((s) => s.session_id === id)) {
        id = list[0]?.session_id ?? "";
        setSelected(id);
        selectedRef.current = id;
      }
      if (id) {
        const result = await api<Run[]>(
          `/runs?session_id=${encodeURIComponent(id)}&limit=${limit}`,
        );
        if (selectedRef.current !== id || revision !== sessionRevision.current)
          return;
        setRuns(result);
        setRunsLoading(false);
        setRunId((old) =>
          result.some((r) => r.run_id === old)
            ? old
            : (result[0]?.run_id ?? ""),
        );
      } else {
        setRuns([]);
        setRunId("");
      }
    } catch (e) {
      setOnline(false);
      handleError(e);
    } finally {
      refreshing.current = false;
    }
  }, [handleError, limit]);
  useEffect(() => {
    void fetch("/healthz", { cache: "no-store" })
      .then((r) => setOnline(r.ok))
      .catch(() => setOnline(false));
    api<{ csrf_token: string }>("/auth/session")
      .then((data) => {
        setCsrf(data.csrf_token);
        setAuth(true);
        setOnline(true);
      })
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) handleError(e);
      })
      .finally(() => setChecking(false));
  }, [handleError]);
  useEffect(() => {
    if (!auth) return;
    void refresh();
    const events = new EventSource("/api/events");
    events.addEventListener("refresh", () => void refresh());
    events.onopen = () => setOnline(true);
    events.onerror = () => {
      setOnline(false);
      void refresh();
    };
    return () => events.close();
  }, [auth, refresh]);
  useEffect(() => {
    if (auth) {
      setRunId("");
      setRuns([]);
      setRunsLoading(Boolean(selected));
      void refresh();
    }
  }, [selected, auth, refresh]);
  async function pair(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const value = code;
    setCode("");
    try {
      const data = await api<{ csrf_token: string }>("/auth/pair", "POST", {
        code: value,
      });
      setCsrf(data.csrf_token);
      setAuth(true);
      setOnline(true);
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(false);
    }
  }
  const current = sessions.find((s) => s.session_id === selected);
  const setSession = (s: Session) => {
    sessionRevision.current += 1;
    setSessions((old) =>
      old.map((x) => (x.session_id === s.session_id ? s : x)),
    );
  };
  async function policy(enabled: boolean) {
    if (!current) return;
    setBusy(true);
    setError("");
    try {
      setSession(
        await api<Session>(`/sessions/${current.session_id}/sudo`, "PUT", {
          enabled,
        }),
      );
    } catch (e) {
      handleError(e);
      void refresh();
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <strong>SSH MCP</strong>
          <span>本机连接工作台</span>
        </div>
        <span className={`backend ${online ? "ok" : "muted"}`}>
          {online ? "● 后端在线" : "○ 后端未连接"}
        </span>
        {auth && (
          <>
            <button onClick={() => setDialog("mcp")}>Codex / MCP 接入</button>
            <button className="primary" onClick={() => setDialog("new")}>
              ＋ 新建连接
            </button>
          </>
        )}
      </header>
      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button aria-label="关闭错误提示" onClick={() => setError("")}>
            关闭
          </button>
        </div>
      )}
      {!auth ? (
        <main className="pairing">
          <h1>连接本机工作台</h1>
          <p>输入启动终端显示的一次性配对码。网页会话在后端重启后失效。</p>
          {checking ? (
            <p>正在检查会话…</p>
          ) : (
            <form onSubmit={pair}>
              <label>
                本机配对码
                <input
                  required
                  inputMode="numeric"
                  autoComplete="off"
                  minLength={6}
                  maxLength={6}
                  pattern="[0-9]{6}"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </label>
              <button className="primary" disabled={busy}>
                {busy ? "配对中…" : "配对并进入"}
              </button>
            </form>
          )}
        </main>
      ) : (
        <div className="workbench">
          <aside className="sidebar">
            <h2>
              SSH 连接 <span className="muted">{sessions.length}</span>
            </h2>
            {sessions.length === 0 ? (
              <p className="muted">
                还没有连接。点击“新建连接”，确认主机指纹后登录。
              </p>
            ) : (
              sessions.map((s) => (
                <button
                  className="connection"
                  key={s.session_id}
                  aria-pressed={s.session_id === selected}
                  onClick={() => {
                    setSelected(s.session_id);
                    setLimit(100);
                  }}
                >
                  <strong>{s.alias}</strong>
                  <small>
                    {s.username}@{s.host}:{s.port}
                  </small>
                  <span className={s.status === "connected" ? "ok" : "muted"}>
                    {stateName[s.status] ?? s.status}
                  </span>
                </button>
              ))
            )}
          </aside>
          <main className="main">
            {!current ? (
              <div className="empty">
                <h1>从一条真实 SSH 连接开始</h1>
                <p>
                  连接后，Agent 可以通过 MCP
                  执行命令。执行记录与输出会在这里实时显示。
                </p>
                <button className="primary" onClick={() => setDialog("new")}>
                  新建连接
                </button>
              </div>
            ) : (
              <>
                <div className="title-row">
                  <div>
                    <h1>{current.alias}</h1>
                    <p className="muted">
                      {current.username}@{current.host}:{current.port}
                    </p>
                    <p className="muted">
                      连接 ID：<code>{current.session_id}</code>
                    </p>
                  </div>
                  <span
                    className={current.status === "connected" ? "ok" : "muted"}
                  >
                    {stateName[current.status]}
                  </span>
                  {current.status !== "connected" && current.status !== "connecting" && (
                    <button className="primary" onClick={() => setDialog("reconnect")}>重连</button>
                  )}
                  <button disabled={busy || current.status === "connecting"} onClick={() => setDialog("edit")}>编辑连接</button>
                  <button
                    disabled={busy || current.status !== "connected"}
                    onClick={async () => {
                      setBusy(true);
                      try {
                        setSession(
                          await api<Session>(
                            `/sessions/${current.session_id}/disconnect`,
                            "POST",
                          ),
                        );
                        void refresh();
                      } catch (e) {
                        handleError(e);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    断开连接
                  </button>
                  {current.status !== "connecting" && (
                    <button
                      onClick={async () => {
                        try {
                          await api(
                            `/sessions/${current.session_id}`,
                            "DELETE",
                          );
                          void refresh();
                        } catch (e) {
                          handleError(e);
                        }
                      }}
                    >
                      删除连接
                    </button>
                  )}
                </div>
                {current.error && (
                  <p className="error">
                    连接状态：{current.error}
                    。请重新登录，旧命令不会自动重放。
                  </p>
                )}
                <section className="policy">
                  <label className="switch">
                    <input
                      type="checkbox"
                      checked={current.sudo_enabled}
                      disabled={busy || current.status !== "connected"}
                      onChange={(e) => void policy(e.target.checked)}
                    />
                    允许 sudo
                  </label>
                  <span className="muted">
                    {current.sudo_password_configured
                      ? "使用登录密码"
                      : current.sudo_enabled ? "未提供账户密码；仅可使用免密 sudo" : "开启后复用登录密码"}{" "}
                    · {current.sudo_enabled ? "sudo 已开启" : "sudo 已关闭"}
                  </span>
                </section>
                <p className="boundary">
                  开关控制可解析的 sudo 调用；脚本或程序的间接提权仍由远端账号和
                  sudoers 限制。关闭会撤销后续任务的 sudo 授权；断开连接时清除内存中的账户密码。
                </p>
                <h2 className="section-title">Agent 命令记录</h2>
                {runsLoading ? (
                  <div className="empty small">正在读取当前连接的命令记录…</div>
                ) : runs.length === 0 ? (
                  <div className="empty small">
                    <p>
                      当前连接还没有 Agent 命令。打开“Codex / MCP
                      接入”，配置客户端后选择此连接执行。
                    </p>
                  </div>
                ) : (
                  <div className="run-layout">
                    <nav className="runs" aria-label="Agent 命令记录">
                      {runs.map((r) => (
                        <button
                          key={r.run_id}
                          aria-pressed={r.run_id === runId}
                          onClick={() => setRunId(r.run_id)}
                        >
                          <code>
                            {r.command.slice(0, 160)}
                            {r.command.length > 160 || r.command_truncated
                              ? "…"
                              : ""}
                          </code>
                          <small>
                            {new Date(
                              r.started_at ?? r.created_at,
                            ).toLocaleTimeString()}{" "}
                            · {stateName[r.status]} · {r.exit_code ?? "—"} ·{" "}
                            {r.sudo_used ? "sudo" : "普通"}
                          </small>
                        </button>
                      ))}
                      {runs.length >= limit && limit < 1000 && (
                        <button
                          onClick={() =>
                            setLimit((n) => Math.min(n + 100, 1000))
                          }
                        >
                          加载更早记录
                        </button>
                      )}
                    </nav>
                    {runId && (
                      <RunDetail
                        key={runId}
                        runId={runId}
                        onError={handleError}
                      />
                    )}
                  </div>
                )}
              </>
            )}
          </main>
        </div>
      )}
      <footer>
        连接配置与私钥保存在本机 · 密码不落盘 · 重启后重新登录即可 · 关闭浏览器不会断开 SSH
      </footer>
      {(dialog === "new" || ((dialog === "reconnect" || dialog === "edit") && current)) && (
        <NewConnection
          existing={dialog !== "new" ? current : undefined}
          editing={dialog === "edit"}
          onClose={() => setDialog(null)}
          onConnected={(s) => {
            sessionRevision.current += 1;
            selectedRef.current = s.session_id;
            setSessions((old) => [
              ...old.filter((x) => x.session_id !== s.session_id && x.session_id !== (dialog === "edit" ? current?.session_id : undefined)),
              s,
            ]);
            setSelected(s.session_id);
            void refresh();
          }}
        />
      )}
      {dialog === "mcp" && <McpSetup onClose={() => setDialog(null)} />}
    </div>
  );
}
