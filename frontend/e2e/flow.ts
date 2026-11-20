// The shared walk from a goal to a claimed delegate stage, used by every scenario.
import { expect, type Page } from '@playwright/test';
import { act, pick, refreshEnvironments } from './support';

export const GOAL = 'Build the amount and summary, then outline monthly reporting.\n';
export const EDITED = `${GOAL}Use the checked output format.\n`;

export async function goalToPlan(page: Page, base: string): Promise<void> {
  await page.goto(`${base}/new`);
  await refreshEnvironments(page);
  await page.getByLabel('原始目标').fill(GOAL);
  await pick(page, '第一位顾问', 'claude-fable-5-2', 'low');
  await act(page, '请顾问完善');
  await expect(page).toHaveURL(/\/consultations\//);
  await page.getByLabel('确认的目标').fill(EDITED);
  await act(page, '按改过的确认');
  await expect(page).toHaveURL(/\/projects\//);
  await pick(page, '规划者', 'claude-fable-5-2', 'low');
  await act(page, '生成计划');
  await expect(page).toHaveURL(/\/plans\//);
}

export async function claim(page: Page, stageId: string, mode: 'direct' | 'delegate'): Promise<void> {
  const row = page.locator('.stage-list > li').filter({ has: page.locator('code', { hasText: new RegExp(`^${stageId}$`) }) });
  await expect(row).toBeVisible();
  if (mode === 'delegate') await row.getByRole('radio', { name: /分派/ }).click();
  await pick(row, '阶段负责人', 'gpt-6.2-sol', 'low');
  await pick(row, '卡片复核', 'claude-opus-5-5', 'low');
  await act(page, '领取这个阶段', row);
  await expect(page).toHaveURL(/\/stages\//);
}

/** Direct stage: test with build and test, then a single Opus review. */
export async function directStage(page: Page): Promise<void> {
  await act(page, '运行测试');
  await page.getByRole('radio', { name: '单席' }).first().click();
  await pick(page, '审核席位 1', 'claude-opus-5-5', 'low');
  await act(page, '开始审核');
  await expect(page.getByText('审核通过', { exact: true }).first()).toBeVisible();
}

/** Delegate stage: claim each leaf with Sonnet (review first), implement, then merge. */
export async function leavesToIntegration(page: Page, opts: { cancelFirst?: (page: Page) => Promise<void> } = {}): Promise<void> {
  for (let round = 0; round < 12; round++) {
    const claimable = page.locator('.leaf').filter({ has: page.getByRole('button', { name: '领取子卡' }) });
    if (await claimable.count()) {
      const leaf = claimable.first();
      if (await leaf.getByRole('button', { name: '领取子卡' }).isEnabled()) {
        await pick(leaf, '执行者', 'claude-sonnet-5-5', 'low');
        await act(page, '领取子卡', leaf);
        continue;
      }
    }
    const runnable = page.locator('.leaf').filter({ has: page.getByRole('button', { name: '实施', exact: true }) });
    if (await runnable.count()) {
      if (opts.cancelFirst) { const f = opts.cancelFirst; opts.cancelFirst = undefined; await f(page); continue; }
      await act(page, '实施', runnable.first());
      continue;
    }
    break;
  }
  await act(page, '合并子卡');
  await expect(page).toHaveURL(/\/integrations\//);
}

export async function integrationReview(page: Page, seats: [string, string][] = [['claude-opus-5-5', 'low']]): Promise<void> {
  await act(page, '运行测试');
  if (seats.length === 1) await page.getByRole('radio', { name: '单席' }).first().click();
  await pick(page, '审核席位 1', seats[0]![0], seats[0]![1]);
  if (seats[1]) await pick(page, '审核席位 2', seats[1][0], seats[1][1]);
  const off = page.getByLabel(/自动返工/);
  if (await off.isChecked()) await off.uncheck();
  await act(page, '开始审核');
}
