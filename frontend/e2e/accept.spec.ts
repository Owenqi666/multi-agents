import { expect, test } from '@playwright/test';
import type { IntegrationRecord } from '../src/api/types';
import { act, jobRequest, NetworkLog, OfflineEnv, readback } from './support';
import { claim, directStage, GOAL, goalToPlan, integrationReview, leavesToIntegration, reviewCandidate } from './flow';

test('accept：从目标走到接受，含一个直接阶段和一个分派阶段', async ({ page }) => {
  const env = await OfflineEnv.start('accept');
  const net = new NetworkLog(page);
  page.on('dialog', (d) => void d.accept());
  try {
    await goalToPlan(page, env.url, async () => {
      await page.getByRole('link', { name: /^顾问 1 ·/ }).last().click();
      await expect(page.locator('.log-body')).not.toBeEmpty();
      await page.goBack();
    });
    const planUrl = page.url();
    await page.locator('section').filter({ has: page.getByRole('heading', { name: '规划日志' }) }).getByRole('link').last().click();
    await expect(page.locator('.log-body')).not.toBeEmpty();
    await page.goBack();
    await claim(page, 'amount', 'direct');
    await directStage(page);
    await page.goto(planUrl);
    await claim(page, 'summary', 'delegate');
    await leavesToIntegration(page);
    await integrationReview(page);
    const integrationApi = `${env.url}/api/resources/integration/${page.url().split('/').at(-1)}`;
    const first = await readback<IntegrationRecord>(page, integrationApi);
    const firstRound = first.reviews.at(-1)!;
    expect(firstRound.assessment?.status).toBe('ready');
    await act(page, '重新测试');
    const retested = await readback<IntegrationRecord>(page, integrationApi);
    expect(retested.test?.attempt).toBe(2);
    expect(retested.reviews.map(({ round_id, source_current }) => [round_id, source_current])).toEqual([[firstRound.round_id, false]]);
    await expect(page.getByRole('button', { name: '接受', exact: true })).toBeDisabled();
    await expect(page.locator('.round-head a').first()).toHaveAttribute('href', `/reviews/${firstRound.round_id}`);
    const reviewRequest = jobRequest(page, 'integration.review');
    await reviewCandidate(page, [['claude-opus-5-5', 'low']]);
    expect((await reviewRequest).postDataJSON().args.test_attempt).toBe(2);
    const reviewed = await readback<IntegrationRecord>(page, integrationApi);
    const newRound = reviewed.reviews.at(-1)!;
    expect(reviewed.reviews.map((round) => round.test_attempt)).toEqual([1, 2]);
    const acceptRequest = jobRequest(page, 'integration.accept');
    await act(page, '接受');
    expect((await acceptRequest).postDataJSON().args.round_id).toBe(newRound.round_id);
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();
    const accepted = await readback<IntegrationRecord>(page, integrationApi);
    expect(accepted.acceptance?.review_round_id).toBe(newRound.round_id);

    const posts = net.posts().length;
    const here = page.url();
    await page.reload();
    await expect(page).toHaveURL(here);
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();
    await page.goBack();
    await page.goForward();
    await page.goto(`${env.url}/jobs`);
    await page.goto(`${env.url}/`);
    await expect(page.locator('svg.track')).toBeVisible();
    expect(net.posts().length, '打开、刷新和查看历史不新增作业').toBe(posts);
    await net.ready();
    expect(net.lockViolations(), '作业未结束期间没有抢锁读取').toEqual([]);
    await net.record('accept', {
      result: 'passed', same_page_after_refresh: here, registered_logs: ['consultation', 'project-plan'],
      retest: { historical_round: firstRound.round_id, historical_test: firstRound.test_attempt,
        current_round: newRound.round_id, current_test: newRound.test_attempt, accepted_round: accepted.acceptance?.review_round_id },
    });
  } catch (e) {
    await net.record('accept', { result: 'failed', error: String(e) });
    throw e;
  } finally {
    await env.stop();
  }
});

test('goal-only：直接确认后的首页显示目标并可进入项目生成计划', async ({ page }) => {
  const env = await OfflineEnv.start('accept');
  const net = new NetworkLog(page);
  page.on('dialog', (d) => void d.accept());
  try {
    await page.goto(`${env.url}/new`);
    await page.getByLabel('原始目标').fill(GOAL);
    await act(page, '直接确认为目标');
    await expect(page).toHaveURL(/\/projects\//);
    const project = page.url();
    await page.goto(env.url);
    await expect(page.locator('.home-card .goal-text')).toHaveText(GOAL.trim());
    await expect(page.getByText('正在读取运行状态…')).toHaveCount(0);
    await page.getByRole('link', { name: '前往项目生成计划' }).click();
    await expect(page).toHaveURL(project);
    await expect(page.getByRole('button', { name: '生成计划' })).toBeVisible();
    await net.record('goal-only', { result: 'passed', project, goal: GOAL });
  } finally {
    await env.stop();
  }
});
