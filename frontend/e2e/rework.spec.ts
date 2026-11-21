import { expect, test, type Page } from '@playwright/test';
import type { IntegrationRecord } from '../src/api/types';
import { act, NetworkLog, OfflineEnv, readback } from './support';
import { claim, directStage, goalToPlan, integrationReview, leavesToIntegration } from './flow';

async function verifyLineage(page: Page, parent: string, child: string) {
  const frame = page.locator('.stage-frame');
  const captions = [
    ['代码审核', ['需要返工，1 条阻塞意见', parent.slice(0, 7)]],
    ['返工', ['1 条阻塞意见']],
    ['复测', ['全部通过', child.slice(0, 7)]],
    ['复审', ['结论：通过', child.slice(0, 7)]],
    ['接受', ['阶段交付完成', child.slice(0, 7)]],
  ] as const;
  const ticks = frame.locator('.tick');
  await expect.poll(async () => (await ticks.evaluateAll((els) => els.map((e) => e.getAttribute('title')))).slice(-captions.length))
    .toEqual(captions.map(([title]) => title));
  const result = [];
  for (const [title, facts] of captions) {
    await frame.getByRole('button', { name: new RegExp(`：${title}$`) }).click();
    const caption = frame.locator('.caption');
    for (const fact of facts) await expect(caption).toContainText(fact);
    result.push({ title, text: await caption.innerText() });
  }
  return result;
}

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
    const stageUrl = page.url();
    await leavesToIntegration(page);
    await integrationReview(page);
    await expect(page.getByText('需要返工').first()).toBeVisible();
    const integrationUrl = page.url();
    const parent = await readback<IntegrationRecord>(page, integrationUrl.replace('/integrations/', '/api/resources/integration/'));

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
    const childUrl = page.url();
    const child = await readback<IntegrationRecord>(page, childUrl.replace('/integrations/', '/api/resources/integration/'));
    await page.getByRole('link', { name: /^返工自/ }).click();
    await expect(page).toHaveURL(integrationUrl);
    await page.goto(stageUrl);
    const stage = await verifyLineage(page, parent.candidate!.commit, child.candidate!.commit);
    await page.goto(`${env.url}/`);
    const home = await verifyLineage(page, parent.candidate!.commit, child.candidate!.commit);
    await page.goto(childUrl);

    const posts = net.posts().length;
    const here = page.url();
    await page.reload();
    await expect(page).toHaveURL(here);
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();
    expect(net.posts().length).toBe(posts);
    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await net.record('rework', {
      result: 'passed', same_page_after_refresh: here,
      parent: { id: parent.integration_id, candidate: parent.candidate?.commit },
      child: { id: child.integration_id, candidate: child.candidate?.commit, accepted_round: child.acceptance?.review_round_id },
      visible_lineage: { home, stage },
    });
  } catch (e) {
    await net.record('rework', { result: 'failed', error: String(e) });
    throw e;
  } finally {
    await env.stop();
  }
});
