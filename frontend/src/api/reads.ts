// Reading records with the lock rules of the contract:
// - a locking read is not sent while a CLI job is unfinished; the last content stays on
//   screen with the time it was read, or the page says it can be read after the job;
// - a lock_busy answer is treated the same way;
// - locking reads go out one at a time, never in parallel;
// - other records may be re-read while a job runs, at most every 5 seconds.

import { useEffect, useRef, useState } from 'react';
import { useJobs } from './jobs';

export interface ReadState<T> {
  data: T | null;
  /** When data was read (ISO). */
  readAt: string | null;
  /** True when what is shown is older than the record, because the lock was held. */
  stale: boolean;
  error: unknown;
  loading: boolean;
}

interface Cached {
  data: unknown;
  readAt: string;
}

const cache = new Map<string, Cached>();
let chain: Promise<unknown> = Promise.resolve();

function serial<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => undefined);
  return next;
}

/** Whether a response is the bridge's lock_busy answer rather than the record. */
export function lockBusy(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const v = value as { lock_busy?: boolean; unavailable_code?: string };
  return v.lock_busy === true || v.unavailable_code === 'lock_busy';
}

export interface ReadOptions {
  locking: boolean;
  /** Re-read every so often while a job runs (non-locking reads only). */
  pollWhileJob?: boolean;
  /** Skip the read entirely (missing id, collapsed panel). */
  enabled?: boolean;
}

const POLL_MS = 5000;

export function useRead<T>(key: string | null, fetcher: () => Promise<T>, opts: ReadOptions): ReadState<T> & { reload: () => void } {
  const { active, generation } = useJobs();
  const locked = active !== null;
  const enabled = (opts.enabled ?? true) && key !== null;
  const cached = key ? cache.get(key) : undefined;
  const [state, setState] = useState<ReadState<T>>({
    data: (cached?.data as T | undefined) ?? null,
    readAt: cached?.readAt ?? null,
    stale: false,
    error: null,
    loading: false,
  });
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!enabled || !key) return;
    let alive = true;
    const fromCache = (): void => {
      const c = cache.get(key);
      setState((s) => ({ ...s, data: (c?.data as T | undefined) ?? null, readAt: c?.readAt ?? null, stale: true, loading: false }));
    };
    const run = async (): Promise<void> => {
      if (opts.locking && locked) { fromCache(); return; }
      setState((s) => ({ ...s, loading: true }));
      try {
        const value = await (opts.locking ? serial(() => fetchRef.current()) : fetchRef.current());
        if (!alive) return;
        if (lockBusy(value)) { fromCache(); return; }
        const readAt = new Date().toISOString();
        cache.set(key, { data: value, readAt });
        setState({ data: value, readAt, stale: false, error: null, loading: false });
      } catch (error) {
        if (!alive) return;
        const c = cache.get(key);
        setState({ data: (c?.data as T | undefined) ?? null, readAt: c?.readAt ?? null, stale: !!c, error, loading: false });
      }
    };
    void run();
    let timer: ReturnType<typeof setInterval> | null = null;
    if (opts.pollWhileJob && !opts.locking && locked) timer = setInterval(() => void run(), POLL_MS);
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [key, enabled, locked, generation, nonce, opts.locking, opts.pollWhileJob]);

  return { ...state, reload: () => setNonce((n) => n + 1) };
}

/** Test hook: forget everything read so far. */
export function clearReadCache(): void {
  cache.clear();
}
