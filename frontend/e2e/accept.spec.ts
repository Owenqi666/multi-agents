import { expect, test } from '@playwright/test';
import { act, NetworkLog, OfflineEnv } from './support';
import { claim, directStage, goalToPlan, integrationReview, leavesToIntegration } from './flow';

test('accept：从目标走到接受，含一个直接阶段和一个分派阶段', async ({ page }) => {
  const env = await OfflineEnv.start('accept');
  const net = new NetworkLog(page);
  page.on('dialog', (d) => void d.accept());
  try {
    await goalToPlan(page, env.url);
    const planUrl = page.url();
    await claim(page, 'amount', 'direct');
    await directStage(page);
    await page.goto(planUrl);
    await claim(page, 'summary', 'delegate');
    await leavesToIntegration(page);
    await integrationReview(page);
    await act(page, '接受');
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();

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
    await net.record('accept', { result: 'passed', same_page_after_refresh: here });
  } catch (e) {
    await net.record('accept', { result: 'failed', error: String(e) });
    throw e;
  } finally {
    await env.stop();
  }
});
