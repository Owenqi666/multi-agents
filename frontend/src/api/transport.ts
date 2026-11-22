// The only way the pages talk to the bridge: same-origin requests under /api. Ids are opaque
// and only ever URL-encoded into the path, never parsed or joined into anything else.

import { tx } from '../i18n';
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> | null = null,
  ) {
    super(message);
  }

  /** 500 and network failures can be retried; the rest are answers. */
  get retryable(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export interface Transport {
  get<T>(path: string, query?: Query): Promise<T>;
  post<T>(path: string, body: unknown): Promise<T>;
}

export function seg(id: string): string {
  return encodeURIComponent(id);
}

function search(query?: Query): string {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) params.set(k, String(v));
  const s = params.toString();
  return s ? `?${s}` : '';
}

async function parse<T>(res: Response): Promise<T> {
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const err = (body as { error?: { code?: string; message?: string; details?: Record<string, unknown> } } | null)?.error;
    throw new ApiError(res.status, err?.code ?? 'http_error', err?.message ?? `HTTP ${res.status}`, err?.details ?? null);
  }
  return body as T;
}

export const http: Transport = {
  async get<T>(path: string, query?: Query): Promise<T> {
    let res: Response;
    try {
      res = await fetch(`/api${path}${search(query)}`, { headers: { accept: 'application/json' } });
    } catch (e) {
      throw new ApiError(0, 'network', String(e));
    }
    return parse<T>(res);
  },
  async post<T>(path: string, body: unknown): Promise<T> {
    let res: Response;
    try {
      res = await fetch(`/api${path}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (e) {
      throw new ApiError(0, 'network', String(e));
    }
    return parse<T>(res);
  },
};

/** Error codes in words, for the pages. The message stays available as detail. */
export function errorText(e: unknown): string {
  if (!(e instanceof ApiError)) return String(e);
  switch (e.code) {
    case 'busy': return tx('已有作业在运行，等它结束后再提交。');
    case 'environment_busy': return tx('这个提供方的环境检查正在运行。');
    case 'job_unavailable': return tx('现在不能执行这个操作。');
    case 'not_found': return tx('找不到这条记录。');
    case 'archive_read_only': return tx('归档工作区为只读。');
    case 'archive_unsupported': return tx('此归档版本不支持这个记录类型。 {0}', e.message);
    case 'invalid_request': case 'unknown_action': case 'invalid_provider': return tx('请求不对，服务没有接受。');
    case 'invalid_host': case 'invalid_origin': return tx('请从服务打印的 http://127.0.0.1:<端口> 地址打开页面。');
    case 'workspace_unavailable': return tx('服务读取工作区失败，可以重试。');
    case 'network': return tx('连不上本机服务，确认它还在运行。');
    default: return e.message;
  }
}
