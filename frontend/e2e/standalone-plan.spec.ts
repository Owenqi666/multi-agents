import { expect, test, type Page } from '@playwright/test';
import type { PlanningView, TaskView, Workspace } from '../src/api/types';
import { act, jobRequest, NetworkLog, OfflineEnv, pick, readback, refreshEnvironments } from './support';

const AMOUNT = 'Implement the standalone amount.\n';
const QUESTION_REQUIREMENTS = 'Implement the amount after the reporting cutoff decision.\n';
const QUESTION = 'Which reporting cutoff should the amount use?';
const ANSWER = 'Use 16:30 London time.';
const DRAFT = { model: 'claude-opus-5-5', effort: 'low' };
const REVIEW = { model: 'gpt-6.2-sol', effort: 'low' };
const FALLBACKS = [
  { provider: 'codex', model: 'gpt-6.1-terra', effort: 'low' },
  { provider: 'claude-code', model: 'claude-sonnet-5-5', effort: 'high' },
];

interface Decision {
  question: string;
  resolution: string;
  source: 'user_declared';
  detail: string;
}

interface PlanRecord extends Omit<PlanningView, 'details'> {
  details: {
    input: {
      requirements: string;
      frozen_rules: { baseline_commit: string };
      draft_request: { provider: string; model: string; effort: string };
      review_request: { provider: string; model: string; effort: string };
      dependencies: { task_id: string; baseline_commit: string; source: string; detail: string }[];
      decisions: Decision[];
    };
  };
  questions: { question: string; blocks_dispatch: boolean; resolution?: string | null }[];
}

async function baselineOf(page: Page, origin: string): Promise<string> {
  const response = await page.request.get(`${origin}/api/workspace`);
  expect(response.ok()).toBe(true);
  const workspace = await response.json() as Workspace;
  expect(workspace.head).toMatch(/^[a-f0-9]{40}$/);
  return workspace.head!;
}

async function fillPlan(page: Page, baseline: string, requirements: string) {
  await page.getByRole('link', { name: '单卡', exact: true }).click();
  await page.getByRole('link', { name: '新建单卡规划', exact: true }).click();
  await page.getByLabel('基线提交', { exact: true }).fill(baseline);
  await page.getByLabel('单卡需求', { exact: true }).fill(requirements);
  await refreshEnvironments(page);
  await selectPlanningModels(page);
}

async function selectPlanningModels(page: Page) {
  await page.getByLabel('指定草拟模型', { exact: true }).check();
  await page.getByLabel('指定复核模型', { exact: true }).check();
  await pick(page, '草拟模型', DRAFT.model, DRAFT.effort);
  await pick(page, '规划复核', REVIEW.model, REVIEW.effort);
}

async function fillDecisionAnswer(page: Page, decision: Decision) {
  await page.getByLabel('回答 1', { exact: true }).fill(decision.resolution);
  await page.getByLabel('决定说明 1', { exact: true }).fill(decision.detail);
}

async function planAt(page: Page, url: string) {
  return readback<PlanRecord>(page, url.replace('/task-plans/', '/api/resources/plan/') + '?implementation=1');
}

async function submitPlan(page: Page) {
  const request = jobRequest(page, 'plan.create');
  await act(page, '开始单卡规划');
  await expect(page).toHaveURL(/\/task-plans\/[a-f0-9]{32}$/);
  const url = page.url();
  return { args: (await request).postDataJSON().args, url, plan: await planAt(page, url) };
}

async function record(net: NetworkLog, scenario: string, evidence: Record<string, unknown>) {
  if (process.env.WORKBENCH_E2E_EVIDENCE) await net.record(scenario, evidence);
}

test('新建单卡可重试失败，沿用冻结输入复核并从同一个规划实施', async ({ page }) => {
  const env = await OfflineEnv.start('standalone-plan');
  const net = new NetworkLog(page);
  try {
    await page.goto(env.url);
    const baseline = await baselineOf(page, env.url);
    const dependency = {
      task_id: 'T-0199',
      baseline_commit: baseline,
      source: 'user_declared',
      detail: 'The amount input is already available on this baseline.',
    };
    const decision: Decision = {
      question: QUESTION,
      resolution: ANSWER,
      source: 'user_declared',
      detail: 'The PM confirmed the regional reporting time.',
    };
    const declarations = { dependencies: [dependency], decisions: [decision] };
    await fillPlan(page, baseline, AMOUNT);
    await page.getByText('冻结声明（可选）', { exact: true }).click();
    await page.getByRole('button', { name: '添加依赖声明', exact: true }).click();
    await page.getByLabel('依赖任务 ID 1', { exact: true }).fill(dependency.task_id);
    await page.getByLabel('依赖基线 SHA 1', { exact: true }).fill(dependency.baseline_commit);
    await page.getByLabel('依赖说明 1', { exact: true }).fill(dependency.detail);
    await page.getByRole('button', { name: '添加用户决定', exact: true }).click();
    await page.getByLabel('问题 1', { exact: true }).fill(decision.question);
    await fillDecisionAnswer(page, decision);

    const { args, url, plan: failed } = await submitPlan(page);
    expect(args).toEqual({ baseline, requirements: AMOUNT, draft: DRAFT, plan_review: REVIEW, declarations });
    expect(failed).toMatchObject({
      phase: 'failed',
      task_run_id: null,
      next_action: { kind: 'plan_retry' },
      details: {
        input: {
          requirements: AMOUNT,
          frozen_rules: { baseline_commit: baseline },
          draft_request: { provider: 'claude', ...DRAFT },
          review_request: { provider: 'codex', ...REVIEW },
          ...declarations,
        },
      },
    });
    expect(failed.attempts.draft).toHaveLength(1);
    expect(failed.attempts.draft[0]!.runtime).toMatchObject({ number: 1, status: 'failed', failure_category: 'model_unavailable' });
    expect(failed.attempts.review).toHaveLength(0);
    await expect(page.getByRole('button', { name: '开始实施', exact: true })).toHaveCount(0);

    const retry = jobRequest(page, 'plan.retry');
    await act(page, '重试规划');
    expect((await retry).postDataJSON().args).toEqual({ planning_run_id: failed.planning_run_id });
    await expect(page).toHaveURL(url);
    const ready = await planAt(page, url);
    expect(ready).toMatchObject({
      planning_run_id: failed.planning_run_id,
      phase: 'ready',
      verification: 'verified',
      dispatch_configurable: true,
      card: { depends_on: [dependency.task_id] },
    });
    expect(ready.details.input).toEqual(failed.details.input);
    expect(ready.attempts.draft).toHaveLength(2);
    expect(ready.attempts.draft[0]).toEqual(failed.attempts.draft[0]);
    expect(ready.attempts.draft[1]).toMatchObject({
      request: { request: { provider: 'claude', ...DRAFT } },
      runtime: { number: 2, status: 'succeeded' },
    });
    expect(ready.attempts.review).toHaveLength(1);
    expect(ready.attempts.review[0]).toMatchObject({
      request: { request: { provider: 'codex', ...REVIEW } },
      runtime: { number: 1, status: 'succeeded' },
    });

    for (const [index, choice] of FALLBACKS.entries()) {
      await page.getByRole('button', { name: '添加备选', exact: true }).click();
      await pick(page, `备选 ${index + 1}`, choice.model, choice.effort);
    }
    const implementation = jobRequest(page, 'task.run');
    await act(page, '开始实施');
    expect((await implementation).postDataJSON().args).toEqual({
      planning_run_id: failed.planning_run_id,
      fallbacks: FALLBACKS,
    });
    await expect(page).toHaveURL(/\/tasks\//);
    const task = await readback<TaskView>(page, page.url().replace('/tasks/', '/api/resources/task/') + '?implementation=1');
    expect(task.planning_run_id).toBe(failed.planning_run_id);
    expect(task.verification).toBe('verified');
    expect(task.candidate).toMatch(/^[a-f0-9]{40}$/);
    expect(task.attempts[0]!.events?.find((event) => event.kind === 'task_execution_prepared')?.dispatch_policy?.fallbacks).toEqual(FALLBACKS);

    const posts = net.posts().length;
    await page.getByRole('link', { name: '单卡', exact: true }).click();
    await page.reload();
    await page.getByRole('list', { name: '规划历史' }).locator(`a[href="/task-plans/${failed.planning_run_id}"]`).click();
    await expect(page).toHaveURL(url);
    await expect(page.getByRole('link', { name: '实施详情', exact: true })).toBeVisible();
    const reopened = await planAt(page, url);
    expect(reopened.task_run_id).toBe(task.task_run_id);
    expect(reopened.details.input).toEqual(failed.details.input);
    expect(reopened.attempts).toEqual(ready.attempts);
    expect(net.posts().length).toBe(posts);

    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await record(net, 'standalone-plan', {
      result: 'passed',
      planning_run_id: reopened.planning_run_id,
      task_run_id: task.task_run_id,
      baseline,
      drafts: reopened.attempts.draft.map((attempt) => attempt.runtime),
      review: reopened.attempts.review[0]!.request.request,
      declarations,
      fallbacks: FALLBACKS,
      candidate: task.candidate,
    });
  } catch (error) {
    await record(net, 'standalone-plan', { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});

test('未回答的问题阻止实施，提交用户回答时新建冻结规划', async ({ page }) => {
  const env = await OfflineEnv.start('standalone-plan');
  const net = new NetworkLog(page);
  try {
    await page.goto(env.url);
    const baseline = await baselineOf(page, env.url);
    await fillPlan(page, baseline, QUESTION_REQUIREMENTS);
    const blocked = await submitPlan(page);
    expect(blocked.plan).toMatchObject({
      phase: 'revision_required',
      task_run_id: null,
      dispatch_configurable: false,
    });
    expect(blocked.plan.questions).toMatchObject([{ question: QUESTION, blocks_dispatch: true }]);
    expect(blocked.plan.questions[0]!.resolution ?? null).toBeNull();
    await expect(page.getByText(QUESTION, { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '开始实施', exact: true })).toHaveCount(0);

    await page.getByRole('link', { name: '回答后新建规划', exact: true }).click();
    await expect(page).toHaveURL(`${env.url}/task-plans/new?from=${blocked.plan.planning_run_id}&question=0`);
    await expect(page.getByLabel('基线提交', { exact: true })).toHaveValue(baseline);
    await expect(page.getByLabel('单卡需求', { exact: true })).toHaveValue(QUESTION_REQUIREMENTS);
    await expect(page.getByLabel('问题 1', { exact: true })).toHaveValue(QUESTION);
    await expect(page.getByRole('button', { name: '开始单卡规划', exact: true })).toBeDisabled();
    await expect(page.getByLabel('草拟模型：模型', { exact: true })).toHaveValue(DRAFT.model);
    await expect(page.getByLabel('草拟模型：档位', { exact: true })).toHaveValue(DRAFT.effort);
    await expect(page.getByLabel('规划复核：模型', { exact: true })).toHaveValue(REVIEW.model);
    await expect(page.getByLabel('规划复核：档位', { exact: true })).toHaveValue(REVIEW.effort);
    const decision: Decision = {
      question: QUESTION,
      resolution: ANSWER,
      source: 'user_declared',
      detail: 'The user answered the blocking question in the new plan.',
    };
    await fillDecisionAnswer(page, decision);
    const answered = await submitPlan(page);
    expect(answered.args).toEqual({
      baseline,
      requirements: QUESTION_REQUIREMENTS,
      draft: DRAFT,
      plan_review: REVIEW,
      declarations: { dependencies: [], decisions: [decision] },
    });
    expect(answered.plan.planning_run_id).not.toBe(blocked.plan.planning_run_id);
    expect(answered.plan).toMatchObject({
      phase: 'ready',
      verification: 'verified',
      dispatch_configurable: true,
      details: { input: { requirements: QUESTION_REQUIREMENTS, decisions: [decision] } },
      questions: [{ question: QUESTION, blocks_dispatch: true, resolution: ANSWER }],
    });
    await expect(page.getByRole('button', { name: '开始实施', exact: true })).toBeEnabled();
    expect(await planAt(page, blocked.url)).toEqual(blocked.plan);

    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await record(net, 'standalone-question', {
      result: 'passed',
      blocked_planning_run_id: blocked.plan.planning_run_id,
      answered_planning_run_id: answered.plan.planning_run_id,
      decision,
    });
  } catch (error) {
    await record(net, 'standalone-question', { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});
