import { stateName, terminal, type Session, type Transfer } from "./api";

function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
}

export function TransferHistory({
  records,
  sessions,
  loading,
  limit,
  onLoadMore,
}: {
  records: Transfer[];
  sessions: Session[];
  loading: boolean;
  limit: number;
  onLoadMore: () => void;
}) {
  function endpoint(sessionId: string | null): string {
    if (sessionId === null) return "工作台本地目录";
    const session = sessions.find((item) => item.session_id === sessionId);
    return session
      ? `${session.alias}（${session.username}@${session.host}:${session.port}）`
      : `连接 ${sessionId}`;
  }

  return (
    <section className="transfer-section" aria-label="文件传输记录">
      <h2 className="section-title">文件传输记录</h2>
      <p className="boundary">显示当前连接作为来源或目标的 MCP 传输。记录仅保留在内存中，重启或到期后清除。</p>
      {loading ? (
        <div className="empty small">正在读取当前连接的传输记录…</div>
      ) : records.length === 0 ? (
        <div className="empty small">当前连接还没有文件传输记录。</div>
      ) : (
        <ul className="transfer-history">
          {records.map((record) => (
            <li className="transfer-record" key={record.transfer_id}>
              <div className="run-meta">
                <strong className={record.status === "finished" ? "ok" : record.error ? "error" : ""}>
                  {stateName[record.status] ?? record.status}
                </strong>
                <span>{record.recursive ? "目录" : "文件"} · {record.engine}</span>
                <time dateTime={record.created_at}>{new Date(record.created_at).toLocaleString()}</time>
                {record.finished_at && <span>结束于 {new Date(record.finished_at).toLocaleString()}</span>}
                <span>{record.overwrite ? "允许覆盖" : "不覆盖已有文件"}</span>
              </div>
              <div className="transfer-endpoints">
                <div><span className="muted">来源：{endpoint(record.source_session_id)}</span><code>{record.source_path}</code></div>
                <span aria-hidden="true">→</span>
                <div><span className="muted">目标：{endpoint(record.target_session_id)}</span><code>{record.target_path}</code></div>
              </div>
              <div className="run-meta">
                <span>
                  {record.total_bytes === null ? `已传输 ${bytes(record.copied_bytes)}` : `${bytes(record.copied_bytes)} / ${bytes(record.total_bytes)}`}
                </span>
                {!terminal(record.status) && <span>{({ queued: "等待传输", downloading: "正在下载", uploading: "正在上传" } as Record<string, string>)[record.phase] ?? "正在传输"}</span>}
                <span>传输 ID：<code>{record.transfer_id}</code></span>
              </div>
              {record.total_bytes !== null && record.total_bytes > 0 && (
                <progress aria-label="传输进度" max={record.total_bytes} value={record.copied_bytes} />
              )}
              {record.error && <p className="error">{record.error_code}：{record.error}</p>}
            </li>
          ))}
        </ul>
      )}
      {!loading && records.length >= limit && limit < 1000 && (
        <button onClick={onLoadMore}>加载更早传输记录</button>
      )}
    </section>
  );
}
