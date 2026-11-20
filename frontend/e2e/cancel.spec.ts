import { expect, test, type Page } from '@playwright/test';
import { act, NetworkLog, OfflineEnv, pick } from './support';
import { claim, directStage, goalToPlan } from './flow';

/** The fixture's sync point: a Codex environment check finishes only once the running Sonnet
 *  leaf has started its session, so the job is surely mid-flight afterwards. */
async function midFlight(page: Page, base: string): Promise<void> {
  const r = await page.request.post(`${base}/api/jobs`, { data: { action: 'environment.refresh', args: { provider: 'codex' } } });
  const { job_id } = (await r.json()) as { job_id: string };
  await expect.poll(async () => ((await (await page.request.get(`${base}/api/jobs/${job_id}`)).json()) as { state: string }).state, { timeout: 120_000 }).toBe('finished');
}

test('cancel：作业中取消，再硬停重启，页面显示 interrupted 并引导恢复', async ({ page }) => {
  const env = await OfflineEnv.start('cancel');
  const net = new NetworkLog(page);
  page.on('dialog', (d) => void d.accept());
  try {
    await goalToPlan(page, env.url);
    const planUrl = page.url();
    await claim(page, 'amount', 'direct');
    await directStage(page);
    await page.goto(planUrl);
    await claim(page, 'summary', 'delegate');
    for (let i = 0; i < 2; i++) {
      const leaf = page.locator('.leaf').filter({ has: page.getByRole('button', { name: '领取子卡' }) }).first();
      await pick(leaf, '执行者', 'claude-sonnet-5-5', 'low');
      await act(page, '领取子卡', leaf);
    }
    const stageUrl = page.url();

    // Cancel the first leaf while it runs.
    await page.locator('.leaf').getByRole('button', { name: '实施', exact: true }).first().click();
    await midFlight(page, env.url);
    await page.locator('.job-bar').getByRole('button', { name: '取消作业' }).click();
    await expect(page.locator('.job-bar')).toContainText('已请求取消');
    await expect(page.locator('.job-bar').getByRole('button', { name: '取消作业' })).toHaveCount(0);
    await expect(page.locator('.job-bar')).toContainText('已取消', { timeout: 120_000 });
    await expect(page.locator('.job-bar')).toContainText('退出码 130');
    await page.getByRole('button', { name: '收起' }).click();

    // Hard-stop the service while the second leaf runs.
    await page.goto(stageUrl);
    const second = page.locator('.leaf').getByRole('button', { name: '实施', exact: true });
    await expect(second.first()).toBeEnabled({ timeout: 60_000 });
    await second.first().click();
    await midFlight(page, env.url);
    await env.hardRestart();
    await expect(page.locator('.job-bar')).toContainText('已中断', { timeout: 120_000 });
    await page.locator('.job-bar').getByRole('link', { name: '作业详情' }).click();
    await expect(page.getByText(/服务重启时这个作业还没结束/)).toBeVisible();
    const here = page.url();
    const posts = net.posts().length;
    await page.reload();
    await expect(page).toHaveURL(here);
    await expect(page.getByText(/服务重启时这个作业还没结束/)).toBeVisible();
    await expect(page.getByText('已中断').first()).toBeVisible();
    expect(net.posts().length).toBe(posts);
    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await net.record('cancel', { result: 'passed', same_page_after_refresh: here });
  } catch (e) {
    await net.record('cancel', { result: 'failed', error: String(e) });
    throw e;
  } finally {
    await env.stop();
  }
});
