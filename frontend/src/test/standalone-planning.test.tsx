import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { setTransport } from '../api/client';
import { jobs } from '../api/jobs';
import { ApiError } from '../api/transport';
import type { PlanningDecision, PlanningDependency, PlanningView, Workspace } from '../api/types';
import { App } from '../app/App';
import { fakeTransport } from './fake';

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/');
});

it('copies frozen inputs and available requests when a blocking question is answered in a new plan', async () => {
  const baseline = 'a'.repeat(40);
  const requirements = 'Add the export.';
  const draft = { provider: 'claude', model: 'claude-opus-5-5', effort: 'low' };
  const review = { provider: 'codex', model: 'gpt-6.2-sol', effort: 'xhigh' };
  const dependency: PlanningDependency = { task_id: 'T-0180', baseline_commit: baseline, detail: 'The data is available.', source: 'user_declared' };
  const decision: PlanningDecision = { question: 'Which formats?', resolution: 'CSV', detail: 'User confirmed.', source: 'user_declared' };
  const question = 'What is the time zone?';
  const workspace: Workspace = {
    repository: '/repo', target_branch: 'main', head: 'b'.repeat(40), read_only: false,
    environments: { claude: null, codex: null },
    presets: { source: 'test', rules: {}, roles: [{
      id: 'review', label: 'Review', families: ['Opus', 'Sol'], candidates: [
        { family: 'Opus', provider: draft.provider, model: draft.model, efforts: ['low', 'high'] },
        { family: 'Sol', provider: review.provider, model: review.model, efforts: ['high', 'xhigh'] },
      ],
    }] },
  };
  const plan: PlanningView = {
    planning_run_id: 'original', task_run_id: null, phase: 'revision_required',
    verification: 'verified', card: null, attempts: { draft: [], review: [] }, next_action: null,
    standalone: true, questions: [{ question, blocks_dispatch: true }],
    details: { input: { requirements, frozen_rules: { baseline_commit: baseline }, draft_request: draft, review_request: review, dependencies: [dependency], decisions: [decision] } },
  };
  const post = vi.fn(() => ({ job_id: 'created', state: 'queued' }));
  setTransport(fakeTransport({
    '/workspace': workspace, '/jobs': { items: [] },
    '/resources/plan/original': { kind: 'plan', id: 'original', data: plan, artifacts: [] },
  }, { '/jobs': post }));
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
  window.history.replaceState(null, '', '/task-plans/original');
  render(<App />);

  fireEvent.click(await screen.findByRole('link', { name: '回答后新建规划' }));
  expect((await screen.findByLabelText<HTMLInputElement>('基线提交')).value).toBe(baseline);
  expect(screen.getByLabelText<HTMLTextAreaElement>('单卡需求').value).toBe(requirements);
  expect(screen.getByLabelText<HTMLSelectElement>('草拟模型：模型').value).toBe(draft.model);
  expect(screen.getByLabelText<HTMLSelectElement>('草拟模型：档位').value).toBe(draft.effort);
  expect(screen.getByLabelText<HTMLSelectElement>('规划复核：模型').value).toBe(review.model);
  expect(screen.getByLabelText<HTMLSelectElement>('规划复核：档位').value).toBe(review.effort);
  expect(screen.getByLabelText<HTMLInputElement>('问题 2').value).toBe(question);
  fireEvent.change(screen.getByLabelText('回答 2'), { target: { value: 'UTC' } });
  fireEvent.change(screen.getByLabelText('决定说明 2'), { target: { value: 'Required for imports.' } });
  const submit = screen.getByRole<HTMLButtonElement>('button', { name: '开始单卡规划' });
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);
  await waitFor(() => expect(post).toHaveBeenCalledWith({
    action: 'plan.create', args: {
      baseline, requirements,
      draft: { model: draft.model, effort: draft.effort },
      plan_review: { model: review.model, effort: review.effort },
      declarations: {
        dependencies: [dependency],
        decisions: [decision, { question, resolution: 'UTC', detail: 'Required for imports.', source: 'user_declared' }],
      },
    },
  }));
});

it('explains an exhausted frozen planning budget without offering retry', async () => {
  const workspace: Workspace = {
    repository: '/repo', target_branch: 'main', head: 'a'.repeat(40), read_only: false,
    environments: { claude: null, codex: null },
    presets: { source: 'test', rules: {}, roles: [] },
  };
  const plan: PlanningView = {
    planning_run_id: 'exhausted', task_run_id: null, phase: 'failed', verification: 'unverified',
    card: null, attempts: { draft: [], review: [] }, standalone: true, retry_configurable: false,
    next_action: { kind: 'budget_exhausted', detail: 'The original planning budget is exhausted.' },
  };
  setTransport(fakeTransport({
    '/workspace': workspace, '/jobs': { items: [] },
    '/resources/plan/exhausted': { kind: 'plan', id: 'exhausted', data: plan, artifacts: [] },
  }));
  window.history.replaceState(null, '', '/task-plans/exhausted');
  render(<App />);

  expect(await screen.findByText(/冻结的规划预算已用完。相同的输入不会重置预算/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: '重试规划' })).toBeNull();
});

it('starts a linked correction that never began from its own page, and says so when the service refuses the retry', async () => {
  const workspace: Workspace = {
    repository: '/repo', target_branch: 'main', head: 'a'.repeat(40), read_only: false,
    environments: { claude: null, codex: null },
    presets: { source: 'test', rules: {}, roles: [] },
  };
  // Created and linked, then stopped before its first draft: the service offers the retry.
  const pending: PlanningView = {
    planning_run_id: 'unstarted', task_run_id: null, phase: 'pending', verification: 'unverified',
    card: null, attempts: { draft: [], review: [] }, standalone: true, retry_configurable: true,
    next_action: { kind: 'plan_retry', detail: "Start this linked correction's first draft using the existing planning run ID." },
  };
  let record = pending;
  let reads = 0;
  let answer: unknown = { job_id: 'job-retry', state: 'queued' };
  const post = vi.fn(() => answer);
  setTransport(fakeTransport({
    '/workspace': workspace, '/jobs': { items: [] },
    '/jobs/job-retry': { job_id: 'job-retry', action: 'plan.retry', state: 'finished', exit_code: 0, result: {}, error: null, error_kind: null, resources: [{ kind: 'plan', id: 'unstarted' }] },
    '/resources/plan/unstarted': () => { reads += 1; return { kind: 'plan', id: 'unstarted', data: record, artifacts: [] }; },
  }, { '/jobs': post }));
  window.history.replaceState(null, '', '/task-plans/unstarted?reused=1');
  const view = render(<App />);
  expect(await screen.findByText(/这是之前那次提交已经建好的规划，这一次没有重复创建。它还没开始或没有完成时，可以在本页点「重试规划」继续/)).toBeTruthy();
  expect(screen.getByText('等待规划')).toBeTruthy();
  const retry = await screen.findByRole('button', { name: '重试规划' }) as HTMLButtonElement;
  await waitFor(() => expect(retry.disabled).toBe(false));
  fireEvent.click(retry);
  await waitFor(() => expect(post).toHaveBeenCalledWith({ action: 'plan.retry', args: { planning_run_id: 'unstarted' } }));
  await jobs.waitFor('job-retry');
  view.unmount();

  // The budget ran out since the page was read: the retry is refused with 409, the page says
  // what the service said and reads the planning again.
  jobs.reset();
  await jobs.init();
  answer = new ApiError(409, 'job_unavailable', 'The original planning budget is exhausted.');
  render(<App />);
  const again = await screen.findByRole('button', { name: '重试规划' }) as HTMLButtonElement;
  await waitFor(() => expect(again.disabled).toBe(false));
  record = { ...pending, phase: 'failed', retry_configurable: false, next_action: { kind: 'budget_exhausted', detail: 'The original planning budget is exhausted.' } };
  const before = reads;
  fireEvent.click(again);
  expect((await screen.findByRole('alert')).textContent).toContain('The original planning budget is exhausted.');
  await waitFor(() => expect(reads).toBeGreaterThan(before));
  expect(await screen.findByText(/冻结的规划预算已用完。相同的输入不会重置预算/)).toBeTruthy();
});
