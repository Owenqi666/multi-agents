import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { setTransport } from '../api/client';
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

  expect(await screen.findByText('冻结的规划预算已用完。')).toBeTruthy();
  expect(screen.queryByRole('button', { name: '重试规划' })).toBeNull();
});
