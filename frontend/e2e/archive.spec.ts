import { expect, test, type Page } from '@playwright/test';
import { NetworkLog, OfflineEnv } from './support';

async function openRecord(page: Page, type: string, id: string) {
  await page.getByLabel('记录类型').selectOption({ label: type });
  await page.getByLabel('记录 ID').fill(id);
  await page.getByRole('button', { name: '打开记录' }).click();
}

test('旧归档从 WAL 恢复冻结输入，网页和直接请求都不能写入', async ({ page }) => {
  const env = await OfflineEnv.start('archive');
  const archive = env.archive!;
  const net = new NetworkLog(page);
  try {
    await page.goto(env.url);
    await expect(page).toHaveURL(env.url + '/archive');
    await expect(page.getByText('只读', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: '提出目标' })).toHaveCount(0);
    expect(Object.keys(archive.files).sort()).toEqual(['old-workbench.sqlite3', 'old-workbench.sqlite3-wal']);

    await openRecord(page, '冻结快照', archive.snapshotId);
    await expect(page).toHaveURL(env.url + '/diffs/snapshot/' + archive.snapshotId);
    await expect(page.getByRole('heading', { name: '冻结的审核输入' })).toBeVisible();
    await expect(page.getByText(/frozen-wal-only/).first()).toBeVisible();
    await page.getByText('完整冻结输入', { exact: true }).click();
    await expect(page.getByText(/Retain the archived requirement/)).toBeVisible();

    await page.getByRole('link', { name: '归档', exact: true }).click();
    await openRecord(page, '项目', 'project-unavailable-in-v5');
    await expect(page.getByText(/此归档版本不支持这个记录类型/)).toBeVisible();

    await page.goto(env.url + '/new');
    await expect(page.getByText('工作区为只读', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '直接确认为目标' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /检查.*环境/ })).toHaveCount(0);

    for (const action of ['project.confirm', 'task.run', 'task.retry', 'environment.refresh']) {
      const response = await page.request.post(env.url + '/api/jobs', { data: { action, args: {} } });
      expect(response.status()).toBe(403);
      expect(await response.json()).toMatchObject({ error: { code: 'archive_read_only' } });
    }
    const cancel = await page.request.post(env.url + '/api/jobs/00000000000000000000000000000000/cancel', { data: {} });
    expect(cancel.status()).toBe(403);
    expect(await cancel.json()).toMatchObject({ error: { code: 'archive_read_only' } });
    expect(await (await page.request.get(env.url + '/api/jobs')).json()).toEqual({ items: [] });
    expect(net.posts()).toEqual([]);
  } finally {
    await env.stop();
  }
  expect(await env.archiveInventory()).toEqual(archive.files);
  if (process.env.WORKBENCH_E2E_EVIDENCE) await net.record('archive', {
    result: 'passed',
    schema_version: archive.version,
    snapshot_id: archive.snapshotId,
    archive_files_after_browser: archive.files,
  });
});
