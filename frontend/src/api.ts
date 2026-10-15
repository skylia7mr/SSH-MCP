export type Session = {
  session_id: string;
  alias: string;
  host: string;
  port: number;
  username: string;
  auth_type: "password" | "key";
  private_key_saved: boolean;
  account_password_configured: boolean;
  status: string;
  sudo_enabled: boolean;
  sudo_password_configured: boolean;
  policy_version: number;
  created_at: string;
  error: string | null;
};
export type Run = {
  run_id: string;
  session_id: string;
  command: string;
  command_truncated: boolean;
  status: string;
  source: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  signal: string | null;
  duration_ms: number | null;
  sudo_used: boolean;
  error_code: string | null;
  error: string | null;
  stdout_cursor: number;
  stderr_cursor: number;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
};
export type Output = {
  run_id: string;
  stdout: string;
  stderr: string;
  next_stdout_cursor: number;
  next_stderr_cursor: number;
  complete: boolean;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
};
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
let csrf = "";
export function setCsrf(value: string) {
  csrf = value;
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch("/api" + path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(method === "GET" ? {} : { "X-CSRF-Token": csrf }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok)
    throw new ApiError(
      result.error_code ?? "REQUEST_FAILED",
      result.error ?? "请求失败",
      response.status,
    );
  return result as T;
}
export const stateName: Record<string, string> = {
  connecting: "连接中",
  connected: "已连接",
  disconnected: "已断开",
  failed: "失败",
  queued: "排队中",
  running: "执行中",
  finished: "已完成",
  timed_out: "超时",
  cancelled: "已取消",
};
export const terminal = (state: string) =>
  ["finished", "failed", "timed_out", "cancelled", "disconnected"].includes(
    state,
  );
