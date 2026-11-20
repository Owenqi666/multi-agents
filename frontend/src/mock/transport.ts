// Mock mode: no bridge. Replays a recorded sample scenario (docs/web-api/samples/<name>/,
// the format the bridge's recorder writes) and gives every submitted job one fixed timeline:
// queued for one poll, running for three, then the recorded outcome. It implements no
// business rules; a request the scenario never made answers 404.

import { ApiError, type Query, type Transport } from '../api/transport';
import type { Job } from '../api/types';

interface Entry {
  seq: number;
  method: string;
  path: string;
  query: string | null;
  request_body: { action?: string; args?: Record<string, unknown> } | null;
  status: number;
  response_file: string;
}

const indexes = import.meta.glob<Entry[]>('@samples/*/index.json', { import: 'default' });
const files = import.meta.glob<unknown>('@samples/*/*.json', { import: 'default' });

function load<T>(table: Record<string, () => Promise<unknown>>, scenario: string, file: string): Promise<T> | null {
  const key = Object.keys(table).find((k) => k.endsWith(`/${scenario}/${file}`));
  return key ? (table[key]!() as Promise<T>) : null;
}

export function scenarios(): string[] {
  return Object.keys(indexes).map((k) => k.split('/').slice(-2, -1)[0]!).sort();
}

function canonical(path: string, query: string | URLSearchParams | null): string {
  const params = new URLSearchParams(query ?? '');
  if (path.startsWith('/api/artifacts/')) params.delete('limit');
  const sorted = [...params.entries()].sort(([a], [b]) => a.localeCompare(b));
  return `${path}?${new URLSearchParams(sorted).toString()}`;
}

interface Timeline {
  job: Job;
  polls: number;
  cancelled: boolean;
}

const QUEUED_POLLS = 1;
const RUNNING_POLLS = 3;

export class MockTransport implements Transport {
  private entries: Entry[] | null = null;
  private loading: Promise<Entry[]> | null = null;
  private lastPost = 0;
  private timelines = new Map<string, Timeline>();

  /** `at` replays the scenario only up to that request number; by default all of it. */
  constructor(private readonly scenario: string, private readonly at = Infinity) {}

  private async all(): Promise<Entry[]> {
    if (this.entries) return this.entries;
    this.loading ??= (async () => {
      const p = load<Entry[]>(indexes, this.scenario, 'index.json');
      if (!p) throw new ApiError(404, 'not_found', `样例 ${this.scenario} 不存在。可选：${scenarios().join('、') || '（还没有样例，T-0069 合入后提供）'}`);
      this.entries = (await p).filter((e) => e.seq <= this.at);
      return this.entries;
    })();
    return this.loading;
  }

  private async body<T>(e: Entry): Promise<T> {
    const p = load<T>(files, this.scenario, e.response_file);
    if (!p) throw new ApiError(500, 'workspace_unavailable', `样例缺少 ${e.response_file}`);
    const v = await p;
    if (e.status >= 400) {
      const err = (v as { error?: { code?: string; message?: string; details?: Record<string, unknown> } }).error;
      throw new ApiError(e.status, err?.code ?? 'http_error', err?.message ?? '', err?.details ?? null);
    }
    return structuredClone(v) as T;
  }

  async get<T>(path: string, query?: Query): Promise<T> {
    const entries = await this.all();
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null) q.set(k, String(v));
    if (path === '/jobs') return { items: [...this.timelines.values()].map((t) => this.view(t, false)).reverse() } as T;
    const job = /^\/jobs\/([^/]+)$/.exec(path);
    if (job && this.timelines.has(decodeURIComponent(job[1]!))) return this.view(this.timelines.get(decodeURIComponent(job[1]!))!, true) as T;
    const want = canonical(`/api${path}`, q);
    const hits = entries.filter((e) => e.method === 'GET' && canonical(e.path, e.query) === want);
    const pathHits = hits.length ? hits : entries.filter((e) => e.method === 'GET' && e.path === `/api${path}`);
    const hit = pathHits[pathHits.length - 1];
    if (!hit) throw new ApiError(404, 'not_found', '模拟样例里没有这次读取。');
    return this.body<T>(hit);
  }

  async post<T>(path: string, body: unknown): Promise<T> {
    const entries = await this.all();
    const cancel = /^\/jobs\/([^/]+)\/cancel$/.exec(path);
    if (cancel) {
      const t = this.timelines.get(decodeURIComponent(cancel[1]!));
      if (!t || this.state(t) !== 'running') throw new ApiError(409, 'job_unavailable', '只有运行中的作业可以取消。');
      t.cancelled = true;
      return { job_id: t.job.job_id, state: 'cancelling' } as T;
    }
    const action = (body as { action?: string }).action;
    const posts = entries.filter((e) => e.method === 'POST' && e.path === '/api/jobs' && e.request_body?.action === action);
    const hit = posts.find((e) => e.seq > this.lastPost) ?? posts[posts.length - 1];
    if (!hit) throw new ApiError(400, 'unknown_action', `模拟样例里没有提交过 ${action}。`);
    this.lastPost = hit.seq;
    const receipt = await this.body<{ job_id: string; state: string }>(hit);
    const polls = entries.filter((e) => e.method === 'GET' && e.path === `/api/jobs/${receipt.job_id}`);
    const final = polls.length ? await this.body<Job>(polls[polls.length - 1]!) : null;
    const job: Job = final ?? { job_id: receipt.job_id, action: action ?? '', state: 'finished', exit_code: 0, result: null, error: null };
    this.timelines.set(receipt.job_id, { job, polls: 0, cancelled: false });
    return receipt as T;
  }

  private state(t: Timeline): Job['state'] {
    if (t.polls < QUEUED_POLLS) return 'queued';
    if (t.polls < QUEUED_POLLS + RUNNING_POLLS) return t.cancelled ? 'cancelling' : 'running';
    return 'finished';
  }

  /** The job as the timeline says now; each poll moves it one step. */
  private view(t: Timeline, poll: boolean): Job {
    const state = this.state(t);
    if (poll) t.polls += 1;
    if (state !== 'finished') {
      return { ...t.job, state, exit_code: null, result: null, error: null, artifacts: null, stderr_tail: null, finished_at: null,
        cancel_requested_at: t.cancelled ? t.job.cancel_requested_at ?? new Date().toISOString() : null };
    }
    if (t.cancelled && t.job.exit_code !== 130) {
      return { ...t.job, exit_code: 130, result: null, error: { code: 'cli_cancelled', message: 'cancelled' }, stderr_tail: '' };
    }
    return t.job;
  }
}
