// A transport that answers from a table, records every request, and refuses the rest.
import { ApiError, type Query, type Transport } from '../api/transport';

export type Answer = unknown | ((query: Query | undefined) => unknown);

export function fakeTransport(gets: Record<string, Answer>, posts: Record<string, Answer> = {}): Transport & { calls: string[] } {
  const calls: string[] = [];
  const answer = (table: Record<string, Answer>, path: string, arg?: Query): unknown => {
    if (!(path in table)) throw new ApiError(404, 'not_found', `no fake answer for ${path}`);
    const a = table[path];
    const v = typeof a === 'function' ? (a as (q?: Query) => unknown)(arg) : a;
    if (v instanceof ApiError) throw v;
    return structuredClone(v);
  };
  return {
    calls,
    async get<T>(path: string, query?: Query): Promise<T> {
      calls.push(`GET ${path}`);
      return answer(gets, path, query) as T;
    },
    async post<T>(path: string, body: unknown): Promise<T> {
      calls.push(`POST ${path} ${(body as { action?: string })?.action ?? ''}`.trim());
      return answer(posts, path, body as Query) as T;
    },
  };
}
