import { expect, test, type Page } from '@playwright/test';
import type { DelegateStage, IntegrationRecord, PlanningView, ReviewRound, TaskView } from '../src/api/types';
import { claim, directStage, goalToPlan, integrationReview, reviewCandidate } from './flow';
import { act, jobRequest, NetworkLog, OfflineEnv, pick, readback } from './support';

const DUAL: [string, string][] = [['claude-opus-5-5', 'low'], ['gpt-6.2-astra', 'low']];
const END = 'END OF OFFLINE DIAGNOSTIC';
const OVERRIDE_REVIEW = { provider: 'claude', model: 'claude-fable-5-2', effort: 'low' };
const INHERITED_REVIEW = { provider: 'claude', model: 'claude-opus-5-5', effort: 'low' };

async function continueConfirmation(page: Page, base: string, parent: ReviewRound, action: string, onRoundPage: boolean) {
  const id = parent.confirmation_round_id!;
  expect(id).toBeTruthy();
  const url = `${base}/api/resources/review/${id}`;
  const failed = await readback<ReviewRound>(page, url);
  const failedSeats = failed.seats.filter((s) => s.attempts.at(-1)?.status === 'failed').map((s) => s.key);
  expect(failedSeats).toHaveLength(1);
  const name = `补跑失败的席位（${failedSeats[0]}）`;
  await expect(page.getByRole('button', { name })).toBeEnabled();
  if (onRoundPage) {
    await page.locator(`.round-head a[href="/reviews/${id}"]`).click();
    await expect(page).toHaveURL(new RegExp(`/reviews/${id}$`));
  }
  const submitted = jobRequest(page, action);
  await act(page, name);
  expect((await submitted).postDataJSON().args).toMatchObject({ round_id: id, seat_keys: failedSeats });
  const repaired = await readback<ReviewRound>(page, url);
  for (const seat of repaired.seats) {
    expect(seat.attempts.at(-1)?.status).toBe('succeeded');
    expect(seat.attempts).toHaveLength(failedSeats.includes(seat.key) ? 2 : 1);
  }
  return { round_id: id, seats: repaired.seats.map((s) => ({ key: s.key, attempts: s.attempts.length })) };
}

async function checkLongLog(page: Page, name: string) {
  await page.getByRole('link', { name, exact: true }).click();
  await expect(page.locator('.log-body')).toContainText('offline diagnostic');
  await expect(page.locator('.log-body')).not.toContainText(END);
  const next = page.waitForResponse((r) => new URL(r.url()).searchParams.get('offset') === '8192');
  await page.getByRole('button', { name: '读下一页' }).click();
  expect((await next).ok()).toBe(true);
  await expect(page.locator('.log-body')).toContainText(END);
  await page.goBack();
}

test('review-fixes：上游返工后重新冻结下游子卡，并补跑任务和集成交叉确认', async ({ page }) => {
  const env = await OfflineEnv.start('review-fixes');
  const net = new NetworkLog(page);
  page.on('dialog', (d) => void d.accept());
  try {
    await goalToPlan(page, env.url);
    const planUrl = page.url();
    await claim(page, 'amount', 'direct');
    await directStage(page);
    await page.goto(planUrl);
    await claim(page, 'summary', 'delegate');
    const stageUrl = page.url();
    const stageApi = `${env.url}/api/resources/stage/${stageUrl.split('/').at(-1)}`;
    const upstream = page.locator('.leaf').filter({ has: page.getByText('T-0800', { exact: true }) });
    const downstream = page.locator('.leaf').filter({ has: page.getByText('T-0801', { exact: true }) });
    await expect(downstream.getByRole('button', { name: '领取子卡' })).toBeDisabled();
    const chooseReview = '为此子卡指定复核模型';
    await pick(upstream, '执行者', 'claude-sonnet-5-5', 'low');
    await upstream.getByLabel(chooseReview, { exact: true }).check();
    await pick(upstream, '子卡复核', OVERRIDE_REVIEW.model, OVERRIDE_REVIEW.effort);
    const claimUpstream = jobRequest(page, 'project.stage.leaf');
    await act(page, '领取子卡', upstream);
    expect((await claimUpstream).postDataJSON().args).toMatchObject({ card_id: 'T-0800', card_review: OVERRIDE_REVIEW });
    await act(page, '实施', upstream);
    await pick(downstream, '执行者', 'claude-sonnet-5-5', 'low');
    await expect(downstream.getByLabel(chooseReview, { exact: true })).not.toBeChecked();
    const claimDownstream = jobRequest(page, 'project.stage.leaf');
    await act(page, '领取子卡', downstream);
    const downstreamArgs = (await claimDownstream).postDataJSON().args;
    expect(downstreamArgs.card_id).toBe('T-0801');
    expect(downstreamArgs).not.toHaveProperty('card_review');
    await expect(downstream.getByRole('button', { name: '实施', exact: true })).toBeEnabled();
    const initial = await readback<DelegateStage>(page, stageApi);
    const oldPlan = initial.leaves[1]!.planning_run_id!;
    const oldCandidate = initial.leaves[0]!.candidate!;
    expect(initial.leaves[1]!.planning?.card?.baseline_commit).toBe(oldCandidate);
    const leafReviewers = [];
    for (const [index, requested] of [OVERRIDE_REVIEW, INHERITED_REVIEW].entries()) {
      const leaf = initial.leaves[index]!;
      const frozen = await readback<PlanningView>(page, `${env.url}/api/resources/plan/${leaf.planning_run_id}?implementation=1`);
      expect(frozen.details?.input?.review_request).toEqual(requested);
      expect(frozen.attempts.review[0]!.request.request).toEqual(requested);
      leafReviewers.push({ card_id: leaf.card_id, requested });
    }

    await upstream.getByRole('link', { name: '实施详情' }).click();
    const taskApi = `${env.url}/api/resources/task/${page.url().split('/').at(-1)}`;
    await act(page, '运行测试');
    await reviewCandidate(page, DUAL);
    await act(page, '请两席交叉确认');
    const task = await readback<TaskView>(page, taskApi);
    const taskConfirmation = await continueConfirmation(page, env.url, task.reviews.find((r) => r.confirmation_round_id)!, 'task.retry_review', true);
    await page.getByRole('link', { name: '返回实施和审核' }).click();
    await act(page, '按阻塞意见返工');
    await page.goto(stageUrl);
    await expect(downstream.getByRole('button', { name: '实施', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '合并子卡' })).toBeDisabled();
    const stale = await readback<DelegateStage>(page, stageApi);
    const newCandidate = stale.leaves[0]!.candidate!;
    expect(newCandidate).not.toBe(oldCandidate);
    expect(stale.leaves[1]!.review_ready).toBe(false);
    const submitRefreeze = jobRequest(page, 'project.stage.leaf');
    await act(page, '重新冻结并复核', downstream);
    expect((await submitRefreeze).postDataJSON().args).toMatchObject({ card_id: 'T-0801', refreeze: true, review_only: true });
    const refrozen = await readback<DelegateStage>(page, stageApi);
    expect(refrozen.leaves[1]!.planning_run_id).not.toBe(oldPlan);
    expect(refrozen.leaves[1]!.planning?.card?.baseline_commit).toBe(newCandidate);
    expect(refrozen.leaves[1]!.review_ready).toBe(true);
    expect(refrozen.leaves[1]!.prior_planning_run_ids).toContain(oldPlan);
    await downstream.getByText(/^原规划/).click();
    await downstream.getByText('完整规划记录').click();
    await expect(downstream.locator('.raw pre')).toContainText(oldCandidate);
    await act(page, '实施', downstream);
    await expect(page.getByRole('button', { name: '合并子卡' })).toBeEnabled();
    expect((await readback<DelegateStage>(page, stageApi)).can_integrate).toBe(true);
    await act(page, '合并子卡');
    await expect(page).toHaveURL(/\/integrations\//);

    await integrationReview(page, DUAL);
    const integrationUrl = page.url();
    const integrationApi = `${env.url}/api/resources/integration/${integrationUrl.split('/').at(-1)}`;
    await checkLongLog(page, '测试 1 · 命令 2 · stdout');
    await page.locator('.round-head a').first().click();
    await checkLongLog(page, '冻结日志 · 命令 2 · stdout');
    await page.goto(integrationUrl);
    await act(page, '请两席交叉确认');
    const parent = await readback<IntegrationRecord>(page, integrationApi);
    const integrationConfirmation = await continueConfirmation(page, env.url, parent.reviews.find((r) => r.confirmation_round_id)!, 'integration.retry_review', false);
    await act(page, '按阻塞意见返工（每条谱系一次）');
    await expect(page).not.toHaveURL(integrationUrl);
    await expect(page.getByRole('button', { name: '接受', exact: true })).toBeEnabled();
    await act(page, '接受');
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();

    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await net.record('review-fixes', {
      result: 'passed', stale_baseline: oldCandidate, refrozen_baseline: newCandidate,
      prior_plan: oldPlan, refrozen_plan: refrozen.leaves[1]!.planning_run_id, can_integrate: true,
      task_confirmation: taskConfirmation, integration_confirmation: integrationConfirmation,
      leaf_reviewers: leafReviewers,
      registered_logs_paged: ['integration', 'frozen review input'], final_record: page.url(),
    });
  } catch (e) {
    await net.record('review-fixes', { result: 'failed', error: String(e) });
    throw e;
  } finally {
    await env.stop();
  }
});
