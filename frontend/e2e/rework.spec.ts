import { expect, test } from '@playwright/test';
import { act, NetworkLog, OfflineEnv } from './support';
import { claim, directStage, goalToPlan, integrationReview, leavesToIntegration } from './flow';

test('rework：返工后通过，显示返工增量，能从阻塞意见跳到冻结文件的对应行', async ({ page }) => {
  const env = await OfflineEnv.start('rework');
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
    await expect(page.getByText('需要返工').first()).toBeVisible();
    const integrationUrl = page.url();

    // From the blocking finding to the frozen file line.
    await page.locator('.round-head a').first().click();
    await expect(page).toHaveURL(/\/reviews\//);
    const finding = page.locator('.finding-list a', { hasText: 'frontend/status.js:1' });
    await expect(finding).toBeVisible();
    await finding.click();
    await expect(page).toHaveURL(/\/snapshots\/.+\/file\?/);
    await expect(page.locator('tr#F1')).toHaveClass(/hot/);
    await page.goto(integrationUrl);

    await act(page, '按阻塞意见返工（每条谱系一次）');
    await expect(page).not.toHaveURL(integrationUrl);
    await expect(page).toHaveURL(/\/integrations\//);
    // The rework job may already retest and re-review the child integration.
    if (await page.getByRole('button', { name: '运行测试', exact: true }).count()) await integrationReview(page);
    await expect(page.getByText('审核通过').first()).toBeVisible();
    await page.getByRole('link', { name: '返工增量' }).click();
    await expect(page).toHaveURL(/against=parent/);
    await expect(page.locator('.diff-file').first()).toBeVisible();
    await page.goBack();
    await act(page, '接受');
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();

    const posts = net.posts().length;
    const here = page.url();
    await page.reload();
    await expect(page).toHaveURL(here);
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();
    expect(net.posts().length).toBe(posts);
    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await net.record('rework', { result: 'passed', same_page_after_refresh: here });
  } catch (e) {
    await net.record('rework', { result: 'failed', error: String(e) });
    throw e;
  } finally {
    await env.stop();
  }
});
