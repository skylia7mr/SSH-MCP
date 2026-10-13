import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, stateName, terminal, type Run, type Output } from "./api";
const plain = (text: string) =>
  text
    .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
export function RunDetail({
  runId,
  onError,
}: {
  runId: string;
  onError: (e: unknown) => void;
}) {
  const [run, setRun] = useState<Run | null>(null);
  const [output, setOutput] = useState({ stdout: "", stderr: "" });
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const [busy, setBusy] = useState(false);
  const cursors = useRef({ stdout: 0, stderr: 0 });
  const scroll = useRef<HTMLPreElement>(null);
  const follow = useRef(true);
  useEffect(() => {
    let active = true;
    let fetching = false;
    let drained = false;
    setRun(null);
    setOutput({ stdout: "", stderr: "" });
    cursors.current = { stdout: 0, stderr: 0 };
    follow.current = true;
    async function update() {
      if (fetching || drained) return;
      fetching = true;
      try {
        const info = await api<Run>(`/runs/${encodeURIComponent(runId)}`);
        if (!active) return;
        setRun(info);
        const c = cursors.current;
        const part = await api<Output>(
          `/runs/${encodeURIComponent(runId)}/output?stdout_cursor=${c.stdout}&stderr_cursor=${c.stderr}&limit=65536`,
        );
        if (!active) return;
        cursors.current = {
          stdout: part.next_stdout_cursor,
          stderr: part.next_stderr_cursor,
        };
        if (part.stdout || part.stderr)
          setOutput((old) => ({
            stdout: old.stdout + plain(part.stdout),
            stderr: old.stderr + plain(part.stderr),
          }));
        drained =
          part.complete &&
          part.next_stdout_cursor === info.stdout_cursor &&
          part.next_stderr_cursor === info.stderr_cursor;
      } catch (e) {
        if (active) onError(e);
      } finally {
        fetching = false;
      }
    }
    void update();
    const timer = setInterval(() => void update(), 250);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [runId, onError]);
  useLayoutEffect(() => {
    if (follow.current && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [output, stream]);
  if (!run)
    return <section className="run-detail muted">读取任务详情…</section>;
  return (
    <section className="run-detail" aria-label="命令结果">
      <code className="full-command">{run.command}</code>
      <div className="run-meta">
        <span>来源：{run.source}</span>
        <span>
          开始：
          {run.started_at
            ? new Date(run.started_at).toLocaleString()
            : "等待执行"}
        </span>
        <span>状态：{stateName[run.status] ?? run.status}</span>
        <span>
          退出码：{run.exit_code ?? "—"}
          {run.signal && ` · 信号 ${run.signal}`}
        </span>
        <span>
          耗时：
          {run.duration_ms === null
            ? "—"
            : `${(run.duration_ms / 1000).toFixed(2)} 秒`}
        </span>
        <span>{run.sudo_used ? "使用 sudo" : "普通权限"}</span>
      </div>
      {run.error && (
        <p className="error" role="alert">
          {run.error_code}：{run.error}
        </p>
      )}
      {!terminal(run.status) && (
        <button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              setRun(await api<Run>(`/runs/${run.run_id}/cancel`, "POST"));
            } catch (e) {
              onError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          尝试取消任务
        </button>
      )}
      <div className="output-tabs">
        <button
          aria-pressed={stream === "stdout"}
          onClick={() => setStream("stdout")}
        >
          标准输出 stdout
        </button>
        <button
          aria-pressed={stream === "stderr"}
          onClick={() => setStream("stderr")}
        >
          标准错误 stderr
        </button>
        <button
          onClick={() => {
            follow.current = true;
            if (scroll.current)
              scroll.current.scrollTop = scroll.current.scrollHeight;
          }}
        >
          跟随最新输出
        </button>
      </div>
      {(stream === "stdout" ? run.stdout_truncated : run.stderr_truncated) && (
        <p className="error">
          此流超过保留上限，输出已截断。远端流仍持续排空。
        </p>
      )}
      <pre
        ref={scroll}
        data-testid="command-output"
        onScroll={() => {
          const el = scroll.current;
          if (el)
            follow.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 32;
        }}
      >
        {output[stream] ||
          (terminal(run.status) ? "（空输出）" : "（等待输出）")}
      </pre>
    </section>
  );
}
