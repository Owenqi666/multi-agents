// Browser end-to-end support: the offline integration environment from T-0069 and a network
// recorder that checks the lock rules from the outside.

import { spawn, type ChildProcess } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { expect, type Locator, type Page } from '@playwright/test';

const here = dirname(fileURLToPath(import.meta.url));
export const REPO = resolve(here, '../..');
const DIST = resolve(REPO, 'frontend/dist');
export const RECORDS = process.env.WORKBENCH_E2E_EVIDENCE ?? resolve(here, 'records');

/** The only way the tests touch the environment: this command, signals, and /api. */
export class OfflineEnv {
  private lines: string[] = [];
  private waiting: ((line: string) => void)[] = [];
  url = '';
  standalonePlans: { codex: string; claude: string } | null = null;

  private constructor(private readonly proc: ChildProcess, readonly scenario: string) {
    createInterface({ input: proc.stdout! }).on('line', (line) => {
      const w = this.waiting.shift();
      if (w) w(line);
      else this.lines.push(line);
    });
    proc.stderr?.on('data', () => undefined);
  }

  private nextLine(timeoutMs = 120_000): Promise<string> {
    const ready = this.lines.shift();
    if (ready !== undefined) return Promise.resolve(ready);
    return new Promise((res, rej) => {
      const t = setTimeout(() => rej(new Error('offline env printed no address')), timeoutMs);
      this.waiting.push((l) => { clearTimeout(t); res(l); });
    });
  }

  static async start(scenario: string): Promise<OfflineEnv> {
    let fixture = 'tests/unit/web_support/offline_env.py';
    if (['review-fixes', 'claim'].includes(scenario)) fixture = 'frontend/e2e/review_fixes_env.py';
    if (scenario === 'task-evidence') fixture = 'frontend/e2e/task_evidence_env.py';
    const proc = spawn('uv', ['run', 'python', fixture, '--scenario', scenario, '--port', '0', '--frontend-dist', DIST], {
      cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'],
    });
    const env = new OfflineEnv(proc, scenario);
    if (scenario === 'task-evidence') env.standalonePlans = JSON.parse(await env.nextLine()) as NonNullable<OfflineEnv['standalonePlans']>;
    env.url = (await env.nextLine()).trim();
    return env;
  }

  /** SIGUSR1: hard-stop the service and its CLI children, then restart on the same port. */
  async hardRestart(): Promise<void> {
    this.proc.kill('SIGUSR1');
    this.url = (await this.nextLine()).trim();
  }

  async stop(): Promise<void> {
    if (this.proc.exitCode !== null) return;
    const done = new Promise((r) => this.proc.once('exit', r));
    this.proc.kill('SIGINT');
    await Promise.race([done, new Promise((r) => setTimeout(r, 20_000))]);
  }
}

const LOCKING_RESOURCES = new Set(['task', 'task-test', 'task-review', 'integration', 'stage', 'plan']);

function artifactKind(handle: string): string | null {
  if (!handle.startsWith('art-')) return null;
  try {
    const json = Buffer.from(handle.slice(4).replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
    const v = JSON.parse(json) as unknown[];
    return typeof v[0] === 'string' ? v[0] : null;
  } catch {
    return null;
  }
}

/** Whether a GET takes the repository lock, by the contract's table. */
export function isLockingRead(path: string): boolean {
  const u = new URL(path, 'http://x');
  const parts = u.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  if (parts[1] === 'resources') return LOCKING_RESOURCES.has(parts[2] ?? '');
  if (parts[1] === 'diffs') return parts[2] === 'task' || parts[2] === 'integration';
  if (parts[1] === 'artifacts') { const k = artifactKind(parts[2] ?? ''); return k !== null && LOCKING_RESOURCES.has(k); }
  return false;
}

interface Event {
  at: number;
  kind: 'request' | 'job';
  method?: string;
  path?: string;
  jobId?: string;
  action?: string;
  state?: string;
  items?: Pick<Event, 'jobId' | 'action' | 'state'>[];
}

const UNFINISHED = new Set(['queued', 'running', 'cancelling']);

export class NetworkLog {
  readonly events: Event[] = [];
  private pending: Promise<void>[] = [];

  constructor(page: Page) {
    page.on('request', (r) => {
      const u = new URL(r.url());
      if (u.pathname.startsWith('/api/')) this.events.push({ at: Date.now(), kind: 'request', method: r.method(), path: u.pathname + u.search });
    });
    // Job states are placed in the log when the response arrives, before the page can act on
    // it; the body is filled in afterwards so the order stays the order the page saw.
    page.on('response', (res) => {
      const u = new URL(res.url());
      const req = res.request();
      const job = /^\/api\/jobs\/([^/]+)$/.exec(u.pathname);
      const isPost = req.method() === 'POST' && u.pathname === '/api/jobs' && res.status() === 202;
      const isPoll = req.method() === 'GET' && job !== null && res.status() === 200;
      const isList = req.method() === 'GET' && u.pathname === '/api/jobs' && res.status() === 200;
      if (!isPost && !isPoll && !isList) return;
      const event: Event = { at: Date.now(), kind: 'job' };
      this.events.push(event);
      this.pending.push((async () => {
        try {
          const body = await res.json();
          if (isPost) {
            const b = body as { job_id: string; state: string };
            Object.assign(event, { jobId: b.job_id, action: (req.postDataJSON() as { action: string }).action, state: b.state });
          } else if (isPoll) {
            const b = body as { job_id: string; action: string; state: string };
            Object.assign(event, { jobId: b.job_id, action: b.action, state: b.state });
          } else {
            const items = (body as { items: { job_id: string; action: string; state: string }[] }).items;
            event.items = items.map((b) => ({ jobId: b.job_id, action: b.action, state: b.state }));
          }
        } catch {
          /* the page navigated away before the body was read */
        }
      })());
    });
  }

  async ready(): Promise<void> {
    await Promise.all(this.pending);
  }

  private records(): Event[] {
    return this.events.flatMap((e) => e.items ? e.items.map((item) => ({ ...e, ...item })) : [e]);
  }

  /** Locking reads sent while the last known state of some CLI job was unfinished. */
  lockViolations(): string[] {
    const state = new Map<string, string>();
    const out: string[] = [];
    for (const e of this.records()) {
      if (e.kind === 'job' && e.jobId && e.action !== 'environment.refresh') state.set(e.jobId, e.state ?? '');
      if (e.kind === 'request' && e.method === 'GET' && e.path && isLockingRead(e.path)) {
        const busy = [...state.entries()].find(([, s]) => UNFINISHED.has(s));
        if (busy) out.push(`${e.path} while job ${busy[0]} was ${busy[1]}`);
      }
    }
    return out;
  }

  posts(): string[] {
    return this.events.filter((e) => e.kind === 'request' && e.method === 'POST' && e.path === '/api/jobs').map((e) => e.path!);
  }

  async record(name: string, extra: Record<string, unknown>): Promise<void> {
    await this.ready();
    mkdirSync(RECORDS, { recursive: true });
    const jobs = new Map<string, { action: string; states: string[] }>();
    for (const e of this.records()) {
      if (e.kind !== 'job' || !e.jobId) continue;
      const j = jobs.get(e.jobId) ?? { action: e.action ?? '', states: [] };
      if (j.states[j.states.length - 1] !== e.state) j.states.push(e.state ?? '');
      jobs.set(e.jobId, j);
    }
    writeFileSync(resolve(RECORDS, `${name}.json`), JSON.stringify({
      scenario: name,
      finished_at: new Date().toISOString(),
      api_requests: this.events.filter((e) => e.kind === 'request').length,
      locking_reads: this.events.filter((e) => e.kind === 'request' && e.path && isLockingRead(e.path)).length,
      lock_violations: this.lockViolations(),
      job_posts: this.posts().length,
      jobs: [...jobs.values()],
      ...extra,
    }, null, 2) + '\n');
  }
}

// ---- Page helpers ----

export async function pick(scope: Page | Locator, label: string, model: string, effort: string): Promise<void> {
  await scope.locator(`select[aria-label="${label}：模型"]`).first().selectOption(model);
  await scope.locator(`select[aria-label="${label}：档位"]`).first().selectOption(effort);
}

/** Wait until the page has read the state after the last job. */
export async function settled(page: Page): Promise<void> {
  await expect(page.locator('html[data-reading="0"]')).toHaveCount(1, { timeout: 60_000 });
}

export async function readback<T>(page: Page, url: string): Promise<T> {
  const response = await page.request.get(url);
  expect(response.ok(), url).toBe(true);
  return ((await response.json()) as { data: T }).data;
}

export function jobRequest(page: Page, action: string) {
  return page.waitForRequest((request) =>
    request.method() === 'POST' && new URL(request.url()).pathname === '/api/jobs'
    && request.postDataJSON()?.action === action);
}

/** Click an action button and wait for its job to end; returns the outcome badge text. */
export async function act(page: Page, name: string, scope: Page | Locator = page, timeout = 300_000): Promise<string> {
  const button = scope.getByRole('button', { name, exact: true }).first();
  await expect(button).toBeEnabled({ timeout: 60_000 });
  await button.click();
  const collapse = page.getByRole('button', { name: '收起' });
  await expect(collapse).toBeVisible({ timeout });
  const outcome = (await page.locator('.job-bar .badge').first().innerText()).trim();
  await collapse.click();
  await settled(page);
  return outcome;
}

/** Run both environment checks once, as a person would on first use. Codex candidates
 *  (Astra, Sol, Terra) only exist after a successful check. */
export async function refreshEnvironments(page: Page): Promise<void> {
  await expect(page.locator('.hint, select[aria-label$="：模型"]').first()).toBeVisible();
  for (const name of ['检查 Codex 环境', '检查 Claude 环境']) {
    const b = page.getByRole('button', { name });
    if (!(await b.count())) continue;
    await b.first().click();
    await expect(page.getByRole('button', { name })).toHaveCount(0, { timeout: 120_000 });
  }
  await expect(page.locator('.hint')).toHaveCount(0);
}
