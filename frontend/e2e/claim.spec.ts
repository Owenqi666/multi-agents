import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';
import type { ProjectPlan, Resource, Stage, Workspace } from '../src/api/types';
import { claim, directStage, goalToPlan, integrationReview, leavesToIntegration } from './flow';
import { act, NetworkLog, OfflineEnv } from './support';

test('领取阶段：独立前置阶段经过测试和审核后，可指定真实合并基线', async ({ page }) => {
  const env = await OfflineEnv.start('claim');
  const net = new NetworkLog(page);
  try {
    await goalToPlan(page, env.url);
    await page.getByRole('button', { name: '修订计划', exact: true }).click();
    const editor = page.getByRole('textbox', { name: '计划文档' });
    const document = JSON.parse(await editor.inputValue()) as NonNullable<ProjectPlan['document']>;
    document.stages.find((s) => s.stage_id === 'summary')!.depends_on = [];
    document.stages.find((s) => s.stage_id === 'report')!.depends_on = ['amount', 'summary'];
    await editor.fill(JSON.stringify(document));
    const originalPlan = page.url();
    await act(page, '保存为新版本');
    await expect(page).not.toHaveURL(originalPlan);
    const planUrl = page.url();
    const report = page.locator('.stage-list > li').filter({ has: page.locator('code', { hasText: /^report$/ }) });
    const reportClaim = report.getByRole('button', { name: '领取这个阶段', exact: true });
    const gate = (deps: string) => report.getByText(`等待前置阶段通过测试和审核：${deps}。`);
    await expect(gate('amount、summary')).toBeVisible();
    await expect(reportClaim).toBeDisabled();

    await claim(page, 'amount', 'direct');
    const amountUrl = page.url();
    // A claimed stage with an implementation is still missing its required evidence.
    await page.goto(planUrl);
    await expect(gate('amount、summary')).toBeVisible();
    await expect(reportClaim).toBeDisabled();
    await page.goto(amountUrl);
    await directStage(page);

    await page.goto(planUrl);
    await expect(gate('summary')).toBeVisible();
    await expect(reportClaim).toBeDisabled();
    await claim(page, 'summary', 'delegate');
    const summaryUrl = page.url();
    await leavesToIntegration(page);
    await integrationReview(page);

    const readStage = async (url: string): Promise<Stage> => {
      const id = new URL(url).pathname.split('/').pop()!;
      const response = await page.request.get(`${env.url}/api/resources/stage/${id}`);
      expect(response.ok()).toBe(true);
      return ((await response.json()) as Resource<Stage>).data!;
    };
    const [a, b, workspace] = await Promise.all([
      readStage(amountUrl),
      readStage(summaryUrl),
      page.request.get(`${env.url}/api/workspace`).then((r) => r.json() as Promise<Workspace>),
    ]);
    expect(a.review_satisfied).toBe(true);
    expect(b.review_satisfied).toBe(true);
    expect(a.candidate).toBeTruthy();
    expect(b.candidate).toBeTruthy();

    // Create the merge object in this test's disposable fixture repository, leaving its
    // checked-out branch and the service's target ref alone.
    const git = (...args: string[]): string => execFileSync('git', [
      '-C', workspace.repository, '-c', 'user.name=Offline Fixture', '-c', 'user.email=offline@example.invalid',
      '-c', 'commit.gpgsign=false', ...args,
    ], { encoding: 'utf8' }).trim();
    const amount = a.candidate!;
    const summary = b.candidate!;
    const common = git('merge-base', amount, summary);
    expect(common).toBe(a.execution_baseline);
    expect(common).not.toBe(amount);
    expect(common).not.toBe(summary);
    const tree = git('merge-tree', '--write-tree', amount, summary);
    const merge = git('commit-tree', tree, '-p', amount, '-p', summary, '-m', 'Merge independent offline stage candidates');

    await page.goto(planUrl);
    await expect(reportClaim).toBeEnabled();
    await report.getByRole('textbox', { name: '执行基线（可选）' }).fill(merge);
    const action = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname === '/api/jobs');
    await claim(page, 'report', 'delegate');
    const { args } = (await action).postDataJSON() as { args: { baseline: string; stage_id: string } };
    expect(args).toMatchObject({ baseline: merge, stage_id: 'report' });
    const done = await readStage(page.url());
    expect(done.execution_baseline).toBe(merge);
    expect(done.dependencies).toEqual([
      expect.objectContaining({ stage_id: 'amount', candidate: amount }),
      expect.objectContaining({ stage_id: 'summary', candidate: summary }),
    ]);
    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await net.record('claim', { result: 'passed', original_baseline: common, amount, summary, merge, stage: done.stage_run_id });
  } catch (error) {
    await net.record('claim', { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});
