// The inbox, job activity, finding discussions, setup check, goal draft and delivery summary,
// against the response shapes of the backend's web/ modules (attention.py, activity.py,
// review_findings.py, onboarding.py, delivery.py).

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { setTransport } from '../api/client';
import { jobs } from '../api/jobs';
import { ApiError, type Query, type Transport } from '../api/transport';
import type {
  Attention, AttentionItem, Delivery, FindingAnchor, IntegrationRecord, Job, JobActivity, Onboarding, ProjectPlan, ReviewFindings, ReviewRound, Workspace,
} from '../api/types';
import { jobEnding } from '../components/Badge';
import { setLangForTest } from '../i18n';
import { openIntegrationRevision } from '../pages/bits';
import { AttentionPanel, REASON_LABEL, useAttention } from '../pages/Attention';
import { EnvironmentHint, NewGoalPage } from '../pages/GoalPages';
import { draftKey, loadDraft, saveDraft } from '../pages/goalDraft';
import { DeliverySummary, ReworkBlock, ReworkOutcome } from '../pages/IntegrationExtras';
import { JobActivityView, useJobActivity } from '../pages/JobActivity';
import { OnboardingPanel, useOnboarding } from '../pages/Onboarding';
import { JobPage } from '../pages/JobPages';
import { NewTaskPlanPage } from '../pages/NewTaskPlanPage';
import { PlanPage } from '../pages/PlanPage';
import { FindingsPanel, useFindings } from '../pages/ReviewFindings';
import { delegateStage } from '../fixtures/specimen';
import { StagePage } from '../pages/StagePage';
import { fakeTransport } from './fake';

const workspace: Workspace = {
  repository: '/repo', target_branch: 'main', head: 'a'.repeat(40), read_only: false,
  branches: [{ name: 'main', head: 'a'.repeat(40) }, { name: 'old', head: 'b'.repeat(40) }],
  environments: { claude: null, codex: null },
  presets: { source: 'test', rules: {}, roles: [] },
};
const finished = (id: string, action: string, extra: Partial<Job> = {}): Job =>
  ({ job_id: id, action, state: 'finished', exit_code: 0, result: {}, error: null, ...extra });

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

/** Lets a test hold the answers to one path: the value is taken when the request is made
 *  and handed over only on release, like a slow response that arrives after newer events. */
function gated(fake: ReturnType<typeof fakeTransport>, path: string) {
  const waiting: (() => void)[] = [];
  const gate = { hold: false, held: 0 };
  const transport: Transport = {
    post: (p, body) => fake.post(p, body),
    async get<T>(p: string, query?: Query): Promise<T> {
      const value = await fake.get<T>(p, query);
      if (p === path && gate.hold) {
        gate.held += 1;
        await new Promise<void>((release) => waiting.push(release));
      }
      return value;
    },
  };
  return { transport, gate, release: () => { gate.hold = false; waiting.splice(0).forEach((go) => go()); } };
}

// ---- 1 首页待办 ----------------------------------------------------------------------

const stage = (id: string): AttentionItem => ({
  kind: 'project-plan', id: 'plan-1', label: `Stage ${id}`, reason: 'choose_stage', detail: `Implement ${id}`, href: `/plans/plan-1?stage=${id}`,
});
const accept: AttentionItem = {
  kind: 'integration', id: 'i1', label: 'Summary report', reason: 'accept_integration',
  detail: 'Tests and review qualify this candidate for acceptance.', href: '/integrations/i1',
};

function Inbox() {
  return <AttentionPanel attention={useAttention()} />;
}

describe('首页待办', () => {
  it('lists two claimable stages of one plan as separate rows, each with the link the service gave', async () => {
    setTransport(fakeTransport({
      '/jobs': { items: [] },
      '/attention': { items: [stage('ingest'), stage('classify'), { ...accept, reason: 'future_reason', detail: 'A reason this page has not seen.' }], complete: true } satisfies Attention,
    }));
    await jobs.init();
    render(<Inbox />);
    expect((await screen.findByRole('link', { name: 'Stage ingest' })).getAttribute('href')).toBe('/plans/plan-1?stage=ingest');
    expect(screen.getByRole('link', { name: 'Stage classify' }).getAttribute('href')).toBe('/plans/plan-1?stage=classify');
    expect(screen.getAllByText('可以领取这个阶段')).toHaveLength(2);
    // An unknown reason falls back to the service's own sentence.
    expect(screen.getByText('A reason this page has not seen.')).toBeTruthy();
    // A stage's own goal is shown under its row.
    expect(screen.getByText('Implement ingest')).toBeTruthy();
    expect(screen.getByText('共 3 项')).toBeTruthy();
  });

  it('keeps known rows as pending while a job gives a partial list, and drops the accepted one afterwards', async () => {
    let answer: Attention = { items: [stage('report'), accept], complete: true };
    let job: Job = { ...finished('j1', 'integration.accept'), state: 'running', exit_code: null, result: null };
    const fake = fakeTransport(
      { '/jobs': { items: [] }, '/attention': () => answer, '/jobs/j1': () => job },
      { '/jobs': { job_id: 'j1', state: 'queued' } },
    );
    setTransport(fake);
    await jobs.init();
    render(<Inbox />);
    await screen.findByRole('link', { name: 'Summary report' });
    expect(screen.queryByText('待刷新')).toBeNull();

    // The job holds the worktree: only database-confirmed items come back (here: none).
    answer = { items: [], complete: false };
    await act(async () => { await jobs.submit('integration.accept', { integration_id: 'i1', round_id: 'r1' }); });
    await waitFor(() => expect(screen.getAllByText('待刷新')).toHaveLength(2));
    expect(screen.getByText(/这次只读到一部分待办/)).toBeTruthy();
    // Kept rows say when they were read and promise nothing about what can be done now.
    expect(screen.getByText(/现在还要不要处理、能不能操作，要等重新读取/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Summary report' })).toBeTruthy();
    expect(screen.queryByText('现在没有待办。')).toBeNull();
    // The fixed sentence that only repeats a known reason is left out.
    expect(screen.queryByText('Tests and review qualify this candidate for acceptance.')).toBeNull();

    // Accepted: the list is read again when the job ends and the candidate leaves it.
    answer = { items: [stage('report')], complete: true };
    job = finished('j1', 'integration.accept');
    await waitFor(() => expect(screen.queryByRole('link', { name: 'Summary report' })).toBeNull(), { timeout: 4000 });
    expect(screen.getByRole('link', { name: 'Stage report' })).toBeTruthy();
    expect(screen.queryByText('待刷新')).toBeNull();
  });

  it('tells "everything handled" apart from "only part of the state could be read"', async () => {
    let answer: Attention = { items: [], complete: true };
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/attention': () => answer }));
    await jobs.init();
    const view = render(<Inbox />);
    expect(await screen.findByText('现在没有待办。')).toBeTruthy();
    view.unmount();

    answer = { items: [], complete: false };
    const running: Job = { ...finished('j2', 'task.run'), state: 'running', exit_code: null };
    jobs.reset();
    setTransport(fakeTransport({ '/jobs': { items: [running] }, '/jobs/j2': running, '/attention': () => answer }));
    await jobs.init();
    render(<Inbox />);
    expect(await screen.findByText('这次没有读到待办，但读取不完整，不代表都处理完了。稍后会重新读取。')).toBeTruthy();
    expect(screen.queryByText('现在没有待办。')).toBeNull();
  });

  // Every reason src/multi_agent/web/attention.py can return.
  const REASONS = [
    'confirm_goal', 'consultation_failed', 'plan_goal', 'retry_project_plan', 'choose_stage', 'dispatch_failed',
    'planning_attention', 'planning_question', 'implementation_attention', 'task_test_failed',
    'integration_conflict', 'rebuild_integration', 'inspect_candidate', 'test_integration', 'integration_test_failed',
    'inspect_tests', 'review_integration', 'review_incomplete', 'review_decision', 'accept_integration',
    // The three steps of a direct stage between its claim and its integration.
    'test_task', 'review_task', 'integrate_stage',
  ];

  it('has its own wording for every reason the service can give, in both languages', async () => {
    // Vitest runs from frontend/; the backend module sits next to it in the repository.
    const backend = resolve(process.cwd(), '../src/multi_agent/web/attention.py');
    if (existsSync(backend)) {
      const source = readFileSync(backend, 'utf8');
      // The list above is the backend's: each reason is a literal in the module that builds the inbox.
      expect(REASONS.filter((reason) => !source.includes(`"${reason}"`))).toEqual([]);
    }
    const items = REASONS.map((reason, i): AttentionItem => ({ kind: 'task', id: `t${i}`, label: `Record ${i}`, reason, detail: `service sentence ${i}`, href: `/tasks/t${i}` }));
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/attention': { items, complete: true } }));
    await jobs.init();
    const zh = render(<Inbox />);
    await screen.findByRole('link', { name: 'Record 0' });
    const shown = [...document.querySelectorAll('.attention-reason')].map((node) => node.textContent ?? '');
    expect(shown).toHaveLength(REASONS.length);
    expect(new Set(shown).size).toBe(REASONS.length);
    // None falls back to the service's sentence, and none shows the raw key.
    expect(shown.filter((text) => /service sentence|^[a-z_]+$/.test(text))).toEqual([]);
    expect(shown.every((text) => /[一-鿿]/.test(text))).toBe(true);
    zh.unmount();

    setLangForTest('en');
    expect(REASONS.filter((reason) => /[一-鿿]/.test(REASON_LABEL[reason] ?? '一'))).toEqual([]);
    render(<Inbox />);
    await screen.findByRole('link', { name: 'Record 0' });
    const english = [...document.querySelectorAll('.attention-reason')].map((node) => node.textContent ?? '');
    expect(english.filter((text) => /[一-鿿：；，、（）。「」]|service sentence|^[a-z_]+$/.test(text))).toEqual([]);
  });

  it('marks the stage a plan link points at', async () => {
    const plan = {
      kind: 'project_plan', plan_id: 'plan-1', status: 'succeeded', planner_request: null, started_at: null, finished_at: null, evidence: null, error: null,
      goal: { kind: 'confirmed_goal', project_id: 'proj', goal_id: 'g', baseline_commit: 'a', original_goal: 'x', confirmed_goal: 'x', confirmed_at: 't' },
      document: { title: 'Plan', stages: [
        { stage_id: 'ingest', title: 'Stage ingest', goal: 'Read input', depends_on: [] },
        { stage_id: 'classify', title: 'Stage classify', goal: 'Sort input', depends_on: [] },
      ] },
    } satisfies ProjectPlan;
    setTransport(fakeTransport({
      '/jobs': { items: [] }, '/workspace': workspace,
      '/resources/project-plan/plan-1': { kind: 'project-plan', id: 'plan-1', data: plan, artifacts: [] },
      '/resources/project-stages/plan-1': { kind: 'project-stages', id: 'plan-1', data: { plan_id: 'plan-1', stages: [] }, artifacts: [] },
      '/resources/project/proj': { kind: 'project', id: 'proj', data: { kind: 'project', project_id: 'proj', goals: [], plans: [] }, artifacts: [] },
    }));
    await jobs.init();
    render(<PlanPage id="plan-1" stage="classify" />);
    const row = (await screen.findByText('Stage classify')).closest('li')!;
    expect(row.getAttribute('aria-current')).toBe('location');
    expect(screen.getByText('Stage ingest').closest('li')!.getAttribute('aria-current')).toBeNull();
    expect(within(row).getByRole('button', { name: '领取这个阶段' })).toBeTruthy();
  });
});

it('marks the delegated card a stage link points at', async () => {
  setTransport(fakeTransport({
    '/jobs': { items: [] }, '/workspace': workspace,
    '/resources/stage/sr-review-pages-01': { kind: 'stage', id: 'sr-review-pages-01', data: delegateStage, artifacts: [] },
  }));
  await jobs.init();
  render(<StagePage id="sr-review-pages-01" card="diff-viewer" />);
  await waitFor(() => expect(document.querySelectorAll('.leaf')).toHaveLength(2));
  const located = document.querySelectorAll('.leaf[aria-current="location"]');
  expect(located).toHaveLength(1);
  expect(within(located[0] as HTMLElement).getByText('diff-viewer')).toBeTruthy();
});

// ---- 2 作业进展和恢复 -----------------------------------------------------------------

const event = (sequence: number, name: string, seat = 'reviewer-2', state: string | null = null) => ({
  sequence, at: '2026-11-25T10:00:00+00:00', event: name, state, resource: { kind: 'review', id: 'r1' }, seat, provider: 'claude', attempt: 1,
});
const activity = (extra: Partial<JobActivity>): JobActivity => ({
  job_id: 'job-a', state: 'finished', observed_at: '2026-11-25T10:00:05+00:00', controller_holds_journal: false, history_truncated: false,
  events: [], actors: [], reviews: [], resources: [], recovery: [], ...extra,
});

describe('作业进展和恢复', () => {
  it('shows each event once across repeated polls and offers only the missing seat after the job ended', async () => {
    const retry = { round_id: 'r1', seat_keys: ['reviewer-2'], expected_attempts: { 'reviewer-2': 1 } };
    let job: Job = { ...finished('job-a', 'review.run'), state: 'running', exit_code: null, result: null };
    let current = activity({ state: 'running', controller_holds_journal: true, events: [event(10, 'attempt_started'), event(20, 'message')] });
    const post = vi.fn(() => ({ job_id: 'job-b', state: 'queued' }));
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': () => ({ items: [job] }), '/jobs/job-a': () => job, '/jobs/job-a/activity': () => current,
      '/jobs/job-b': { ...finished('job-b', 'review.retry'), exit_code: 2, result: null, error: { code: 'cli_refused', message: 'Seat attempts have changed.' }, error_kind: 'refused' },
    }, { '/jobs': post }));
    await jobs.init();
    render(<JobPage id="job-a" />);
    const list = await screen.findByRole('list', { name: '最近的事件' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);

    // The next poll repeats sequence 20 and adds 30: three rows, none twice.
    current = activity({ state: 'running', controller_holds_journal: true, events: [event(20, 'message'), event(30, 'provider_wait')] });
    await waitFor(() => expect(within(screen.getByRole('list', { name: '最近的事件' })).getAllByRole('listitem')).toHaveLength(3), { timeout: 4000 });
    expect(within(screen.getByRole('list', { name: '最近的事件' })).getAllByText('模型有输出')).toHaveLength(1);
    expect(screen.queryByRole('heading', { name: '可以继续的动作' })).toBeNull();

    // The job ends: the valid report stays, and recovery names only the missing seat.
    job = { ...finished('job-a', 'review.run'), exit_code: 1 };
    current = activity({
      events: [event(20, 'message'), event(30, 'provider_wait'), event(40, 'attempt_settled')],
      actors: [{ resource: { kind: 'review', id: 'r1' }, seat: 'reviewer-2', provider: 'claude', attempt: 1, state: 'failed', last_observed_state: 'failed', observed_at: '2026-11-25T10:00:04+00:00', last_event: 'attempt_settled' }],
      reviews: [{ round_id: 'r1', snapshot_id: 's1', retry: { action: 'review.retry', args: retry }, seats: [
        { seat: 'reviewer-1', provider: 'codex', attempt: 1, state: 'succeeded', retryable: false },
        { seat: 'reviewer-2', provider: 'claude', attempt: 1, state: 'failed', retryable: true },
      ] }],
      resources: [{ kind: 'review', id: 'r1' }],
      recovery: [{ resource: { kind: 'review', id: 'r1' }, action: 'review.retry', args: retry }],
    });
    const button = await screen.findByRole('button', { name: '只补跑缺失的席位（reviewer-2）' }, { timeout: 4000 });
    expect(screen.getByText('报告有效')).toBeTruthy();
    expect(screen.getAllByText('可以补跑')).toHaveLength(1);
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    // The request is the action and arguments exactly as the service returned them.
    await waitFor(() => expect(post).toHaveBeenCalledWith({ action: 'review.retry', args: retry }));
    // Accepted with 202, then refused while running: the job error is shown here.
    const refusal = await screen.findByText(/被服务拒绝：Seat attempts have changed\./, undefined, { timeout: 4000 });
    // Said apart from a failure, with the list read again.
    expect(refusal.textContent).toContain('已经过期');
  });

  it('reads the activity again when the job ended while a slower read was still on its way', async () => {
    const retry = { round_id: 'r1', seat_keys: ['reviewer-2'], expected_attempts: { 'reviewer-2': 1 } };
    let job: Job = { ...finished('job-a', 'review.run'), state: 'running', exit_code: null, result: null };
    let current = activity({ state: 'running', controller_holds_journal: true });
    const slow = gated(fakeTransport({ '/workspace': workspace, '/jobs': () => ({ items: [job] }), '/jobs/job-a': () => job, '/jobs/job-a/activity': () => current }), '/jobs/job-a/activity');
    setTransport(slow.transport);
    await jobs.init();
    render(<JobPage id="job-a" />);
    await screen.findByRole('heading', { name: '最近的事件' });

    // A read made while the job still runs is slow; the job ends before it arrives.
    slow.gate.hold = true;
    await waitFor(() => expect(slow.gate.held).toBeGreaterThan(0), { timeout: 4000 });
    job = { ...finished('job-a', 'review.run'), exit_code: 1 };
    current = activity({ recovery: [{ resource: { kind: 'review', id: 'r1' }, action: 'review.retry', args: retry }] });
    await screen.findByText('结果不满足', undefined, { timeout: 4000 });
    slow.release();
    // The late answer still says "running"; the page must ask once more and offer the recovery.
    expect(await screen.findByRole('button', { name: '只补跑缺失的席位（reviewer-2）' }, { timeout: 4000 })).toBeTruthy();
  }, 15_000);

  it('does not offer an old recovery again while the activity is being read after the retry', async () => {
    const retry = { round_id: 'r1', seat_keys: ['reviewer-2'], expected_attempts: { 'reviewer-2': 1 } };
    let current = activity({ recovery: [{ resource: { kind: 'review', id: 'r1' }, action: 'review.retry', args: retry }] });
    let retried: Job = { ...finished('job-b', 'review.retry'), state: 'running', exit_code: null };
    const post = vi.fn(() => ({ job_id: 'job-b', state: 'queued' }));
    const slow = gated(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-a': { ...finished('job-a', 'review.run'), exit_code: 1 },
      '/jobs/job-a/activity': () => current, '/jobs/job-b': () => retried,
    }, { '/jobs': post }), '/jobs/job-a/activity');
    setTransport(slow.transport);
    await jobs.init();
    render(<JobPage id="job-a" />);
    const button = await screen.findByRole('button', { name: '只补跑缺失的席位（reviewer-2）' }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    slow.gate.hold = true;
    fireEvent.click(button);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    // The retry ends and the seat has a newer attempt, but the fresh activity is slow to arrive.
    current = activity({});
    retried = finished('job-b', 'review.retry');
    await waitFor(() => expect(slow.gate.held).toBeGreaterThan(0), { timeout: 4000 });
    await new Promise((r) => setTimeout(r, 700));
    const stale = screen.queryByRole('button', { name: '只补跑缺失的席位（reviewer-2）' }) as HTMLButtonElement | null;
    expect(stale === null || stale.disabled).toBe(true);
    slow.release();
    await waitFor(() => expect(screen.queryByRole('button', { name: '只补跑缺失的席位（reviewer-2）' })).toBeNull(), { timeout: 4000 });
    expect(post).toHaveBeenCalledTimes(1);
  }, 15_000);

  it('has words for every state the service journals and survives entries without their inner keys', async () => {
    // AttemptStatus, the advisor seat states and TaskTestPhase (core/state.py, core/task_test_state.py).
    const states = ['pending', 'ready', 'running', 'cancelling', 'awaiting_evidence', 'succeeded', 'failed', 'cancelled'];
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-s': finished('job-s', 'task.test'),
      '/jobs/job-s/activity': {
        ...activity({ job_id: 'job-s', events: states.map((state, i) => event(i + 1, 'suite_settled', 'test-1', state)) }),
        // A review without seats and a recovery without arguments, as an older service might send.
        reviews: [{ round_id: 'r1', snapshot_id: 's1', retry: null }],
        recovery: [{ resource: { kind: 'review', id: 'r1' }, action: 'review.retry' }],
      },
    }));
    await jobs.init();
    render(<JobPage id="job-s" />);
    const list = await screen.findByRole('list', { name: '最近的事件' });
    const shown = [...list.querySelectorAll('li')].map((item) => item.lastElementChild?.textContent ?? '');
    expect(shown).toHaveLength(states.length);
    expect(shown.filter((text) => /^[a-z_]+$/.test(text))).toEqual([]);
    expect(shown).toContain('等待证据');
    // The list scrolls inside its own box, so it can take the keyboard focus.
    expect(list.getAttribute('tabindex')).toBe('0');
    expect(screen.getByRole('button', { name: '补跑独立审核' })).toBeTruthy();
  });

  it('keeps the way into the records of an old interrupted job that cannot name its seat attempts', async () => {
    const job: Job = { job_id: 'job-old', action: 'review.retry', state: 'interrupted', exit_code: null, result: null, error: null, args: { round_id: 'r1', seat_keys: ['reviewer-2'] } };
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-old': job,
      '/jobs/job-old/activity': activity({
        job_id: 'job-old', state: 'interrupted', controller_holds_journal: null,
        actors: [{ resource: { kind: 'review', id: 'r1' }, seat: 'reviewer-2', provider: 'claude', attempt: 2, state: 'unconfirmed', last_observed_state: 'running', observed_at: '2026-11-25T10:00:04+00:00', last_event: 'message' }],
        reviews: [{ round_id: 'r1', snapshot_id: 's1', retry: null, seats: [
          { seat: 'reviewer-1', provider: 'codex', attempt: 1, state: 'failed', retryable: false },
          { seat: 'reviewer-2', provider: 'claude', attempt: 2, state: 'failed', retryable: false },
        ] }],
        resources: [{ kind: 'review', id: 'r1' }],
      }),
    }));
    await jobs.init();
    render(<JobPage id="job-old" />);
    expect(await screen.findByText(/这个作业没有可以直接继续的动作/)).toBeTruthy();
    expect(screen.getByText(/中断前最后观测于/)).toBeTruthy();
    expect(screen.getByText(/这个作业没有留下活动记录/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /补跑/ })).toBeNull();
    const records = within(screen.getByRole('heading', { name: '相关记录' }).closest('section')!);
    expect(records.getByRole('link', { name: /审核轮/ }).getAttribute('href')).toBe('/reviews/r1');
  });
});

// ---- 3 审查意见 ----------------------------------------------------------------------

const anchor = (seat: string): FindingAnchor => ({ round_id: 'r1', snapshot_id: 's1', seat, attempt: 1, finding_id: 'status-blocker' });
const finding = { id: 'status-blocker', severity: 'P1', category: 'correctness', side: 'candidate', path: 'frontend/status.js', line_start: 1, line_end: 1,
  quote: 'draft', title: 'Status is still draft', impact: 'The summary shows the draft state.', rationale: 'The frozen line returns draft.', reference_status: 'verified' } as const;
const findingsOf = (second: ReviewFindings['groups'][0]['members'][0]['activity'] = []): ReviewFindings => ({
  round_id: 'r1', snapshot_id: 's1', status: 'needs_rework', reasons: ['blocking_findings_present'],
  source: { kind: 'repository', baseline: 'b'.repeat(40), candidate: 'c'.repeat(40), correction_baseline: 'c'.repeat(40) },
  groups: [{
    location: { side: 'candidate', path: 'frontend/status.js', line_start: 1, line_end: 1 }, blocking: true,
    independent_reports: { 'reviewer-1': 'reported', 'reviewer-2': 'reported' },
    confirmations: [{ seat: 'reviewer-1', target_seat: 'reviewer-2', finding_id: 'status-blocker', decision: 'uncertain', reason: 'Intent unclear.' }],
    // Both reviewers chose the same finding id.
    members: [
      { seat: 'reviewer-1', finding, anchor: anchor('reviewer-1'), activity: [] },
      { seat: 'reviewer-2', finding, anchor: anchor('reviewer-2'), activity: second },
    ],
  }],
});
const round = { round_id: 'r1', snapshot_id: 's1', status: 'ready_for_assessment', seats: [], assessment: null, metadata: { kind: 'review', created_at: 't' } } satisfies ReviewRound;

function Findings() {
  return <FindingsPanel round={round} findings={useFindings('r1')} fileHref={() => '/snapshots/s1/file'} />;
}

describe('审查意见', () => {
  it('binds the decision, the follow-up and the correction to the chosen seat when both seats share a finding id', async () => {
    let current = findingsOf();
    const decide = vi.fn((body: unknown) => {
      current = findingsOf([{ id: 1, kind: 'decision', created_at: '2026-11-25T10:00:00+00:00', decision: 'disputed', reason: 'Excluded upstream.' }]);
      return { id: 1, kind: 'decision', ...(body as object) };
    });
    const submit = vi.fn(() => ({ job_id: 'job-q', state: 'queued' }));
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/reviews/r1/findings': () => current,
      '/jobs/job-q': finished('job-q', 'review.followup'),
    }, { '/reviews/r1/decisions': decide, '/jobs': submit }));
    await jobs.init();
    render(<Findings />);
    await screen.findAllByText('Status is still draft');
    const cards = document.querySelectorAll<HTMLElement>('.finding-card');
    expect([...cards].map((card) => card.dataset.seat)).toEqual(['reviewer-1', 'reviewer-2']);
    const second = within(cards[1]!);
    // The cross-confirmation aimed at reviewer-2 sits on that card only.
    expect(second.getByText('Intent unclear.')).toBeTruthy();
    expect(within(cards[0]!).queryByText('Intent unclear.')).toBeNull();

    fireEvent.click(second.getByRole('radio', { name: '存疑' }));
    fireEvent.change(second.getByLabelText('判断的理由'), { target: { value: 'Excluded upstream.' } });
    fireEvent.click(second.getByRole('button', { name: '记录判断' }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith({ anchor: anchor('reviewer-2'), decision: 'disputed', reason: 'Excluded upstream.' }));
    // After the re-read the record hangs under reviewer-2; reviewer-1 stays untouched.
    await waitFor(() => expect(within(document.querySelectorAll<HTMLElement>('.finding-card')[1]!).getByText('Excluded upstream.')).toBeTruthy());
    expect(within(document.querySelectorAll<HTMLElement>('.finding-card')[0]!).queryByText('Excluded upstream.')).toBeNull();

    const card = within(document.querySelectorAll<HTMLElement>('.finding-card')[1]!);
    fireEvent.change(card.getByLabelText('要问的问题'), { target: { value: 'Which input triggers this?' } });
    const ask = card.getByRole('button', { name: '提交追问' });
    await waitFor(() => expect((ask as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(ask);
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ action: 'review.followup', args: { review_anchor: anchor('reviewer-2'), question: 'Which input triggers this?' } }));
    expect(card.getByRole('link', { name: /^发起关联修复/ }).getAttribute('href'))
      .toBe('/task-plans/new?review=r1&seat=reviewer-2&attempt=1&finding=status-blocker');
    // Each card has the same three controls; their accessible names say which finding they belong to.
    const names = screen.getAllByRole('link', { name: /^发起关联修复/ }).map((link) => link.getAttribute('aria-label'));
    expect(names).toEqual(['发起关联修复：Status is still draft（reviewer-1）', '发起关联修复：Status is still draft（reviewer-2）']);
    const summaries = [...document.querySelectorAll('.finding-form > summary')].map((node) => node.getAttribute('aria-label'));
    expect(new Set(summaries).size).toBe(4);
    expect(summaries[0]).toBe('记录你的判断：Status is still draft（reviewer-1）');
  });

  it('announces why a decision was not recorded', async () => {
    setTransport(fakeTransport({ '/workspace': workspace, '/jobs': { items: [] }, '/reviews/r1/findings': findingsOf() }, {
      '/reviews/r1/decisions': new ApiError(400, 'invalid_request', 'finding anchor requires the current completed seat attempt'),
    }));
    await jobs.init();
    render(<Findings />);
    await screen.findAllByText('Status is still draft');
    const card = within(document.querySelectorAll<HTMLElement>('.finding-card')[0]!);
    fireEvent.change(card.getByLabelText('判断的理由'), { target: { value: 'A reason.' } });
    fireEvent.click(card.getByRole('button', { name: '记录判断' }));
    const alert = await card.findByRole('alert');
    expect(alert.textContent).toContain('finding anchor requires the current completed seat attempt');
    // The reason stays in the box for another try.
    expect((card.getByLabelText('判断的理由') as HTMLTextAreaElement).value).toBe('A reason.');
  });

  it('keeps a question typed while the previous follow-up was still running', async () => {
    let asked: Job = { ...finished('job-q', 'review.followup'), state: 'running', exit_code: null };
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/reviews/r1/findings': findingsOf(), '/jobs/job-q': () => asked,
    }, { '/jobs': { job_id: 'job-q', state: 'queued' } }));
    await jobs.init();
    render(<Findings />);
    await screen.findAllByText('Status is still draft');
    const card = within(document.querySelectorAll<HTMLElement>('.finding-card')[0]!);
    const box = card.getByLabelText('要问的问题') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'First question?' } });
    const ask = card.getByRole('button', { name: '提交追问' }) as HTMLButtonElement;
    await waitFor(() => expect(ask.disabled).toBe(false));
    fireEvent.click(ask);
    await screen.findAllByText('已提交，等作业结束');
    fireEvent.change(box, { target: { value: 'A second question, written meanwhile.' } });
    asked = finished('job-q', 'review.followup');
    await waitFor(() => expect(screen.queryByText('已提交，等作业结束')).toBeNull(), { timeout: 4000 });
    await new Promise((r) => setTimeout(r, 50));
    expect(box.value).toBe('A second question, written meanwhile.');
  });

  it('starts a linked correction from the reviewed commit and sends the anchor of the chosen seat', async () => {
    const submit = vi.fn(() => ({ job_id: 'job-p', state: 'queued' }));
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/reviews/r1/findings': findingsOf(),
      '/jobs/job-p': finished('job-p', 'plan.create'),
    }, { '/jobs': submit }));
    await jobs.init();
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    render(<NewTaskPlanPage from={null} questionIndex={null} review={{ round: 'r1', seat: 'reviewer-2', attempt: 1, finding: 'status-blocker' }} />);
    const baseline = await screen.findByLabelText('基线提交') as HTMLInputElement;
    expect(baseline.value).toBe('c'.repeat(40));
    expect(baseline.readOnly).toBe(true);
    const requirements = (screen.getByLabelText('单卡需求') as HTMLTextAreaElement).value;
    expect(requirements).toContain('Status is still draft');
    expect(requirements).toContain('frontend/status.js:1（候选一侧）');
    const start = screen.getByRole('button', { name: '开始单卡规划' });
    await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(start);
    await waitFor(() => expect(submit).toHaveBeenCalled());
    const body = (submit.mock.calls[0] as unknown[])[0] as { action: string; args: { baseline: string; review_anchor: FindingAnchor } };
    expect(body.action).toBe('plan.create');
    expect(body.args.baseline).toBe('c'.repeat(40));
    expect(body.args.review_anchor).toEqual(anchor('reviewer-2'));
  });

  it('asks for an explicit baseline when the reviewed input was a pasted patch, and refuses a stale anchor', async () => {
    const pasted: ReviewFindings = { ...findingsOf(), source: { kind: 'pasted_diff', baseline: null, candidate: null, correction_baseline: null } };
    const submit = vi.fn(() => ({ job_id: 'job-p', state: 'queued' }));
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/reviews/r1/findings': pasted, '/jobs/job-p': finished('job-p', 'plan.create'),
    }, { '/jobs': submit }));
    await jobs.init();
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    const view = render(<NewTaskPlanPage from={null} questionIndex={null} review={{ round: 'r1', seat: 'reviewer-1', attempt: 1, finding: 'status-blocker' }} />);
    const baseline = await screen.findByLabelText('基线提交') as HTMLInputElement;
    expect(baseline.value).toBe('');
    expect(baseline.readOnly).toBe(false);
    const start = screen.getByRole('button', { name: '开始单卡规划' }) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    expect(screen.getByText('粘贴的补丁没有候选提交，请明确填写这个仓库里的基线。')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('或选分支'), { target: { value: 'b'.repeat(40) } });
    await waitFor(() => expect(start.disabled).toBe(false));
    fireEvent.click(start);
    await waitFor(() => expect(submit).toHaveBeenCalled());
    const body = (submit.mock.calls[0] as unknown[])[0] as { args: { baseline: string; review_anchor: FindingAnchor } };
    expect(body.args.baseline).toBe('b'.repeat(40));
    expect(body.args.review_anchor).toEqual(anchor('reviewer-1'));
    view.unmount();

    // The seat was retried since the link was made: its finding now belongs to another attempt.
    render(<NewTaskPlanPage from={null} questionIndex={null} review={{ round: 'r1', seat: 'reviewer-1', attempt: 2, finding: 'status-blocker' }} />);
    expect(await screen.findByText('找不到这条意见')).toBeTruthy();
    expect(screen.getByRole('link', { name: '返回审核轮' }).getAttribute('href')).toBe('/reviews/r1');
  });

  it('shows who owns the blockers and the refusal reason, and enables rework only when eligible', async () => {
    const integration = { integration_id: 'i1', rework: null } as unknown as IntegrationRecord;
    let eligibility: unknown = {
      integration_id: 'i1', round_id: 'r1', lock_busy: false, eligible: false, owner: null,
      reason: 'Blocking findings belong to different members.',
      findings: [
        { seat: 'seat-1', finding_id: 'status-blocker', path: 'frontend/status.js', reference_status: 'verified', owners: [{ task_id: 'T-0800', task_run_id: 'task-a' }] },
        { seat: 'seat-2', finding_id: 'status-blocker', path: 'frontend/other.js', reference_status: 'verified', owners: [{ task_id: 'T-0801', task_run_id: 'task-b' }] },
      ],
    };
    const submit = vi.fn(() => ({ job_id: 'job-r', state: 'queued' }));
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/integrations/i1/rework-eligibility': () => eligibility,
      '/jobs/job-r': finished('job-r', 'integration.rework', { exit_code: 1, result: { rework: { status: 'user_decision', reason: 'The review directory moved.' } } }),
    }, { '/jobs': submit }));
    await jobs.init();
    const view = render(<ReworkBlock it={integration} enabled />);
    expect(await screen.findByText('Blocking findings belong to different members.')).toBeTruthy();
    expect(screen.getByRole('group', { name: '返工资格' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'T-0800' }).getAttribute('href')).toBe('/tasks/task-a');
    expect(screen.getByRole('link', { name: 'T-0801' }).getAttribute('href')).toBe('/tasks/task-b');
    expect((screen.getByRole('button', { name: '按阻塞意见返工（每条谱系一次）' }) as HTMLButtonElement).disabled).toBe(true);
    view.unmount();

    eligibility = { integration_id: 'i1', round_id: 'r2', lock_busy: false, eligible: true, reason: null, owner: { task_id: 'T-0800', task_run_id: 'task-a' },
      findings: [{ seat: 'seat-1', finding_id: 'status-blocker', path: 'frontend/status.js', reference_status: 'verified', owners: [{ task_id: 'T-0800', task_run_id: 'task-a' }] }] };
    render(<ReworkBlock it={{ ...integration, integration_id: 'i1' }} enabled />);
    const button = await screen.findByRole('button', { name: '按阻塞意见返工（每条谱系一次）' });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ action: 'integration.rework', args: { integration_id: 'i1', round_id: 'r2' } }));
    // What the command itself decided is shown when no new integration came out of it.
    expect(await screen.findByText(/返工没有产生新的集成：The review directory moved\./, undefined, { timeout: 4000 })).toBeTruthy();
  });

  it('opens the integration the rework produced, as named in the receipt', async () => {
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] },
      '/integrations/i1/rework-eligibility': { integration_id: 'i1', round_id: 'r1', lock_busy: false, eligible: true, reason: null, owner: { task_id: 'T-0800', task_run_id: 'task-a' }, findings: [] },
      // Other integrations may be listed with the job; the receipt names the child.
      '/jobs/job-r': finished('job-r', 'integration.rework', {
        result: { rework: { rework_id: 'w1', source_id: 'i1', child_id: 'i-child' }, revision: { integration_id: 'i-child' } },
        resources: [{ kind: 'integration', id: 'i-ancestor' }, { kind: 'integration', id: 'i1' }, { kind: 'integration', id: 'i-child' }],
      }),
    }, { '/jobs': { job_id: 'job-r', state: 'queued' } }));
    await jobs.init();
    window.history.replaceState(null, '', '/integrations/i1');
    render(<ReworkBlock it={{ integration_id: 'i1', rework: null } as unknown as IntegrationRecord} enabled />);
    const button = await screen.findByRole('button', { name: '按阻塞意见返工（每条谱系一次）' }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(window.location.pathname).toBe('/integrations/i-child'), { timeout: 4000 });
  });

  it('waits for the job instead of showing eligibility that was read under a held lock', async () => {
    const fake = fakeTransport({
      '/jobs': { items: [] }, '/integrations/i1/rework-eligibility': { integration_id: 'i1', eligible: false, lock_busy: true },
    });
    setTransport(fake);
    await jobs.init();
    render(<ReworkBlock it={{ integration_id: 'i1', rework: null } as unknown as IntegrationRecord} enabled />);
    // No job of this page runs: the page says the service was busy and offers another reading.
    expect(await screen.findByText('服务正忙，这次没有读到返工资格。')).toBeTruthy();
    expect(screen.queryByText(/作业运行中/)).toBeNull();
    expect(screen.getByRole('button', { name: '重新读取' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '按阻塞意见返工（每条谱系一次）' })).toBeNull();
  });
});

// ---- 4 首次接入和草稿 ------------------------------------------------------------------

const TEMPLATE = 'version: 1\ncommands:\n  unit:\n    executable: python3\n';
const unknownProvider = { status: 'unknown', cli_version: null, observed_at: null, check_job_id: null, error: null };
const onboardingOf = (extra: Partial<Onboarding>): Onboarding => ({
  repository: '/repo', read_only: false, branch: 'main', head: 'a'.repeat(40), branches: workspace.branches!, baseline: 'a'.repeat(40),
  rules: { path: '.multi-agent/rules.yaml', status: 'missing', detail: null, commands: [] }, rules_template: TEMPLATE,
  providers: { codex: unknownProvider, claude: { ...unknownProvider, status: 'login_required', cli_version: '2.1.325' } },
  next_steps: [{ kind: 'add_rules' }, { kind: 'check_provider', provider: 'codex' }, { kind: 'sign_in', provider: 'claude' }, { kind: 'create_goal' }],
  ...extra,
});

function goalFake(onboarding: (query: unknown) => unknown, posts: Record<string, unknown> = {}, gets: Record<string, unknown> = {}) {
  return fakeTransport({ '/workspace': workspace, '/jobs': { items: [] }, '/runs': { items: [] }, '/workspace/onboarding': onboarding, ...gets }, posts);
}

describe('首次接入和目标草稿', () => {
  it('reads rules and commands from the selected commit and offers the template for a commit without rules', async () => {
    const asked: unknown[] = [];
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    setTransport(goalFake((query) => {
      asked.push(query ?? null);
      const baseline = (query as { baseline?: string } | undefined)?.baseline;
      return baseline === 'b'.repeat(40)
        ? onboardingOf({ baseline })
        : onboardingOf({ rules: { path: '.multi-agent/rules.yaml', status: 'ready', detail: null, commands: [{ name: 'ci', argv: ['python3', '-m', 'unittest'] }] }, next_steps: [{ kind: 'create_goal' }] });
    }));
    await jobs.init();
    render(<NewGoalPage />);
    // HEAD is asked without a baseline parameter and shows its registered command.
    const commands = await screen.findByRole('list', { name: '已登记的命令' });
    expect(within(commands).getByText('ci')).toBeTruthy();
    expect(within(commands).getByText('python3 -m unittest')).toBeTruthy();
    expect(asked[0]).toBeNull();

    // The older commit has no rules: the template is open and can be copied.
    fireEvent.change(screen.getByLabelText('或选分支'), { target: { value: 'b'.repeat(40) } });
    expect(await screen.findByText('这个提交里没有规则文件', undefined, { timeout: 4000 })).toBeTruthy();
    expect(asked).toContainEqual({ baseline: 'b'.repeat(40) });
    expect(screen.getByText('不是当前 HEAD')).toBeTruthy();
    expect(screen.getByLabelText('规则文件模板').textContent).toBe(TEMPLATE);
    fireEvent.click(screen.getByRole('button', { name: '复制模板' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(TEMPLATE));
    expect(await screen.findByText('已复制到剪贴板。')).toBeTruthy();
    expect(screen.queryByRole('list', { name: '已登记的命令' })).toBeNull();
  });

  it('shows each provider state with what can be done about it', async () => {
    const check = (status: string, extra: object = {}) => ({ ...unknownProvider, status, ...extra });
    let providers: Onboarding['providers'] = {
      codex: check('missing_cli'),
      claude: check('check_failed', { check_job_id: 'job-check', observed_at: '2026-11-25T10:00:00+00:00', error: { code: 'environment_check_failed', message: 'The CLI printed no version.' } }),
    };
    let steps: Onboarding['next_steps'] = [{ kind: 'install_cli', provider: 'codex' }, { kind: 'check_provider', provider: 'claude' }, { kind: 'create_goal' }];
    setTransport(goalFake(() => onboardingOf({ providers, next_steps: steps })));
    await jobs.init();
    const first = render(<NewGoalPage />);
    // Without the CLI there is nothing to check; the hint says to install it.
    expect(await screen.findByText('找不到命令行程序')).toBeTruthy();
    expect(screen.getByText('找不到 Codex 的命令行程序。要用它的模型，先安装。')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /检查 Codex 登录|重新检查 Codex/ })).toBeNull();
    // A failed check shows the service's error, its record, and can be run again.
    expect(screen.getByText('检查失败')).toBeTruthy();
    expect(screen.getByText('The CLI printed no version.')).toBeTruthy();
    expect(screen.getByRole('link', { name: '检查记录' }).getAttribute('href')).toBe('/jobs/job-check');
    expect(screen.getByText('上一次检查 Claude 没有成功。看上面的错误，处理后再检查一次。')).toBeTruthy();
    expect(screen.queryByText(/还没有确认 Claude 的登录/)).toBeNull();
    expect(screen.getByRole('button', { name: '重新检查 Claude' })).toBeTruthy();
    first.unmount();

    // While a check runs there is no second button; a finished one shows what was observed.
    providers = {
      codex: check('checking', { check_job_id: 'job-running' }),
      claude: check('ready', { cli_version: '2.1.325 (Claude Code)', observed_at: '2026-11-25T10:00:00+00:00', check_job_id: 'job-done' }),
    };
    steps = [{ kind: 'create_goal' }];
    render(<NewGoalPage />);
    expect(await screen.findByText('检查中')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /检查 Codex 登录|重新检查 Codex/ })).toBeNull();
    expect(screen.getByText('已登录')).toBeTruthy();
    expect(screen.getByText('2.1.325 (Claude Code)')).toBeTruthy();
    expect(screen.getByRole('button', { name: '重新检查 Claude' })).toBeTruthy();
    // A state this page has never seen is shown as written rather than dropped.
    expect(screen.queryByText('unknown')).toBeNull();
  });

  it('shows a check as running from the click on, without a second button to press', async () => {
    let checking: Job = { job_id: 'job-e', action: 'environment.refresh', state: 'running', exit_code: null, result: null, error: null, args: { provider: 'codex' } };
    const submit = vi.fn(() => ({ job_id: 'job-e', state: 'queued' }));
    let providers: Onboarding['providers'] = { codex: unknownProvider, claude: unknownProvider };
    setTransport(goalFake(() => onboardingOf({ providers }), { '/jobs': submit }, { '/jobs/job-e': () => checking }));
    await jobs.init();
    render(<NewGoalPage />);
    const button = await screen.findByRole('button', { name: '检查 Codex 登录' }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ action: 'environment.refresh', args: { provider: 'codex' } }));
    // The service still answers "unknown" until its own read; the page knows the check is running.
    expect(await screen.findByText('检查中')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /检查 Codex 登录|重新检查 Codex/ })).toBeNull();
    expect(screen.getByRole('button', { name: '检查 Claude 登录' })).toBeTruthy();
    providers = { codex: { ...unknownProvider, status: 'ready', cli_version: 'codex-cli 0.169.0' }, claude: unknownProvider };
    checking = { ...checking, state: 'finished', result: { logged_in: true } };
    expect(await screen.findByText('已登录', undefined, { timeout: 4000 })).toBeTruthy();
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it('lets the original text be confirmed with a valid baseline before any model is signed in', async () => {
    const submit = vi.fn(() => ({ job_id: 'job-c', state: 'queued' }));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    setTransport(goalFake(() => onboardingOf({}), { '/jobs': submit }, {
      '/jobs/job-c': finished('job-c', 'project.confirm', { resources: [{ kind: 'project', id: 'proj' }, { kind: 'goal', id: 'goal' }] }),
    }));
    await jobs.init();
    render(<NewGoalPage />);
    await screen.findByText('需要登录');
    const confirm = screen.getByRole('button', { name: '直接确认为目标' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('原始目标'), { target: { value: 'Record the goal first.' } });
    expect(loadDraft(draftKey('/repo'))?.goal).toBe('Record the goal first.');
    await waitFor(() => expect(confirm.disabled).toBe(false));
    expect(screen.getByText(/直接确认原文不需要顾问/)).toBeTruthy();
    fireEvent.click(confirm);
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ action: 'project.confirm',
      args: { baseline: 'a'.repeat(40), original_goal: 'Record the goal first.', confirmed_goal: 'Record the goal first.' } }));
    // The service returned the goal: the submitted draft is gone.
    await waitFor(() => expect(loadDraft(draftKey('/repo'))).toBeNull(), { timeout: 4000 });
  });

  it('blocks confirmation while the repository has no commit', async () => {
    setTransport(fakeTransport({
      '/workspace': { ...workspace, head: null, branches: [] }, '/jobs': { items: [] }, '/runs': { items: [] },
      '/workspace/onboarding': onboardingOf({ head: null, baseline: null, branch: 'main', branches: [], rules: { path: '.multi-agent/rules.yaml', status: 'no_commit', detail: null, commands: [] }, next_steps: [{ kind: 'commit_baseline' }] }),
    }));
    await jobs.init();
    render(<NewGoalPage />);
    expect(await screen.findByText('仓库还没有提交。先在仓库里提交一次，作为基线。')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('原始目标'), { target: { value: 'A goal.' } });
    expect((screen.getByRole('button', { name: '直接确认为目标' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getAllByText('仓库还没有提交，先提交一个基线。').length).toBeGreaterThan(0);
  });

  it('restores the draft per repository and keeps it when the job fails or another tab stored a newer one', async () => {
    saveDraft(draftKey('/repo'), 'Yesterday’s unfinished goal.');
    saveDraft(draftKey('/elsewhere'), 'Another repository.');
    let outcome: Job = finished('job-d', 'project.confirm', { exit_code: 2, result: null, error: { code: 'cli_refused', message: 'refused' } });
    const submit = vi.fn(() => ({ job_id: 'job-d', state: 'queued' }));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    setTransport(goalFake(() => onboardingOf({}), { '/jobs': submit }, { '/jobs/job-d': () => outcome }));
    await jobs.init();
    const view = render(<NewGoalPage />);
    const goal = await screen.findByLabelText('原始目标') as HTMLTextAreaElement;
    expect(goal.value).toBe('Yesterday’s unfinished goal.');
    expect(screen.getByText(/已恢复这个仓库上次没提交的目标/)).toBeTruthy();

    // A refused job keeps the draft.
    const confirm = screen.getByRole('button', { name: '直接确认为目标' }) as HTMLButtonElement;
    await waitFor(() => expect(confirm.disabled).toBe(false));
    fireEvent.click(confirm);
    await waitFor(() => expect(window.location.pathname).toBe('/jobs/job-d'), { timeout: 4000 });
    expect(loadDraft(draftKey('/repo'))?.goal).toBe('Yesterday’s unfinished goal.');
    view.unmount();

    // A success that finds a newer text from another tab keeps that newer text.
    jobs.reset();
    await jobs.init();
    outcome = finished('job-d', 'project.confirm', { resources: [{ kind: 'project', id: 'proj' }] });
    render(<NewGoalPage />);
    const again = screen.getByRole('button', { name: '直接确认为目标' }) as HTMLButtonElement;
    await waitFor(() => expect(again.disabled).toBe(false));
    fireEvent.click(again);
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    saveDraft(draftKey('/repo'), 'Edited in another tab.');
    await waitFor(() => expect(window.location.pathname).toBe('/projects/proj'), { timeout: 4000 });
    expect(loadDraft(draftKey('/repo'))?.goal).toBe('Edited in another tab.');
    expect(loadDraft(draftKey('/elsewhere'))?.goal).toBe('Another repository.');
  });

  it('never shows the error of the previous baseline for the next one', async () => {
    const renders: { baseline: string; error: boolean }[] = [];
    function Probe({ baseline }: { baseline: string }) {
      const read = useOnboarding(baseline, 'a'.repeat(40));
      renders.push({ baseline, error: read.error !== null });
      return <p>{read.check ? 'checked' : read.error ? 'failed' : 'reading'}</p>;
    }
    setTransport(goalFake((query) => {
      if ((query as { baseline?: string } | undefined)?.baseline === 'typo') throw new ApiError(400, 'invalid_request', 'Git input rejected');
      return onboardingOf({});
    }));
    await jobs.init();
    const view = render(<Probe baseline="typo" />);
    await screen.findByText('failed');
    view.rerender(<Probe baseline="main" />);
    await screen.findByText('checked', undefined, { timeout: 4000 });
    expect(renders.filter((r) => r.baseline === 'main' && r.error)).toEqual([]);
  });

  it('clears the draft of a goal whose job succeeded after the page had been reloaded', async () => {
    let outcome: Job = { ...finished('job-c', 'project.consult'), state: 'running', exit_code: null };
    const submit = vi.fn(() => ({ job_id: 'job-c', state: 'queued' }));
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    const gets = { '/jobs/job-c': () => outcome };
    setTransport(goalFake(() => onboardingOf({}), { '/jobs': submit }, { ...gets, '/workspace': { ...workspace, presets: { source: 'test', rules: {}, roles: [
      { id: 'advisor', label: 'Advisor', families: [], candidates: [{ family: 'Fable', provider: 'claude', model: 'claude-fable-5-2', efforts: ['low', 'high'] }] },
    ] } } }));
    await jobs.init();
    const first = render(<NewGoalPage />);
    fireEvent.change(await screen.findByLabelText('原始目标'), { target: { value: 'Ask the advisor about this.' } });
    const consult = await screen.findByRole('button', { name: '请顾问完善' }) as HTMLButtonElement;
    await waitFor(() => expect(consult.disabled).toBe(false));
    fireEvent.click(consult);
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    await screen.findAllByText('已提交，等作业结束');

    // The tab is reloaded while the consultation runs: the page that would clear the draft is gone.
    first.unmount();
    jobs.reset();
    setTransport(goalFake(() => onboardingOf({}), {}, { ...gets, '/jobs': () => ({ items: [outcome] }) }));
    await jobs.init();
    render(<NewGoalPage />);
    expect((await screen.findByLabelText('原始目标') as HTMLTextAreaElement).value).toBe('Ask the advisor about this.');
    // The job ends well afterwards: the text was taken by the service, so it is no draft any more.
    outcome = finished('job-c', 'project.consult', { resources: [{ kind: 'consultation', id: 'c1' }] });
    await waitFor(() => expect(loadDraft(draftKey('/repo'))).toBeNull(), { timeout: 4000 });
    await waitFor(() => expect((screen.getByLabelText('原始目标') as HTMLTextAreaElement).value).toBe(''));
    expect(screen.getByRole('link', { name: '查看那个作业' }).getAttribute('href')).toBe('/jobs/job-c');
  });

  it('carries a failed consultation into the form without touching the repository draft', async () => {
    saveDraft(draftKey('/repo'), 'A different unsubmitted goal.');
    setTransport(goalFake(() => onboardingOf({ baseline: 'b'.repeat(40) }), {}, {
      '/resources/consultation/c9': { kind: 'consultation', id: 'c9', artifacts: [], data: {
        kind: 'goal_consultation', consultation_id: 'c9', baseline_commit: 'b'.repeat(40), original_goal: 'Build a report', project_id: 'proj-7',
        started_at: null, finished_at: null, status: 'failed', draft: null, seats: [],
      } },
      '/runs': { items: [{ kind: 'project', id: 'proj-7', label: 'Reports', parent_id: null, updated_at: 't' }] },
    }));
    await jobs.init();
    render(<NewGoalPage consultation="c9" />);
    expect((await screen.findByLabelText('原始目标') as HTMLTextAreaElement).value).toBe('Build a report');
    expect((screen.getByLabelText('基线提交') as HTMLInputElement).value).toBe('b'.repeat(40));
    await waitFor(() => expect((screen.getByLabelText('归入项目') as HTMLSelectElement).value).toBe('proj-7'));
    expect(screen.getByRole('link', { name: '打开那次咨询' }).getAttribute('href')).toBe('/consultations/c9');
    expect(loadDraft(draftKey('/repo'))?.goal).toBe('A different unsubmitted goal.');
  });
});

it('sends one environment check however fast the hint button is clicked, and holds it while the check runs', async () => {
  let check: Job = { job_id: 'job-e', action: 'environment.refresh', state: 'running', exit_code: null, result: null, error: null, args: { provider: 'codex' } };
  const submit = vi.fn(() => ({ job_id: 'job-e', state: 'queued' }));
  setTransport(fakeTransport({ '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-e': () => check }, { '/jobs': submit }));
  await jobs.init();
  render(<EnvironmentHint ws={workspace} />);
  const codex = screen.getByRole('button', { name: '检查 Codex 环境' }) as HTMLButtonElement;
  await waitFor(() => expect(codex.disabled).toBe(false));
  // A double click: the second one lands before the service has answered the first.
  fireEvent.click(codex);
  fireEvent.click(codex);
  await waitFor(() => expect(submit).toHaveBeenCalled());
  // While the check runs the button keeps its name and cannot be pressed again.
  await waitFor(() => expect(codex.disabled).toBe(true));
  expect(screen.getByText('正在检查 Codex 环境…')).toBeTruthy();
  fireEvent.click(codex);
  await new Promise((r) => setTimeout(r, 50));
  expect(submit).toHaveBeenCalledTimes(1);
  expect(submit).toHaveBeenCalledWith({ action: 'environment.refresh', args: { provider: 'codex' } });
  // The other provider's check is its own.
  expect((screen.getByRole('button', { name: '检查 Claude 环境' }) as HTMLButtonElement).disabled).toBe(false);
  check = { ...check, state: 'finished', result: { logged_in: true } };
  await waitFor(() => expect(codex.disabled).toBe(false), { timeout: 4000 });
});

// ---- 5 验收交付 ----------------------------------------------------------------------

const delivery: Delivery = {
  receipt: {
    acceptance_id: 'acceptance-1', integration_id: 'i1', target_ref: 'refs/heads/main', target_baseline: 'b'.repeat(40),
    candidate: { ref: 'refs/heads/multi-agent/integrations/i1', commit: 'c'.repeat(40), tree: 't'.repeat(40) },
    test_attempt: 2, test_evidence_hash: 'e'.repeat(64), review_round_id: 'r1', snapshot_id: 's1', review_checkpoint_hash: 'f'.repeat(64),
    accepted_at: '2026-11-25T10:00:00+00:00',
  },
  tasks: [{ id: 'T-0800', goal: 'Deliver frontend/status.js' }],
  tests: [{ name: 'build', exit_code: 0 }, { name: 'test', exit_code: 0 }],
  links: { integration: '/integrations/i1', diff: '/diffs/snapshot/s1', review: '/reviews/r1' },
  markdown: `## 集成交付摘要\n\n- 候选提交 \` ${'c'.repeat(40)} \`\n`,
};

describe('验收交付', () => {
  it('previews the receipt with full identifiers and reports whether the copy worked', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/integrations/i1/delivery': delivery }));
    render(<DeliverySummary id="i1" />);
    expect(await screen.findByText('c'.repeat(40))).toBeTruthy();
    expect(screen.getByRole('group', { name: '交付摘要' })).toBeTruthy();
    expect(screen.getByText('b'.repeat(40))).toBeTruthy();
    expect(screen.getByText('e'.repeat(64))).toBeTruthy();
    expect(screen.getByText('f'.repeat(64))).toBeTruthy();
    expect(screen.getByText('第 2 次测试通过')).toBeTruthy();
    expect(screen.getByRole('link', { name: '冻结差异' }).getAttribute('href')).toBe('/diffs/snapshot/s1');
    expect(screen.getByRole('link', { name: '审核依据' }).getAttribute('href')).toBe('/reviews/r1');
    expect((screen.getByLabelText('摘要预览（Markdown）') as HTMLTextAreaElement).value).toBe(delivery.markdown);
    fireEvent.click(screen.getByRole('button', { name: '复制交付摘要' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(delivery.markdown));
    expect(await screen.findByText('已复制到剪贴板。')).toBeTruthy();

    writeText.mockRejectedValueOnce(new Error('denied'));
    fireEvent.click(screen.getByRole('button', { name: '复制交付摘要' }));
    expect(await screen.findByText(/复制失败：浏览器没有给剪贴板权限/)).toBeTruthy();
  });

  it('says the summary needs an acceptance receipt when the service answers 409', async () => {
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/integrations/i1/delivery': new ApiError(409, 'job_unavailable', 'A delivery summary is available after acceptance.') }));
    render(<DeliverySummary id="i1" />);
    expect(await screen.findByText('交付摘要：还没有验收收据，接受后才能导出。')).toBeTruthy();
  });
});

// ---- Answers without the expected keys ------------------------------------------------

it('still renders when the new reads come back without their keys', async () => {
  // The contract reads a missing key as null; a stub or an older service may answer anything.
  setTransport(fakeTransport({
    '/workspace': workspace, '/jobs': { items: [] }, '/runs': { items: [] },
    '/attention': { items: [] }, '/workspace/onboarding': { items: [] }, '/integrations/i1/delivery': { items: [] },
    '/integrations/i2/delivery': { receipt: { integration_id: 'i2' } },
    '/integrations/i2/rework-eligibility': { integration_id: 'i2', eligible: false, reason: 'No.', findings: [{ seat: 'seat-1', finding_id: 'f', path: 'a.py' }] },
    '/reviews/r1/findings': { round_id: 'r1', groups: [{ blocking: true }, { blocking: false, members: [{ seat: 'seat-1', finding }] }] },
    '/jobs/job-x': finished('job-x', 'project.confirm'), '/jobs/job-x/activity': { items: [] },
  }));
  await jobs.init();
  render(<>
    <Inbox /><NewGoalPage /><DeliverySummary id="i1" /><JobPage id="job-x" />
    <DeliverySummary id="i2" /><ReworkBlock it={{ integration_id: 'i2', rework: null } as unknown as IntegrationRecord} enabled /><Findings />
  </>);
  expect(await screen.findByText('现在没有待办。')).toBeTruthy();
  // Entries that lack their inner keys (owners, candidate, members, anchor) render as empty.
  expect(await screen.findByText('没有成员改过这个文件')).toBeTruthy();
  expect(await screen.findByText('Status is still draft')).toBeTruthy();
  expect(await screen.findByRole('group', { name: '交付摘要' })).toBeTruthy();
  fireEvent.change(await screen.findByLabelText('原始目标'), { target: { value: 'A goal.' } });
  await waitFor(() => expect((screen.getByRole('button', { name: '直接确认为目标' }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByRole('heading', { name: '检查接入' })).toBeNull();
  await screen.findByRole('heading', { name: '概况' });
  expect(screen.queryByRole('heading', { name: '最近的事件' })).toBeNull();
  expect(screen.getAllByRole('group', { name: '交付摘要' })).toHaveLength(1);
});

// ---- English ------------------------------------------------------------------------

it('shows the new panels without Chinese text in English', async () => {
  setLangForTest('en');
  setTransport(fakeTransport({
    '/workspace': workspace, '/jobs': { items: [] }, '/runs': { items: [] },
    '/attention': { items: [stage('ingest'), accept], complete: false },
    '/reviews/r1/findings': findingsOf([
      { id: 1, kind: 'decision', created_at: '2026-11-25T10:00:00+00:00', decision: 'disputed', reason: 'Excluded upstream.' },
      { id: 2, kind: 'answer', created_at: '2026-11-25T10:01:00+00:00', question: 'Which input?', answer: 'Zero.', evidence: { reported_model: null, reported_effort: null } },
      { id: 3, kind: 'linked_plan', created_at: '2026-11-25T10:02:00+00:00', planning_run_id: 'plan-9' },
    ]),
    '/integrations/i1/delivery': { ...delivery, markdown: '## Delivery\n' },
    '/workspace/onboarding': onboardingOf({}),
    '/jobs/job-old': { job_id: 'job-old', action: 'review.retry', state: 'interrupted', exit_code: null, result: null, error: null },
    '/jobs/job-old/activity': activity({
      job_id: 'job-old', state: 'interrupted', history_truncated: true, events: [event(1, 'provider_wait'), event(2, 'attempt_settled', 'followup:reviewer-1', 'failed')],
      actors: [{ resource: { kind: 'review', id: 'r1' }, seat: 'reviewer-2', provider: 'claude', attempt: 1, state: 'unconfirmed', last_observed_state: 'running', observed_at: '2026-11-25T10:00:04+00:00', last_event: 'message' }],
      reviews: [{ round_id: 'r1', snapshot_id: 's1', retry: null, seats: [{ seat: 'reviewer-1', provider: 'codex', attempt: null, state: 'pending', retryable: true }] }],
      recovery: [{ resource: { kind: 'review', id: 'r1' }, action: 'review.retry', args: { round_id: 'r1', seat_keys: ['reviewer-1'], expected_attempts: { 'reviewer-1': 0 }, recover: true } }],
    }),
  }));
  await jobs.init();
  const { container } = render(<><Inbox /><Findings /><DeliverySummary id="i1" /><NewGoalPage /><JobPage id="job-old" /></>);
  await screen.findByText('Waiting for me');
  await screen.findByText('Your decision');
  await screen.findByText('Delivery summary');
  await screen.findByText('Setup check');
  await screen.findByText('Rules file template');
  await screen.findByText('What can continue');
  const found = [...(container.textContent ?? '').matchAll(/[^\s]*[一-鿿：；，、（）。「」][^\s]*/g)].map((m) => m[0]);
  expect(found).toEqual([]);
});

// ---- After the backend's review of the integration summary --------------------------------

describe('后端核对后的改动', () => {
  it('reads a partial inbox again on its own when no job explains it, and never calls it all done', async () => {
    let answer: Attention = { items: [], complete: false };
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/attention': () => answer }));
    await jobs.init();
    function Quick() { return <AttentionPanel attention={useAttention(40)} />; }
    render(<Quick />);
    expect(await screen.findByText(/这次没有读到待办，但读取不完整，不代表都处理完了/)).toBeTruthy();
    expect(screen.queryByText('现在没有待办。')).toBeNull();
    // No job runs here, and the note does not claim one does.
    expect(screen.getByText(/这次只读到一部分待办/).textContent).not.toContain('作业运行中');
    answer = { items: [stage('report')], complete: true };
    expect(await screen.findByRole('link', { name: 'Stage report' }, { timeout: 4000 })).toBeTruthy();
    expect(screen.queryByText(/这次只读到一部分待办/)).toBeNull();
  });

  it('keeps reading an interrupted job while its controller holds the journal, then only on request or when back at the front', async () => {
    const interrupted: Job = { job_id: 'job-i', action: 'task.review', state: 'interrupted', exit_code: null, result: null, error: null };
    const recover = { round_id: 'r1', seat_keys: ['reviewer-2'], expected_attempts: { 'reviewer-2': 1 }, recover: true };
    let current = activity({ job_id: 'job-i', state: 'interrupted', controller_holds_journal: true });
    let reads = 0;
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-i': interrupted,
      '/jobs/job-i/activity': () => { reads += 1; return current; },
    }));
    await jobs.init();
    function Quick() { return <JobActivityView job={interrupted} activity={useJobActivity('job-i', 40)} />; }
    render(<Quick />);
    expect(await screen.findByText(/这个作业启动的 CLI 还在运行/)).toBeTruthy();
    // The controller lets go. No job ends, and the recovery still shows up.
    current = activity({ job_id: 'job-i', state: 'interrupted', controller_holds_journal: false, recovery: [{ resource: { kind: 'review', id: 'r1' }, action: 'task.retry_review', args: recover }] });
    expect(await screen.findByRole('button', { name: '接管并补跑中断的席位（reviewer-2）' }, { timeout: 4000 })).toBeTruthy();
    // Nothing holds the journal any more: the timer has stopped.
    await new Promise((r) => setTimeout(r, 60));
    const settled = reads;
    await new Promise((r) => setTimeout(r, 200));
    expect(reads).toBe(settled);
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(reads).toBe(settled + 1));
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(reads).toBe(settled + 2));
  });

  it('shows states and events it has no words for as they came', async () => {
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-n': finished('job-n', 'task.test'),
      '/jobs/job-n/activity': activity({
        job_id: 'job-n', events: [event(1, 'some_new_event', 'test-1', 'some_new_state')],
        actors: [{ resource: { kind: 'task', id: 't1' }, seat: 'test-1', provider: null, attempt: 1, state: 'another_new_state', observed_at: '2026-11-25T10:00:04+00:00', last_event: 'some_new_event' }],
      }),
      '/attention': { items: [{ kind: 'new-kind', id: 'x1', label: 'A new row', reason: 'some_new_reason', detail: 'What the service says about it.', href: '/tasks/x1' }], complete: true },
    }));
    await jobs.init();
    render(<><JobPage id="job-n" /><Inbox /></>);
    const list = await screen.findByRole('list', { name: '最近的事件' });
    expect(within(list).getByText('some_new_event')).toBeTruthy();
    expect(within(list).getByText('some_new_state')).toBeTruthy();
    expect(screen.getByText('another_new_state')).toBeTruthy();
    expect(await screen.findByText('What the service says about it.')).toBeTruthy();
    expect(screen.getByText('new-kind')).toBeTruthy();
  });

  it('says a job that ran to a conclusion that is not a pass apart from a refused one and from a check without an exit code', async () => {
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] },
      '/jobs/job-u': finished('job-u', 'task.retry_review', { exit_code: 1, result: { assessment: { status: 'needs_rework' } }, resources: [{ kind: 'review', id: 'r1' }] }),
      '/jobs/job-u/activity': activity({ job_id: 'job-u' }),
      '/jobs/job-f': finished('job-f', 'task.review', { exit_code: 2, result: null, error: { code: 'cli_refused', message: 'Seat attempts have changed.' }, error_kind: 'refused' }),
      '/jobs/job-o': finished('job-o', 'project.plan', { exit_code: 1, result: { status: 'failed' } }),
      '/jobs/job-o/activity': activity({ job_id: 'job-o' }),
      '/jobs/job-f/activity': activity({ job_id: 'job-f' }),
      '/jobs/job-e': { job_id: 'job-e', action: 'environment.refresh', state: 'finished', exit_code: null, result: { status: 'ready' }, error: null },
      '/jobs/job-e/activity': activity({ job_id: 'job-e' }),
    }));
    await jobs.init();
    let view = render(<JobPage id="job-u" />);
    // Exit code 1 with a receipt. For a review the receipt is a conclusion: go and read the findings.
    expect(await screen.findByText(/这个作业跑到了结束。退出码 1 表示审核的结论不是通过：打开下面的审核记录看意见/)).toBeTruthy();
    expect(screen.getByText('结果不满足')).toBeTruthy();
    expect(screen.queryByText(/执行时拒绝了它/)).toBeNull();
    view.unmount();
    view = render(<JobPage id="job-f" />);
    expect(await screen.findByText(/服务受理了这次提交，执行时拒绝了它。通常是提交时页面上的状态已经过期/)).toBeTruthy();
    expect(screen.getByText('Seat attempts have changed.')).toBeTruthy();
    expect(screen.queryByText(/这个作业跑到了结束/)).toBeNull();
    view.unmount();
    // Another kind of action with the same exit code: the page does not say which kind of result it is.
    view = render(<JobPage id="job-o" />);
    expect(await screen.findByText(/这个作业跑到了结束并留下了结果。退出码 1 表示结果不是成功/)).toBeTruthy();
    view.unmount();
    render(<JobPage id="job-e" />);
    expect(await screen.findByText('无（环境检查在服务内执行）')).toBeTruthy();
    expect(screen.getByText('已结束')).toBeTruthy();
    expect(screen.queryByText(/这个作业跑到了结束/)).toBeNull();
    expect(screen.queryByText(/执行时拒绝了它/)).toBeNull();
  });

  it('says a refused follow-up apart from a failed one, reads the findings again and keeps the question', async () => {
    let reads = 0;
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/reviews/r1/findings': () => { reads += 1; return findingsOf(); },
      '/jobs/job-q': finished('job-q', 'review.followup', { exit_code: 2, result: null, error: { code: 'cli_refused', message: 'finding anchor requires the current completed seat attempt' }, error_kind: 'refused' }),
    }, { '/jobs': { job_id: 'job-q', state: 'queued' } }));
    await jobs.init();
    render(<Findings />);
    await screen.findAllByText('Status is still draft');
    const card = within(document.querySelectorAll<HTMLElement>('.finding-card')[0]!);
    fireEvent.change(card.getByLabelText('要问的问题'), { target: { value: 'Why?' } });
    const ask = card.getByRole('button', { name: '提交追问' }) as HTMLButtonElement;
    await waitFor(() => expect(ask.disabled).toBe(false));
    const before = reads;
    fireEvent.click(ask);
    const alert = await card.findByRole('alert', undefined, { timeout: 4000 });
    expect(alert.textContent).toContain('追问被服务拒绝：');
    expect(alert.textContent).toContain('finding anchor requires the current completed seat attempt');
    await waitFor(() => expect(reads).toBeGreaterThan(before));
    expect((card.getByLabelText('要问的问题') as HTMLTextAreaElement).value).toBe('Why?');
  });

  it('opens the planning the service already has when the same correction is submitted again', async () => {
    const submit = vi.fn(() => ({ job_id: 'job-p', state: 'queued' }));
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/reviews/r1/findings': findingsOf(),
      // No second planning: the receipt names the one that exists.
      '/jobs/job-p': finished('job-p', 'plan.create', { result: { operation: 'plan', planning_run_id: 'plan-linked', reused: true }, resources: [] }),
    }, { '/jobs': submit }));
    await jobs.init();
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    window.history.replaceState(null, '', '/task-plans/new');
    render(<NewTaskPlanPage from={null} questionIndex={null} review={{ round: 'r1', seat: 'reviewer-2', attempt: 1, finding: 'status-blocker' }} />);
    const start = await screen.findByRole('button', { name: '开始单卡规划' }) as HTMLButtonElement;
    await waitFor(() => expect(start.disabled).toBe(false));
    fireEvent.click(start);
    await waitFor(() => expect(`${window.location.pathname}${window.location.search}`).toBe('/task-plans/plan-linked?reused=1'), { timeout: 4000 });
  });

  it('keeps a correction whose job ended without a planning on its form, says how it ended and reads the finding again', async () => {
    let reads = 0;
    let ending: Job = finished('job-p', 'plan.create', { exit_code: 2, result: null, resources: [], error: { code: 'cli_refused', message: 'finding anchor requires the current completed seat attempt' }, error_kind: 'refused' });
    const submit = vi.fn(() => ({ job_id: 'job-p', state: 'queued' }));
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/reviews/r1/findings': () => { reads += 1; return findingsOf(); }, '/jobs/job-p': () => ending,
    }, { '/jobs': submit }));
    await jobs.init();
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    window.history.replaceState(null, '', '/task-plans/new');
    const view = render(<NewTaskPlanPage from={null} questionIndex={null} review={{ round: 'r1', seat: 'reviewer-2', attempt: 1, finding: 'status-blocker' }} />);
    const start = await screen.findByRole('button', { name: '开始单卡规划' }) as HTMLButtonElement;
    await waitFor(() => expect(start.disabled).toBe(false));
    const before = reads;
    fireEvent.click(start);
    const alert = await screen.findByRole('alert', undefined, { timeout: 4000 });
    expect(alert.textContent).toContain('被服务拒绝：finding anchor requires the current completed seat attempt');
    expect(window.location.pathname).toBe('/task-plans/new');
    await waitFor(() => expect(reads).toBeGreaterThan(before));
    view.unmount();

    // Lost sight of: the same request may be sent again, and the page says why that is safe.
    ending = { job_id: 'job-p', action: 'plan.create', state: 'interrupted', exit_code: null, result: null, error: { code: 'service_interrupted', message: 'stopped' }, error_kind: 'interrupted' };
    jobs.reset();
    await jobs.init();
    render(<NewTaskPlanPage from={null} questionIndex={null} review={{ round: 'r1', seat: 'reviewer-2', attempt: 1, finding: 'status-blocker' }} />);
    const again = await screen.findByRole('button', { name: '开始单卡规划' }) as HTMLButtonElement;
    await waitFor(() => expect(again.disabled).toBe(false));
    fireEvent.click(again);
    const note = await screen.findByRole('alert', undefined, { timeout: 4000 });
    expect(note.textContent).toContain('内容相同的重新提交不会建第二份');
    await waitFor(() => expect((screen.getByRole('button', { name: '开始单卡规划' }) as HTMLButtonElement).disabled).toBe(false));
  });

  it('says what the service said when it finds a submission wrong, and that nothing was sent when another job runs', async () => {
    let post: unknown = new ApiError(400, 'invalid_request', 'expected_attempts must match the selected seats');
    const interrupted: Job = { job_id: 'job-a', action: 'review.run', state: 'interrupted', exit_code: null, result: null, error: null };
    let reads = 0;
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-a': interrupted,
      '/jobs/job-a/activity': () => { reads += 1; return activity({ state: 'interrupted', recovery: [{ resource: { kind: 'review', id: 'r1' }, action: 'review.retry', args: { round_id: 'r1', seat_keys: ['reviewer-2'], expected_attempts: { 'reviewer-2': 1 } } }] }); },
    }, { '/jobs': () => post }));
    await jobs.init();
    render(<JobPage id="job-a" />);
    const button = await screen.findByRole('button', { name: '只补跑缺失的席位（reviewer-2）' }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    const before = reads;
    fireEvent.click(button);
    const said = await screen.findByText(/expected_attempts must match the selected seats/);
    expect(said.getAttribute('role')).toBe('alert');
    // A request that was not accepted is a reason to read what can be continued again.
    await waitFor(() => expect(reads).toBeGreaterThan(before));
    post = new ApiError(409, 'busy', 'Another job is running.', { active_job_id: null });
    await waitFor(() => expect((screen.getByRole('button', { name: '只补跑缺失的席位（reviewer-2）' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '只补跑缺失的席位（reviewer-2）' }));
    expect(await screen.findByText(/已有作业在运行，这次没有提交。等它结束后再试。/)).toBeTruthy();
  });

  it('follows a rework to the integration it produced and says there that the rework job did not pass', async () => {
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    const rework = finished('job-r', 'integration.rework', { exit_code: 1, result: { rework: { child_id: 'i-child' }, revision: { integration_id: 'i-child' } } });
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-r': rework,
      '/integrations/i1/rework-eligibility': { integration_id: 'i1', round_id: 'r1', lock_busy: false, eligible: true, reason: null, owner: { task_id: 'T-0800', task_run_id: 'task-a' }, findings: [] },
    }, { '/jobs': { job_id: 'job-r', state: 'queued' } }));
    await jobs.init();
    window.history.replaceState(null, '', '/integrations/i1');
    const view = render(<ReworkBlock it={{ integration_id: 'i1', rework: null } as unknown as IntegrationRecord} enabled />);
    const button = await screen.findByRole('button', { name: '按阻塞意见返工（每条谱系一次）' }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(`${window.location.pathname}${window.location.search}`).toBe('/integrations/i-child?rework_job=job-r'), { timeout: 4000 });
    view.unmount();
    render(<ReworkOutcome jobId="job-r" />);
    expect(await screen.findByText(/这个集成是作业「集成返工」里的返工产生的，但那个作业的结果不是成功（退出码 1）/)).toBeTruthy();
    expect(screen.getByRole('link', { name: '查看那个作业' }).getAttribute('href')).toBe('/jobs/job-r');
  });

  it('explains an archive without acceptance receipts, accepts the 409 by its code, and says in English that the export is Chinese', async () => {
    setTransport(fakeTransport({
      '/jobs': { items: [] },
      '/integrations/i-old/delivery': new ApiError(404, 'archive_unsupported', 'This archive predates integration acceptance.'),
      '/integrations/i-wait/delivery': new ApiError(400, 'job_unavailable', 'A delivery summary is available after acceptance.'),
      '/integrations/i1/delivery': delivery,
    }));
    let view = render(<DeliverySummary id="i-old" />);
    expect(await screen.findByText('交付摘要：这份归档早于验收收据功能，没有可导出的摘要。')).toBeTruthy();
    expect(document.querySelector('.problem')).toBeNull();
    view.unmount();
    view = render(<DeliverySummary id="i-wait" />);
    expect(await screen.findByText('交付摘要：还没有验收收据，接受后才能导出。')).toBeTruthy();
    view.unmount();
    view = render(<DeliverySummary id="i1" />);
    await screen.findByRole('group', { name: '交付摘要' });
    expect(screen.queryByText(/导出的摘要是中文的/)).toBeNull();
    view.unmount();
    setLangForTest('en');
    render(<DeliverySummary id="i1" />);
    expect(await screen.findByText(/The exported summary is written in Chinese/)).toBeTruthy();
  });

  it('keeps "the commands are registered" apart from "the environment can run them"', async () => {
    setTransport(fakeTransport({
      '/jobs': { items: [] },
      '/workspace/onboarding': onboardingOf({
        rules: { path: '.multi-agent/rules.yaml', status: 'ready', detail: null, commands: [{ name: 'unit', argv: ['python3', '-m', 'unittest'] }] },
        next_steps: [{ kind: 'create_goal' }],
      }),
    }));
    await jobs.init();
    function Check() { return <OnboardingPanel read={useOnboarding('', 'a'.repeat(40))} />; }
    render(<Check />);
    expect(await screen.findByText('已登记测试命令')).toBeTruthy();
    expect(screen.getByText('这里只确认命令已经登记。测试运行环境里能不能跑这些命令还没有验证，第一次运行测试时才知道。')).toBeTruthy();
  });

  it('takes the kind of an error from the service and makes no judgement where it gives none', () => {
    const ended = (extra: Partial<Job>) => jobEnding(finished('j', 'task.review', extra));
    const error = { code: 'whatever_the_code', message: 'm' };
    // The same exit code 2 without a receipt: the service says which it was.
    expect(ended({ exit_code: 2, result: null, error, error_kind: 'refused' })).toBe('refused');
    expect(ended({ exit_code: 2, result: null, error, error_kind: 'failed' })).toBe('failed');
    expect(ended({ exit_code: 0, result: null, error, error_kind: 'failed' })).toBe('failed');
    expect(ended({ exit_code: null, result: null, error, error_kind: 'failed' })).toBe('failed');
    expect(ended({ exit_code: 2, result: null, error, error_kind: 'cancelled' })).toBe('cancelled');
    expect(ended({ exit_code: null, result: null, error, error_kind: 'interrupted' })).toBe('interrupted');
    // An older service does not say, and the page does not guess from the code or the exit code.
    expect(ended({ exit_code: 2, result: null, error: { code: 'cli_refused', message: 'm' } })).toBe('errored');
    expect(ended({ exit_code: 2, result: null, error: { code: 'followup_failed', message: 'm' } })).toBe('errored');
    expect(ended({ exit_code: 2, result: null, error, error_kind: 'a_new_kind' })).toBe('errored');
    expect(ended({ exit_code: 3, result: null, error: null })).toBe('errored');
    // Without an error the exit code and the receipt are read.
    expect(ended({ exit_code: 0, error_kind: null })).toBe('done');
    expect(ended({ exit_code: null, result: { status: 'ready' }, error: null })).toBe('done');
    expect(ended({ exit_code: 1, result: { phase: 'failed' } })).toBe('unmet');
    expect(ended({ exit_code: 130, result: null, error: null })).toBe('cancelled');
    expect(jobEnding({ state: 'interrupted', exit_code: null, result: null, error: { code: 'state_write_failed', message: 'm' } as never, error_kind: 'failed' })).toBe('interrupted');
    expect(jobEnding({ state: 'running', exit_code: null, result: null, error: null })).toBe('unfinished');
  });

  it('shows a job that never started without calling it an environment check or saying nothing needs doing', async () => {
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] },
      '/jobs/job-l': finished('job-l', 'task.run', { exit_code: null, result: null, error: { code: 'cli_launch_failed', message: 'The CLI could not be started.' }, error_kind: 'failed' }),
      '/jobs/job-l/activity': activity({ job_id: 'job-l' }),
    }));
    await jobs.init();
    render(<JobPage id="job-l" />);
    expect(await screen.findByText('无（没有观测到退出码）')).toBeTruthy();
    expect(screen.getByText('失败')).toBeTruthy();
    expect(screen.queryByText('没有需要继续的动作。')).toBeNull();
    expect(screen.getByText(/这个作业没有可以直接继续的动作/)).toBeTruthy();
  });

  it('tries a failed activity reading again by itself and lets the user ask', async () => {
    let fail = true;
    let reads = 0;
    const done = finished('job-z', 'task.test');
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-z': done,
      '/jobs/job-z/activity': () => { reads += 1; return fail ? new ApiError(503, 'workspace_unavailable', 'busy') : activity({ job_id: 'job-z', events: [event(1, 'suite_settled', 'test-1', 'succeeded')] }); },
    }));
    await jobs.init();
    function Quick() { return <JobActivityView job={done} activity={useJobActivity('job-z', 15_000, 60)} />; }
    render(<Quick />);
    expect(await screen.findByText(/读不到作业活动/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '重新读取' })).toBeTruthy();
    await waitFor(() => expect(reads).toBeGreaterThan(2), { timeout: 4000 });
    fail = false;
    expect(await screen.findByRole('list', { name: '最近的事件' }, { timeout: 4000 })).toBeTruthy();
    // Read: nothing asks again.
    const settled = reads;
    await new Promise((r) => setTimeout(r, 250));
    expect(reads).toBe(settled);
  });

  it('does not call an interrupted rework a rework without result', async () => {
    setTransport(fakeTransport({
      '/workspace': workspace, '/jobs': { items: [] },
      '/integrations/i1/rework-eligibility': { integration_id: 'i1', round_id: 'r1', lock_busy: false, eligible: true, reason: null, owner: { task_id: 'T-0800', task_run_id: 'task-a' }, findings: [] },
      '/jobs/job-r': { job_id: 'job-r', action: 'integration.rework', state: 'interrupted', exit_code: null, result: null, error: { code: 'service_interrupted', message: 'The previous service stopped before observing a result.' } },
    }, { '/jobs': { job_id: 'job-r', state: 'queued' } }));
    await jobs.init();
    window.history.replaceState(null, '', '/integrations/i1');
    render(<ReworkBlock it={{ integration_id: 'i1', rework: null } as unknown as IntegrationRecord} enabled />);
    const button = await screen.findByRole('button', { name: '按阻塞意见返工（每条谱系一次）' }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    const alert = await screen.findByRole('alert', undefined, { timeout: 4000 });
    expect(alert.textContent).toContain('返工作业被中断，没有观测到结果');
    expect(alert.textContent).toContain('之后仍可能产生新的集成');
    expect(alert.textContent).not.toContain('返工没有产生新的集成');
  });

  it('tells the page a self-rework opens when the job that reworked did not end as a success', () => {
    vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
    const review = (extra: Partial<Job>) => finished('job-v', 'integration.review', { args: { integration_id: 'i1' }, ...extra });
    openIntegrationRevision(review({ exit_code: 1, result: { revision: { integration_id: 'i-child' } } }));
    expect(`${window.location.pathname}${window.location.search}`).toBe('/integrations/i-child?rework_job=job-v');
    openIntegrationRevision(review({ exit_code: 0, result: { revision: { integration_id: 'i-child' } } }));
    expect(`${window.location.pathname}${window.location.search}`).toBe('/integrations/i-child');
    // The same integration is no rework: nothing to tell.
    openIntegrationRevision(review({ exit_code: 1, result: { revision: { integration_id: 'i1' } } }));
    expect(`${window.location.pathname}${window.location.search}`).toBe('/integrations/i1');
  });

  it('shows what single-card planning requests by default, and submits nothing for it', async () => {
    const defaults = { source: 'cli_default', stage_mode: 'direct' as const, sol: { model: 'gpt-6.2-sol', effort: 'high' }, card_review: { provider: 'claude', model: 'claude-opus-5-5', effort: 'high' },
      task_plan: { draft: { provider: 'claude', model: 'claude-opus-5-5', effort: 'high' }, plan_review: { provider: 'codex', model: 'gpt-6.1-sol', effort: 'high' } } };
    const submit = vi.fn(() => ({ job_id: 'job-p', state: 'queued' }));
    setTransport(fakeTransport({
      '/workspace': { ...workspace, defaults }, '/jobs': { items: [] }, '/jobs/job-p': finished('job-p', 'plan.create'),
    }, { '/jobs': submit }));
    await jobs.init();
    render(<NewTaskPlanPage from={null} questionIndex={null} />);
    const shown = await screen.findAllByText(/不指定时默认使用/);
    expect(shown.map((line) => line.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      '不指定时默认使用 claude · claude-opus-5-5 · high', '不指定时默认使用 codex · gpt-6.1-sol · high',
    ]);
    fireEvent.change(screen.getByLabelText('单卡需求'), { target: { value: 'Add a check.' } });
    const start = screen.getByRole('button', { name: '开始单卡规划' }) as HTMLButtonElement;
    await waitFor(() => expect(start.disabled).toBe(false));
    fireEvent.click(start);
    await waitFor(() => expect(submit).toHaveBeenCalled());
    const body = (submit.mock.calls[0] as unknown[])[0] as { args: Record<string, unknown> };
    expect(Object.keys(body.args).sort()).toEqual(['baseline', 'requirements']);
    // Naming a model hides the default of that seat only.
    fireEvent.click(screen.getByLabelText('指定草拟模型'));
    await waitFor(() => expect(screen.getAllByText(/不指定时默认使用/)).toHaveLength(1));
  });

  it('shows no default where the service does not report one', async () => {
    setTransport(fakeTransport({ '/workspace': workspace, '/jobs': { items: [] } }));
    await jobs.init();
    render(<NewTaskPlanPage from={null} questionIndex={null} />);
    await screen.findByLabelText('单卡需求');
    expect(screen.queryByText(/不指定时默认使用/)).toBeNull();
  });
});
