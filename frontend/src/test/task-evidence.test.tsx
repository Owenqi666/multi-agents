import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { setTransport } from '../api/client';
import { jobs } from '../api/jobs';
import type { Candidate, ImplementationAttempt, Job, PlanningView, TaskView, Workspace } from '../api/types';
import { TaskPage } from '../pages/TaskPage';
import { TaskPlanPage } from '../pages/TaskPlanPage';
import { fakeTransport } from './fake';

const sol = { provider: 'codex', model: 'gpt-6.2-sol', effort: 'high' };
const terra = { provider: 'codex', model: 'gpt-6.1-terra', effort: 'low' };
const sonnet = { provider: 'claude-code', model: 'claude-sonnet-5-5', effort: 'low' };
const candidates: Candidate[] = [
  { family: 'Sol', provider: 'codex', model: sol.model, efforts: ['high'] },
  { family: 'Terra', provider: 'codex', model: terra.model, efforts: ['high', 'low'] },
  { family: 'Sonnet', provider: 'claude', model: sonnet.model, efforts: ['high', 'low'] },
];
const workspace: Workspace = {
  repository: '/repo', target_branch: 'main', head: 'base', read_only: false,
  environments: { claude: null, codex: null },
  presets: {
    source: 'test', rules: {},
    roles: ['implementation', 'fallback'].map((id) => ({ id, label: id, families: [], candidates })),
  },
};
const plan: PlanningView = {
  planning_run_id: 'plan-1', task_run_id: null, phase: 'ready', verification: 'verified',
  card: { goal: 'Update the checked output' }, attempts: { draft: [], review: [] },
  next_action: { kind: 'run', detail: 'The reviewed plan is ready for implementation.' },
  dispatch_configurable: true,
};
const done: Job = {
  job_id: 'task-run-job', action: 'task.run', state: 'finished', exit_code: 0, result: {}, error: null,
};
const read = (kind: string, id: string, data: unknown) => ({ kind, id, data, artifacts: [] });

async function showPlan(record: PlanningView) {
  const post = vi.fn(() => ({ job_id: done.job_id, state: 'queued' }));
  setTransport(fakeTransport({
    '/jobs': { items: [] }, '/workspace': workspace, [`/jobs/${done.job_id}`]: done,
    '/resources/plan/plan-1': read('plan', 'plan-1', record),
  }, { '/jobs': post }));
  await jobs.init();
  render(<TaskPlanPage id="plan-1" />);
  await screen.findByText('Update the checked output');
  return post;
}

describe('已复核独立单卡的实施配置', () => {
  it('sends the current order and effort after moving and removing backups', async () => {
    const post = await showPlan(plan);
    fireEvent.click(screen.getByLabelText('指定实施模型'));
    await waitFor(() => expect((screen.getByLabelText('实施模型：模型') as HTMLSelectElement).value).toBe(sol.model));
    const add = screen.getByRole('button', { name: '添加备选' });
    fireEvent.click(add);
    fireEvent.change(screen.getByLabelText('备选 1：模型'), { target: { value: terra.model } });
    fireEvent.change(screen.getByLabelText('备选 1：档位'), { target: { value: 'low' } });
    fireEvent.click(add);
    fireEvent.change(screen.getByLabelText('备选 2：模型'), { target: { value: sonnet.model } });
    fireEvent.change(screen.getByLabelText('备选 2：档位'), { target: { value: 'low' } });
    fireEvent.click(add);
    fireEvent.click(screen.getByRole('button', { name: '删除备选 3' }));
    fireEvent.click(screen.getByRole('button', { name: '备选 2 上移' }));
    expect((screen.getByLabelText('备选 1：模型') as HTMLSelectElement).value).toBe(sonnet.model);
    expect((screen.getByLabelText('备选 2：档位') as HTMLSelectElement).value).toBe('low');

    fireEvent.click(screen.getByRole('button', { name: '开始实施' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith({
      action: 'task.run', args: { planning_run_id: 'plan-1', implementation: sol, fallbacks: [sonnet, terra] },
    }));
    await jobs.waitFor(done.job_id);
  });

  it('keeps default dispatch when the form is left empty', async () => {
    const post = await showPlan(plan);
    fireEvent.click(screen.getByRole('button', { name: '开始实施' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith({ action: 'task.run', args: { planning_run_id: 'plan-1' } }));
    await jobs.waitFor(done.job_id);
  });

  it('does not offer configuration without the server capability and links an existing task', async () => {
    await showPlan({ ...plan, dispatch_configurable: undefined, task_run_id: 'task-1' });
    expect(screen.queryByRole('button', { name: '开始实施' })).toBeNull();
    expect(screen.getByRole('link', { name: '实施详情' }).getAttribute('href')).toBe('/tasks/task-1');
  });
});

it('separates attempted dispatch, reported usage and runtime reroutes for each implementation attempt', async () => {
  const attempts: ImplementationAttempt[] = [
    {
      request: { kind: 'task_attempt_started', worktree: '/repo-data/old/workspace', decision: { choice: sol, requested: sol }, started_at: null },
      runtime: { number: 1, status: 'failed', reported_model: null, reported_effort: null },
    },
    {
      request: {
        kind: 'task_attempt_started', worktree: '/repo-data/new/workspace', started_at: null,
        decision: {
          choice: terra, requested: sol, reason: 'enabled_ordered_fallback',
          selection_history: [{ choice: sol, outcome: 'known_unavailable' }, { choice: terra, outcome: 'selected_request' }],
        },
      },
      runtime: { number: 2, status: 'succeeded', reported_model: null, reported_effort: null, result: { outcome: 'completed', required_paths: [], summary: 'Changed calc.py only.' } },
      events: [{ kind: 'task_execution_prepared', dispatch_policy: { fallbacks: [sonnet, terra] } }],
      usage_evidence: { source: 'thread/tokenUsage/updated', counts: { 'total.inputTokens': 36, 'last.inputTokens': 9, 'last.outputTokens': 0 } },
      runtime_reroutes: [{ source: 'model/rerouted', from_model: 'original', to_model: 'rerouted', reason: 'highRiskCyberActivity' }],
    },
    {
      request: { kind: 'task_rework_started', decision: { choice: sonnet, requested: null }, started_at: null },
      runtime: {
        number: 3, status: 'succeeded', reported_model: 'claude-reported', reported_effort: null,
        result: { outcome: 'scope_blocked', required_paths: ['.multi-agent/rules.yaml'], summary: 'The runner has no working python3.', task_hash: 'h' },
      },
      usage_evidence: { source: 'result', counts: { output_tokens: 7, client_estimated_cost_usd: 0, constructor: 2 } },
      runtime_reroutes: [],
    },
  ];
  const task: TaskView = {
    task_run_id: 'task-1', planning_run_id: 'plan-1', implementation_attempt: 3, phase: 'delivered', candidate: null,
    verification: 'verified', attempts, tests: [], reviews: [], rework_used: true, next_action: null,
  };
  setTransport(fakeTransport({
    '/jobs': { items: [] }, '/workspace': workspace, '/resources/task/task-1': read('task', 'task-1', task),
  }));
  await jobs.init();
  render(<TaskPage id="task-1" />);
  const old = within((await screen.findByRole('heading', { name: '第 1 次实施' })).closest('li')!);
  expect(old.getAllByText('未确认').length).toBeGreaterThan(0);
  expect(old.queryByText('未记录改派')).toBeNull();
  const current = within(screen.getByRole('heading', { name: '第 2 次实施' }).closest('li')!);
  expect(current.getByText('/repo-data/new/workspace')).toBeTruthy();
  expect(current.getByText('按冻结顺序选用备选')).toBeTruthy();
  expect(current.getByText('已知不可用')).toBeTruthy();
  expect(current.getByText('thread/tokenUsage/updated')).toBeTruthy();
  expect(current.getByText('累计 · 输入 token').nextElementSibling?.textContent).toBe('36');
  expect(current.getByText('最近一次 · 输入 token').nextElementSibling?.textContent).toBe('9');
  expect(current.getByText('最近一次 · 输出 token').nextElementSibling?.textContent).toBe('0');
  expect(current.getByText('model/rerouted')).toBeTruthy();
  expect(current.getByText('highRiskCyberActivity')).toBeTruthy();
  expect(current.getByText('rerouted').closest('dd')?.previousElementSibling?.textContent).toBe('运行时改派');

  const rework = within(screen.getByRole('heading', { name: '返工' }).closest('li')!);
  expect(rework.getByText('claude-reported')).toBeTruthy();
  expect(rework.getByText('Claude 客户端估算（USD）').nextElementSibling?.textContent).toBe('0');
  expect(rework.getByText('constructor').nextElementSibling?.textContent).toBe('2');
  expect(rework.getByText('未记录改派')).toBeTruthy();

  // The implementer's own report: absent, done, and stopped for lack of scope with the paths it names.
  expect(old.getByText('实施者报告').nextElementSibling?.textContent).toBe('未确认');
  const reported = current.getByText('实施者报告').nextElementSibling!;
  expect(reported.textContent).toContain('报告完成');
  expect(reported.textContent).toContain('Changed calc.py only.');
  expect(reported.textContent).not.toContain('它说还需要改');
  const blocked = rework.getByText('实施者报告').nextElementSibling!;
  expect(blocked.querySelector('.badge-warn')?.textContent).toBe('报告改动范围不够，没有交付');
  expect(within(blocked as HTMLElement).getByText('.multi-agent/rules.yaml')).toBeTruthy();
  expect(blocked.textContent).toContain('The runner has no working python3.');
});

describe('同一个候选重新测试', () => {
  const failedTest = (extra: Record<string, unknown> = {}) => ({
    task_run_id: 'task-1', implementation_attempt: 1, test_attempt: 1, candidate: 'c'.repeat(40), phase: 'failed', verification: 'verified', source_current: true,
    // Out of order on purpose: the retest sends them by sequence.
    commands: [{ sequence: 2, name: 'test', status: 'failed', exit_code: 1 }, { sequence: 1, name: 'build', status: 'passed', exit_code: 0 }],
    details: { is_current: true, retry_ready: true, finished: true, cancel_requested: false, declaration_source: 'PM' },
    ...extra,
  });
  const taskOf = (id: string, tests: unknown[], next: unknown, extra: Record<string, unknown> = {}): TaskView => ({
    task_run_id: id, planning_run_id: 'plan-1', implementation_attempt: 1, phase: 'awaiting_delivery', candidate: 'c'.repeat(40),
    verification: 'verified', attempts: [], tests, reviews: [], rework_used: false, next_action: next, ...extra,
  } as unknown as TaskView);
  const review = { kind: 'task_review', test_attempt: 1, detail: 'Select reviewers.' };

  it('repeats the commands and declaration of the failed test and shows why the service refused', async () => {
    const post = vi.fn(() => ({ job_id: 'job-t', state: 'queued' }));
    setTransport(fakeTransport({
      '/jobs': { items: [] }, '/workspace': workspace, '/resources/task/task-1': read('task', 'task-1', taskOf('task-1', [failedTest()], review)),
      '/jobs/job-t': { job_id: 'job-t', action: 'task.test', state: 'finished', exit_code: 2, result: null, error: { code: 'cli_refused', message: 'test retry must preserve its managed policy' }, error_kind: 'refused' },
    }, { '/jobs': post }));
    await jobs.init();
    render(<TaskPage id="task-1" />);
    const button = await screen.findByRole('button', { name: '按原环境重新测试' }) as HTMLButtonElement;
    expect(screen.getByText(/服务换过工具目录之后的重测暂不支持，会被拒绝/)).toBeTruthy();
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(post).toHaveBeenCalledWith({
      action: 'task.test', args: { task_run_id: 'task-1', implementation_attempt: 1, commands: ['build', 'test'], declaration_source: 'PM' },
    }));
    // Said as the service said it, without a promise that nothing ran: the record above decides.
    expect(await screen.findByText(/重新测试被服务拒绝：test retry must preserve its managed policy。上面的测试记录是重新读取的/, undefined, { timeout: 4000 })).toBeTruthy();
    expect(screen.getByText(/它结束之前不能开始审核/)).toBeTruthy();
  });

  it('is offered only for the current test of the current implementation, and only where the service says it is ready', async () => {
    const cases: [string, TaskView][] = [
      ['the service says it is not ready', taskOf('task-a', [failedTest({ details: { is_current: true, retry_ready: false, finished: true, declaration_source: 'PM' } })], review)],
      ['an older service does not say', taskOf('task-b', [failedTest({ details: { is_current: true, finished: true, cancel_requested: false, declaration_source: 'PM' } })], review)],
      ['the record does not say who declared the commands', taskOf('task-c', [failedTest({ details: { is_current: true, retry_ready: true, finished: true } })], review)],
      ['the test belongs to an earlier implementation', taskOf('task-d', [failedTest()], { kind: 'task_test', detail: '' }, { implementation_attempt: 2 })],
      ['the record is not the current test', taskOf('task-e', [failedTest({ details: { is_current: false, retry_ready: true, finished: true, declaration_source: 'PM' } })], review)],
    ];
    setTransport(fakeTransport({
      '/jobs': { items: [] }, '/workspace': workspace,
      ...Object.fromEntries(cases.map(([, task]) => [`/resources/task/${task.task_run_id}`, read('task', task.task_run_id, task)])),
    }));
    await jobs.init();
    for (const [why, task] of cases) {
      const view = render(<TaskPage id={task.task_run_id} />);
      await screen.findByRole('heading', { name: '测试' });
      await screen.findByText(/实施 1 · 测试 1/);
      expect(screen.queryByRole('button', { name: '按原环境重新测试' }), why).toBeNull();
      view.unmount();
    }
  });

  it('depends on the readiness of the current test alone, whatever the next step is and whether or not it was reviewed', async () => {
    const post = vi.fn(() => ({ job_id: 'job-t', state: 'queued' }));
    const round = { round_id: 'r1', snapshot_id: 's1', status: 'ready_for_assessment', seats: [], assessment: null, implementation_attempt: 1, test_attempt: 1, metadata: { kind: 'review', created_at: 't' } };
    const cases: [string, TaskView, boolean][] = [
      // Reviewed already: the next step is the rework or the user's decision, and names no test.
      ['a review asks for rework', taskOf('task-p', [failedTest()], { kind: 'rework', review_round_id: 'r1', detail: '' }, { reviews: [round] }), true],
      ['a review leaves the decision to the user', taskOf('task-q', [failedTest()], { kind: 'user_decision', detail: '' }, { reviews: [round] }), true],
      // A retest that was cancelled, or whose evidence did not verify: the record is the new current test.
      ['a cancelled retest', taskOf('task-r', [failedTest({ test_attempt: 2, phase: 'cancelled', verification: 'unverified', details: { is_current: true, retry_ready: true, finished: true, cancel_requested: true, declaration_source: 'PM' } })], { kind: 'reconciliation_required', test_attempt: 2, detail: '' }), false],
      ['the next step names another test', taskOf('task-s', [failedTest()], { ...review, test_attempt: 7 }), false],
    ];
    setTransport(fakeTransport({
      '/jobs': { items: [] }, '/workspace': workspace, '/jobs/job-t': { job_id: 'job-t', action: 'task.test', state: 'finished', exit_code: 0, result: {}, error: null, error_kind: null },
      ...Object.fromEntries(cases.map(([, task]) => [`/resources/task/${task.task_run_id}`, read('task', task.task_run_id, task)])),
    }, { '/jobs': post }));
    await jobs.init();
    for (const [why, task, reviewed] of cases) {
      const view = render(<TaskPage id={task.task_run_id} />);
      expect(await screen.findByRole('button', { name: '按原环境重新测试' }), why).toBeTruthy();
      // With a review on record the page says what becomes of it.
      expect(screen.queryByText('之前的审核会保留，它看的是上一次测试。重新测试通过后，要对新的测试结果重新送审。') !== null, why).toBe(reviewed);
      view.unmount();
    }
    // Submitted as the earlier test was: its implementation, its commands in order, its declaration.
    render(<TaskPage id="task-p" />);
    const button = await screen.findByRole('button', { name: '按原环境重新测试' }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(post).toHaveBeenCalledWith({
      action: 'task.test', args: { task_run_id: 'task-p', implementation_attempt: 1, commands: ['build', 'test'], declaration_source: 'PM' },
    }));
  });
});

it('says on the planning page that a repeated correction opened the planning that already existed', async () => {
  setTransport(fakeTransport({ '/jobs': { items: [] }, '/workspace': workspace, '/resources/plan/plan-1': read('plan', 'plan-1', plan) }));
  await jobs.init();
  const view = render(<TaskPlanPage id="plan-1" reused />);
  await screen.findByText('Update the checked output');
  expect(screen.getByRole('status').textContent).toContain('这是之前那次提交已经建好的规划，这一次没有重复创建。它还没开始或没有完成时，可以在本页点「重试规划」继续');
  view.unmount();
  render(<TaskPlanPage id="plan-1" />);
  await screen.findByText('Update the checked output');
  expect(screen.queryByText(/这一次没有重复创建/)).toBeNull();
});

