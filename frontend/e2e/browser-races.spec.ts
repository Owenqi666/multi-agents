import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import type { Job, ProjectPlan, Resource, Workspace } from '../src/api/types';
import { isLockingRead, NetworkLog, OfflineEnv, pick, REPO, settled } from './support';

function acceptSample<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(REPO, 'docs/web-api/samples/accept', `${name}.json`), 'utf8')) as T;
}

const held: Job = {
  job_id: 'held', action: 'project.plan', state: 'running', args: {}, exit_code: null, result: null, error: null,
};
const cancelled: Job = { ...held, state: 'finished', exit_code: 130, cancel_requested_at: '2026-01-01T00:00:00Z' };

async function settleFrames(page: Page): Promise<void> {
  // Let the response go through fetch, state publication and the next rendered effects.
  await page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
}

test('阶段领取默认值：工作区与计划两种返回顺序，以及用户改选', async ({ browser }) => {
  const env = await OfflineEnv.start('accept');
  try {
    const workspace = acceptSample<Workspace>('006');
    workspace.defaults!.sol.effort = 'low';
    workspace.defaults!.card_review.effort = 'low';
    const plan = acceptSample<Resource<ProjectPlan>>('016');
    const planId = plan.id;
    const receipts = [];
    let net: NetworkLog | null = null;
    for (const [first, manual] of [['workspace', false], ['plan', false], ['plan', true]] as const) {
      const page = await browser.newPage();
      net = new NetworkLog(page);
      const gate = Promise.withResolvers<void>();
      const order: string[] = [];
      try {
        await page.route('**/api/**', async (route) => {
          const path = new URL(route.request().url()).pathname;
          if (path === '/api/workspace') {
            if (first !== 'workspace') await gate.promise;
            if (!order.includes('workspace')) order.push('workspace');
            await route.fulfill({ json: workspace });
          } else if (path === `/api/resources/project-plan/${planId}`) {
            if (first !== 'plan') await gate.promise;
            if (!order.includes('plan')) order.push('plan');
            await route.fulfill({ json: plan });
          } else if (path === `/api/resources/project-stages/${planId}`) {
            await route.fulfill({ json: { kind: 'project-stages', id: planId, data: { plan_id: planId, stages: [] }, artifacts: [] } });
          } else if (path.startsWith('/api/resources/project/')) {
            await route.fulfill({ json: acceptSample('013') });
          } else if (path === '/api/jobs' && route.request().method() === 'POST') {
            await route.fulfill({ status: 202, json: { job_id: 'captured', state: 'queued' } });
          } else if (path === '/api/jobs') {
            await route.fulfill({ json: { items: [] } });
          } else {
            await route.fulfill({ status: 404, json: { error: { code: 'not_found', message: path } } });
          }
        });

        const firstResponse = page.waitForResponse((r) => new URL(r.url()).pathname === (first === 'workspace' ? '/api/workspace' : `/api/resources/project-plan/${planId}`));
        await page.goto(`${env.url}/plans/${planId}`);
        await (await firstResponse).finished();
        if (first === 'plan') await expect(page.getByRole('heading', { name: plan.data!.document!.title })).toBeVisible();
        await settleFrames(page);
        gate.resolve();
        const row = page.locator('.stage-list > li').filter({ has: page.locator('code', { hasText: /^amount$/ }) });
        const review = manual ? { provider: 'codex', model: 'gpt-6.2-astra', effort: 'high' } : workspace.defaults!.card_review;
        if (manual) await pick(row, '卡片复核', review.model, review.effort);
        await expect(row.getByLabel('卡片复核：模型')).toHaveValue(review.model);
        await expect(row.getByLabel('卡片复核：档位')).toHaveValue(review.effort);
        const submission = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname === '/api/jobs');
        await row.getByRole('button', { name: '领取这个阶段' }).click();
        const body = (await submission).postDataJSON();
        expect(order.slice(0, 2)).toEqual(first === 'plan' ? ['plan', 'workspace'] : ['workspace', 'plan']);
        expect(body).toMatchObject({
          action: 'project.stage.start',
          args: { stage_id: 'amount', sol: workspace.defaults!.sol, card_review: review },
        });
        receipts.push({ order, manual, payload: body });
      } finally {
        gate.resolve();
        await page.close();
      }
    }
    await net?.record('plan-default-order', { result: 'passed', receipts });
  } finally {
    await env.stop();
  }
});

function bootReadCycle() {
  return {
    list: Promise.withResolvers<void>(),
    polled: Promise.withResolvers<void>(),
    finish: Promise.withResolvers<void>(),
    requests: [] as string[],
  };
}

test('恢复读取：直接打开或刷新阶段、任务和 diff 时，等初始作业列表与活动作业结束', async ({ context }) => {
  const env = await OfflineEnv.start('accept');
  let net: NetworkLog | null = null;
  const receipts = [];
  try {
    const stage = acceptSample<Resource<unknown>>('027');
    const task = acceptSample<Resource<unknown>>('019');
    const routes = [
      { path: `/stages/${stage.id}`, locking: `/api/resources/stage/${stage.id}`, data: stage },
      { path: `/tasks/${task.id}`, locking: `/api/resources/task/${task.id}`, data: task },
      { path: `/diffs/task/${task.id}`, locking: `/api/diffs/task/${task.id}`, data: acceptSample('020') },
    ];
    for (const { path, locking, data } of routes) {
      const page = await context.newPage();
      net = new NetworkLog(page);
      let cycle = bootReadCycle();
      await page.route('**/api/**', async (route) => {
        const { list, polled, finish, requests } = cycle;
        const requestPath = new URL(route.request().url()).pathname;
        if (isLockingRead(requestPath)) requests.push(requestPath);
        if (requestPath === '/api/jobs') {
          requests.push('GET list');
          await list.promise;
          requests.push('restore running');
          await route.fulfill({ json: { items: [held] } });
        } else if (requestPath === '/api/jobs/held') {
          polled.resolve();
          await finish.promise;
          requests.push('finish');
          await route.fulfill({ json: cancelled });
        } else if (requestPath === '/api/workspace') {
          await route.fulfill({ json: acceptSample('006') });
        } else if (requestPath === locking) {
          await route.fulfill({ json: data });
        } else {
          await route.fulfill({ status: 404, json: { error: { code: 'not_found', message: requestPath } } });
        }
      });
      try {
        for (const reload of [false, true]) {
          if (reload) {
            cycle = bootReadCycle();
            await page.reload();
          } else {
            await page.goto(`${env.url}${path}`);
          }
          const { list, polled, finish, requests } = cycle;
          await expect.poll(() => requests, { message: `${path}, reload=${reload}` }).toContain('GET list');
          await settleFrames(page);
          expect(requests.filter(isLockingRead)).toEqual([]);

          list.resolve();
          await expect(page.getByText('作业运行中，结束后可读', { exact: true })).toBeVisible();
          await polled.promise;
          expect(requests.filter(isLockingRead)).toEqual([]);

          const unlocked = page.waitForResponse((r) => new URL(r.url()).pathname === locking && r.ok());
          finish.resolve();
          await unlocked;
          await expect(page.getByText('作业运行中，结束后可读', { exact: true })).toHaveCount(0);
          await settled(page);
          receipts.push({ path, reload, requests: [...requests] });
        }
        await net.ready();
        expect(net.lockViolations()).toEqual([]);
      } finally {
        cycle.list.resolve();
        cycle.finish.resolve();
        await page.close();
      }
    }
    await net?.record('boot-locks', { result: 'passed', receipts });
  } catch (error) {
    await net?.record('boot-locks', { result: 'failed', receipts, error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});

test('取消作业：终态先于旧轮询返回，之后仍能提交新作业', async ({ page }) => {
  const env = await OfflineEnv.start('accept');
  const net = new NetworkLog(page);
  const oldPoll = Promise.withResolvers<void>();
  const receivedFirst = Promise.withResolvers<void>();
  try {
    const events: string[] = [];
    let polls = 0;
    await page.route('**/api/**', async (route) => {
      const { pathname } = new URL(route.request().url());
      if (pathname === '/api/jobs/held') {
        polls += 1;
        if (polls === 1) {
          events.push('old poll started');
          receivedFirst.resolve();
          await oldPoll.promise;
          events.push('old poll returns running');
          await route.fulfill({ json: held });
        } else {
          events.push('cancel poll returns finished');
          await route.fulfill({ json: cancelled });
        }
      } else if (pathname === '/api/jobs/held/cancel') {
        await route.fulfill({ json: { job_id: 'held', state: 'cancelling' } });
      } else if (pathname === '/api/jobs' && route.request().method() === 'POST') {
        expect(route.request().postDataJSON()).toMatchObject({ action: 'project.confirm' });
        events.push('follow-up submitted');
        await route.fulfill({ status: 202, json: { job_id: 'next', state: 'queued' } });
      } else if (pathname === '/api/jobs') {
        await route.fulfill({ json: { items: [held] } });
      } else if (pathname === '/api/jobs/next') {
        await route.fulfill({ json: { ...held, job_id: 'next', action: 'project.confirm', state: 'finished', exit_code: 0 } });
      } else if (pathname === '/api/workspace') {
        await route.fulfill({ json: acceptSample('006') });
      } else {
        await route.fulfill({ json: { items: [] } });
      }
    });

    await page.goto(`${env.url}/new`);
    await page.getByLabel('原始目标').fill('Confirm a new goal after the previous job is cancelled.');
    await receivedFirst.promise;
    await page.locator('.job-bar').getByRole('button', { name: '取消作业' }).click();
    await expect(page.locator('.job-bar')).toContainText('已取消');
    const stale = page.waitForResponse(async (r) => new URL(r.url()).pathname === '/api/jobs/held' && ((await r.json()) as Job).state === 'running');
    oldPoll.resolve();
    await (await stale).finished();
    await settleFrames(page);
    await expect(page.locator('.job-bar')).toContainText('已取消');
    await expect(page.locator('html')).toHaveAttribute('data-job', 'none');

    const confirm = page.getByRole('button', { name: '直接确认为目标' });
    await expect(confirm).toBeEnabled();
    page.once('dialog', (dialog) => void dialog.accept());
    const started = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/jobs' && r.request().method() === 'POST' && r.status() === 202);
    await confirm.click();
    await started;
    await expect(page).toHaveURL(/\/jobs\/next$/);
    expect(events).toEqual(['old poll started', 'cancel poll returns finished', 'old poll returns running', 'follow-up submitted']);
    await net.record('cancel-poll-order', { result: 'passed', events, polls });
  } catch (error) {
    await net.record('cancel-poll-order', { result: 'failed', error: String(error) });
    throw error;
  } finally {
    oldPoll.resolve();
    await env.stop();
  }
});
