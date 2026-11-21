import { expect, test, type Page } from '@playwright/test';
import type { RoleRequest } from '../src/api/types';
import { act, jobRequest, NetworkLog, OfflineEnv, pick, readback, refreshEnvironments } from './support';

const SOL = { provider: 'codex', model: 'gpt-6.2-sol', effort: 'high' };
const TERRA = { provider: 'codex', model: 'gpt-6.1-terra', effort: 'low' };
const SONNET = { provider: 'claude-code', model: 'claude-sonnet-5-5', effort: 'high' };

interface Attempt {
  request: {
    worktree: string;
    decision: {
      requested: RoleRequest | null;
      choice: RoleRequest;
      reason: string;
      selection_history: { choice: RoleRequest; outcome: string }[];
    };
  };
  runtime: { reported_model: string | null; reported_effort: string | null; failure_category: string | null };
  events: {
    kind: string;
    dispatch_policy?: { requested: RoleRequest | null; fallbacks: RoleRequest[] };
  }[];
  usage_evidence: { source: string; counts: Record<string, number> } | null;
  runtime_reroutes: { source: string; from_model: string; to_model: string; reason: string }[] | null;
}

interface TaskReadback {
  phase: string;
  verification: string;
  candidate: string | null;
  attempts: Attempt[];
}

async function openPlan(page: Page, origin: string, id: string) {
  await page.getByRole('link', { name: '单卡', exact: true }).click();
  await page.getByLabel('规划运行 ID', { exact: true }).fill(id);
  await page.getByRole('button', { name: '打开规划', exact: true }).click();
  await expect(page).toHaveURL(origin + '/task-plans/' + id);
}

async function taskReadback(page: Page, url: string) {
  return readback<TaskReadback>(page, url.replace('/tasks/', '/api/resources/task/') + '?implementation=1');
}

function preparation(attempt: Attempt) {
  return attempt.events.find((event) => event.kind === 'task_execution_prepared')?.dispatch_policy;
}

function attemptView(page: Page, number: number) {
  return page.getByRole('list', { name: '实施尝试' }).locator(':scope > li').filter({
    has: page.getByText(`第 ${number} 次实施`, { exact: true }),
  });
}

test('独立单卡冻结用户排序的备选链，重试改派后可读回每次实施证据', async ({ page }) => {
  const env = await OfflineEnv.start('task-evidence');
  const net = new NetworkLog(page);
  try {
    const plans = env.standalonePlans!;
    await page.goto(env.url);
    await openPlan(page, env.url, plans.codex);
    await page.getByLabel('指定实施模型', { exact: true }).check();
    await refreshEnvironments(page);
    await pick(page, '实施模型', SOL.model, SOL.effort);
    const add = page.getByRole('button', { name: '添加备选', exact: true });
    await add.click();
    await pick(page, '备选 1', TERRA.model, TERRA.effort);
    await add.click();
    await pick(page, '备选 2', SONNET.model, SONNET.effort);
    await add.click();
    await pick(page, '备选 3', SOL.model, 'low');
    await page.getByRole('button', { name: '备选 2 上移', exact: true }).click();
    await page.getByRole('button', { name: '删除备选 3', exact: true }).click();
    await expect(page.getByLabel('备选 1：模型', { exact: true })).toHaveValue(SONNET.model);
    await expect(page.getByLabel('备选 2：模型', { exact: true })).toHaveValue(TERRA.model);
    await expect(page.getByLabel('备选 3：模型', { exact: true })).toHaveCount(0);

    const firstRequest = jobRequest(page, 'task.run');
    await act(page, '开始实施');
    expect((await firstRequest).postDataJSON().args).toEqual({
      planning_run_id: plans.codex,
      implementation: SOL,
      fallbacks: [SONNET, TERRA],
    });
    await expect(page).toHaveURL(/\/tasks\//);
    const codexTask = page.url();
    const failed = await taskReadback(page, codexTask);
    expect(failed.phase).toBe('failed');
    expect(failed.attempts).toHaveLength(1);
    expect(preparation(failed.attempts[0]!)).toMatchObject({ requested: SOL, fallbacks: [SONNET, TERRA] });
    expect(failed.attempts[0]!.runtime.failure_category).toBe('model_unavailable');
    expect(failed.attempts[0]!.usage_evidence).toBeNull();
    expect(failed.attempts[0]!.runtime_reroutes).toBeNull();
    await expect(attemptView(page, 1)).toContainText(failed.attempts[0]!.request.worktree);
    await expect(attemptView(page, 1)).toContainText('未确认');
    await expect(page.getByRole('button', { name: '添加备选', exact: true })).toHaveCount(0);

    const retryRequest = jobRequest(page, 'task.retry');
    await act(page, '重试实施');
    expect((await retryRequest).postDataJSON().args).toEqual({ planning_run_id: plans.codex });
    await expect(page).toHaveURL(codexTask);
    const completed = await taskReadback(page, codexTask);
    expect(completed).toMatchObject({ phase: 'awaiting_delivery', verification: 'verified' });
    expect(completed.candidate).toMatch(/^[a-f0-9]{40}$/);
    expect(completed.attempts).toHaveLength(2);
    const first = completed.attempts[0]!;
    const second = completed.attempts[1]!;
    expect(first).toEqual(failed.attempts[0]);
    expect(second.request.worktree).not.toBe(first.request.worktree);
    expect(preparation(second)).toEqual(preparation(first));
    expect(second.request.decision).toMatchObject({
      requested: SOL,
      choice: TERRA,
      reason: 'enabled_ordered_fallback',
      selection_history: [
        { choice: SOL, outcome: 'known_unavailable' },
        { choice: SONNET, outcome: 'disallowed_by_rules' },
        { choice: TERRA, outcome: 'selected_request' },
      ],
    });
    expect(second.runtime).toMatchObject({ reported_model: null, reported_effort: null });
    expect(second.usage_evidence).toMatchObject({
      source: 'thread/tokenUsage/updated',
      counts: { 'total.inputTokens': 31, 'total.totalTokens': 44, 'last.totalTokens': 27 },
    });
    expect(second.runtime_reroutes).toEqual([
      { source: 'model/rerouted', from_model: 'first', to_model: 'second', reason: 'highRiskCyberActivity' },
    ]);

    const postsBeforeReload = net.posts().length;
    await page.reload();
    await expect(page).toHaveURL(codexTask);
    for (const [number, worktree] of [first, second].map((item, index) => [index + 1, item.request.worktree] as const)) {
      await expect(attemptView(page, number)).toContainText(worktree);
    }
    const visible = attemptView(page, 2);
    for (const text of ['请求', '工作台选择', '运行时报告', '冻结备选链', '用量', '运行时改派', SOL.model, TERRA.model, SONNET.model, '未确认', '已知不可用', '冻结规则禁止', '按冻结顺序选用备选', '44', 'first', 'second', 'highRiskCyberActivity']) {
      await expect(visible).toContainText(text);
    }
    expect(net.posts().length).toBe(postsBeforeReload);
    expect(await taskReadback(page, codexTask)).toEqual(completed);

    await openPlan(page, env.url, plans.claude);
    const defaultRequest = jobRequest(page, 'task.run');
    await act(page, '开始实施');
    const defaultArgs = (await defaultRequest).postDataJSON().args;
    expect(defaultArgs.planning_run_id).toBe(plans.claude);
    expect(defaultArgs.implementation).toBeUndefined();
    expect(defaultArgs.fallbacks ?? []).toEqual([]);
    await expect(page).toHaveURL(/\/tasks\//);
    const claudeTask = await taskReadback(page, page.url());
    expect(claudeTask).toMatchObject({ phase: 'awaiting_delivery', verification: 'verified' });
    expect(claudeTask.candidate).toMatch(/^[a-f0-9]{40}$/);
    const claude = claudeTask.attempts[0]!;
    expect(claude.request.decision).toMatchObject({
      requested: null,
      choice: { ...SONNET, effort: 'low' },
    });
    expect(claude.runtime).toMatchObject({ reported_model: 'actual-claude', reported_effort: null });
    expect(claude.usage_evidence).toMatchObject({
      source: 'result',
      counts: { input_tokens: 11, output_tokens: 7, client_estimated_cost_usd: 0.04 },
    });
    await expect(attemptView(page, 1)).toContainText('Claude 客户端估算（USD）');
    await expect(attemptView(page, 1)).toContainText('0.04');
    await expect(attemptView(page, 1)).toContainText('actual-claude');

    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    if (process.env.WORKBENCH_E2E_EVIDENCE) await net.record('task-evidence', {
      result: 'passed',
      verified_ready_plans: plans,
      frozen_fallbacks: preparation(second)?.fallbacks,
      fallback_selection: second.request.decision,
      codex: { candidate: completed.candidate, worktree: second.request.worktree, usage: second.usage_evidence, reroutes: second.runtime_reroutes },
      claude: { candidate: claudeTask.candidate, worktree: claude.request.worktree, usage: claude.usage_evidence },
    });
  } catch (error) {
    if (process.env.WORKBENCH_E2E_EVIDENCE) await net.record('task-evidence', { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});
