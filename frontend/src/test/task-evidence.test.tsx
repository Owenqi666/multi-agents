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
      runtime: { number: 2, status: 'succeeded', reported_model: null, reported_effort: null },
      events: [{ kind: 'task_execution_prepared', dispatch_policy: { fallbacks: [sonnet, terra] } }],
      usage_evidence: { source: 'thread/tokenUsage/updated', counts: { 'total.inputTokens': 36, 'last.inputTokens': 9, 'last.outputTokens': 0 } },
      runtime_reroutes: [{ source: 'model/rerouted', from_model: 'original', to_model: 'rerouted', reason: 'highRiskCyberActivity' }],
    },
    {
      request: { kind: 'task_rework_started', decision: { choice: sonnet, requested: null }, started_at: null },
      runtime: { number: 3, status: 'succeeded', reported_model: 'claude-reported', reported_effort: null },
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
});
