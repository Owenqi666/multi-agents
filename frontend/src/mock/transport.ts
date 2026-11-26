// Mock mode: no bridge. Replays a recorded sample scenario (docs/web-api/samples/<name>/,
// the format the bridge's recorder writes) and gives every submitted job one fixed timeline:
// queued for one poll, running for three, then the recorded outcome. It implements no
// business rules; a request the scenario never made answers 404.
//
// Reads answer from the recording up to a position (`?at=`, by default its end). Job
// submissions do not move it: a job replays the recorded job of that action through the
// fixed timeline, and the records around it stay as they were read at that position.
//
// A judgement on a finding is the one write that is not a job. Only the recorded judgement
// is replayed (same round, same finding of the same seat and attempt, same decision and
// reason); the position then moves to the reads the recording made right after it, so the
// findings read next include it. A follow-up question and a linked correction are jobs,
// but they speak about one finding in the user's own words, so they too are replayed only
// for the recorded finding (and, for the question, the recorded wording). Whatever the
// recording does not hold is refused in words that say this is a demonstration.

import { ApiError, type Query, type Transport } from '../api/transport';
import type { Job } from '../api/types';
import { tx, joinList } from '../i18n';

interface Entry {
  seq: number;
  method: string;
  path: string;
  query: string | null;
  /** A job submission ({action, args}) or a judgement on a finding ({anchor, decision, reason}). */
  request_body: { action?: string; args?: Record<string, unknown> & { review_anchor?: unknown; question?: unknown; provider?: unknown }; anchor?: unknown; decision?: unknown; reason?: unknown } | null;
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

/** The same value whatever the order of its keys. */
const same = (a: unknown, b: unknown): boolean => {
  const flat = (v: unknown): string => JSON.stringify(v && typeof v === 'object' && !Array.isArray(v)
    ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([x], [y]) => x.localeCompare(y))) : v);
  return flat(a) === flat(b);
};

const DECISION: Record<string, string> = { confirmed: '确认存在', disputed: '存疑', deferred: '暂缓处理' };

const QUEUED_POLLS = 1;
const RUNNING_POLLS = 3;

export class MockTransport implements Transport {
  private entries: Entry[] | null = null;
  private loading: Promise<Entry[]> | null = null;
  private lastPost = 0;
  private timelines = new Map<string, Timeline>();
  /** Reads answer from the requests recorded up to here. */
  private position: number;

  /** `at` starts the replay at that request number; by default (and for anything that is not
   *  a positive number) at the end of the recording. */
  constructor(private readonly scenario: string, at = Infinity) {
    this.position = at > 0 ? at : Infinity;
  }

  /** The name of the recording, for the page to say that it is one. */
  get demoScenario(): string {
    return this.scenario;
  }

  private async all(): Promise<Entry[]> {
    if (this.entries) return this.entries;
    this.loading ??= (async () => {
      const p = load<Entry[]>(indexes, this.scenario, 'index.json');
      if (!p) throw new ApiError(404, 'not_found', tx('样例 {0} 不存在。可选：{1}', this.scenario, joinList(scenarios()) || tx('（未找到离线样例）')));
      this.entries = await p;
      return this.entries;
    })();
    return this.loading;
  }

  /** The requests the replay has reached. */
  private async reached(): Promise<Entry[]> {
    return (await this.all()).filter((e) => e.seq <= this.position);
  }

  private async body<T>(e: Entry): Promise<T> {
    const p = load<T>(files, this.scenario, e.response_file);
    if (!p) throw new ApiError(500, 'workspace_unavailable', tx('样例缺少 {0}', e.response_file));
    const v = await p;
    if (e.status >= 400) {
      const err = (v as { error?: { code?: string; message?: string; details?: Record<string, unknown> } }).error;
      throw new ApiError(e.status, err?.code ?? 'http_error', err?.message ?? '', err?.details ?? null);
    }
    return structuredClone(v) as T;
  }

  async get<T>(path: string, query?: Query): Promise<T> {
    const entries = await this.reached();
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null) q.set(k, String(v));
    if (path === '/jobs') return { items: [...this.timelines.values()].map((t) => this.view(t, false)).reverse() } as T;
    const job = /^\/jobs\/([^/]+)$/.exec(path);
    if (job && this.timelines.has(decodeURIComponent(job[1]!))) return this.view(this.timelines.get(decodeURIComponent(job[1]!))!, true) as T;
    const want = canonical(`/api${path}`, q);
    const hits = entries.filter((e) => e.method === 'GET' && canonical(e.path, e.query) === want);
    const pathHits = hits.length ? hits : entries.filter((e) => e.method === 'GET' && e.path === `/api${path}`);
    const hit = pathHits[pathHits.length - 1];
    if (!hit) throw new ApiError(404, 'not_found', tx('模拟样例里没有这次读取。'));
    return this.body<T>(hit);
  }

  async post<T>(path: string, body: unknown): Promise<T> {
    const entries = await this.all();
    const judged = /^\/reviews\/([^/]+)\/decisions$/.exec(path);
    if (judged) return this.judgement<T>(entries, path, body as Entry['request_body']);
    const cancel = /^\/jobs\/([^/]+)\/cancel$/.exec(path);
    if (cancel) {
      const t = this.timelines.get(decodeURIComponent(cancel[1]!));
      if (!t || this.state(t) !== 'running') throw new ApiError(409, 'job_unavailable', tx('只有运行中的作业可以取消。'));
      t.cancelled = true;
      return { job_id: t.job.job_id, state: 'cancelling' } as T;
    }
    const sent = body as NonNullable<Entry['request_body']>;
    const action = sent.action;
    const reached = entries.filter((e) => e.seq <= this.position);
    let posts = reached.filter((e) => e.method === 'POST' && e.path === '/api/jobs' && e.request_body?.action === action);
    if (!posts.length) throw this.refusal(tx('回放到这一步时，录制的样例里还没有「{0}」这次提交。', action ?? ''));
    // What the user writes about one finding is replayed only for the recorded finding, and a
    // question only in the recorded wording: the recorded answer answers nothing else.
    const anchor = sent.args?.review_anchor;
    if (anchor !== undefined) {
      const about = posts.filter((e) => same(e.request_body?.args?.review_anchor, anchor));
      if (!about.length) throw this.refusal(tx('录制的样例里没有对这条意见做过这个操作。'));
      posts = about;
      if (action === 'review.followup') {
        const asked = posts.filter((e) => e.request_body?.args?.question === sent.args?.question);
        if (!asked.length) throw this.refusal(tx('只能回放录制过的那一次追问，问题要照原样填：「{0}」', String(posts[0]!.request_body?.args?.question ?? '')));
        posts = asked;
      }
    }
    // An environment check belongs to one provider.
    if (sent.args && 'provider' in sent.args) {
      posts = posts.filter((e) => e.request_body?.args?.provider === sent.args?.provider);
      if (!posts.length) throw this.refusal(tx('录制的样例里没有检查过这个提供方。'));
    }
    const hit = posts.find((e) => e.seq > this.lastPost) ?? posts[posts.length - 1]!;
    this.lastPost = hit.seq;
    const receipt = await this.body<{ job_id: string; state: string }>(hit);
    const polls = reached.filter((e) => e.method === 'GET' && e.path === `/api/jobs/${receipt.job_id}`);
    const final = polls.length ? await this.body<Job>(polls[polls.length - 1]!) : null;
    const job: Job = final ?? { job_id: receipt.job_id, action: action ?? '', state: 'finished', exit_code: 0, result: null, error: null };
    this.timelines.set(receipt.job_id, { job, polls: 0, cancelled: false });
    return receipt as T;
  }

  /** A write the recording does not hold. The page shows the message of an invalid request. */
  private refusal(why: string): ApiError {
    return new ApiError(400, 'invalid_request', `${tx('这是演示。')}${why}`);
  }

  /** A judgement on a finding. Only the judgement the recording holds is replayed: the same
   *  round, the same finding of the same seat and attempt, the same decision and reason.
   *  Anything else is refused, in words that say why. */
  private async judgement<T>(entries: Entry[], path: string, sent: Entry['request_body']): Promise<T> {
    const recorded = entries.filter((e) => e.method === 'POST' && e.path === `/api${path}` && same(e.request_body?.anchor, sent?.anchor));
    const hit = recorded.find((e) => e.request_body?.decision === sent?.decision && e.request_body?.reason === sent?.reason);
    if (!hit) {
      const known = recorded[0]?.request_body;
      throw this.refusal(known
        ? tx('只能回放录制过的那一次判断：选「{0}」，理由填「{1}」。别的判断不会被记录。', tx(DECISION[String(known.decision)] ?? String(known.decision)), String(known.reason))
        : tx('录制的样例里没有对这条意见记录过判断。'));
    }
    // What is read next is what the recording read right after this judgement: the reads up
    // to its next write, and no further.
    const next = entries.find((e) => e.method === 'POST' && e.seq > hit.seq);
    this.position = Math.max(this.position, next ? next.seq - 1 : Infinity);
    return this.body<T>(hit);
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
      // Cancelled here, not in the recording: what the recorded job produced did not happen.
      return { ...t.job, exit_code: 130, result: null, error: { code: 'cli_cancelled', message: 'cancelled' }, error_kind: 'cancelled', resources: [], artifacts: null, stderr_tail: '' };
    }
    return t.job;
  }
}
