import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, test } from '@playwright/test';
import type { Consultation, Job, ProjectPlan, Resource, RunItem } from '../src/api/types';
import { NetworkLog, OfflineEnv, REPO } from './support';

function sample<T>(id: string): T {
  return JSON.parse(readFileSync(resolve(REPO, 'docs/web-api/samples/accept', `${id}.json`), 'utf8')) as T;
}

test('首页作业归属：另一项目只显示全局作业，同项目的规划推进舞台', async ({ page }) => {
  const env = await OfflineEnv.start('accept');
  const net = new NetworkLog(page);
  try {
    const plan = sample<Resource<ProjectPlan>>('016');
    const consultation = sample<Resource<Consultation>>('009');
    const projectA = plan.data!.goal.project_id;
    const goalA = plan.data!.goal.goal_id;
    const projectB = 'project-20000080000000000000000000000000';
    const goalB = 'goal-20000090000000000000000000000000';
    const older = '1999-12-31T23:59:59.000000+00:00';
    const runs = sample<{ items: RunItem[] }>('055').items.filter((run) =>
      ['project', 'goal', 'project-plan', 'consultation'].includes(run.kind));
    runs.push(
      { kind: 'project', id: projectB, parent_id: null, label: 'Project B', updated_at: older },
      { kind: 'goal', id: goalB, parent_id: projectB, label: 'Earlier goal B', updated_at: older },
    );

    const planning = (jobId: string, goalId: string): Job => ({
      job_id: jobId,
      action: 'project.plan',
      args: { goal_id: goalId, planner: plan.data!.planner_request },
      state: 'running',
      exit_code: null,
      result: null,
      error: null,
    });
    const foreignJob = planning('20000500000000000000000000000000', goalB);
    const ownJob = planning('10000500000000000000000000000000', goalA);
    let active: Job | null = null;
    await page.route('**/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/jobs') await route.fulfill({ json: { items: active ? [active] : [] } });
      else if (path === `/api/jobs/${foreignJob.job_id}`) await route.fulfill({ json: foreignJob });
      else if (path === `/api/jobs/${ownJob.job_id}`) await route.fulfill({ json: ownJob });
      else if (path === '/api/runs') await route.fulfill({ json: { items: runs } });
      else if (path === `/api/resources/project-plan/${plan.id}`) await route.fulfill({ json: plan });
      else if (path === `/api/resources/consultation/${consultation.id}`) await route.fulfill({ json: consultation });
      else await route.continue();
    });

    const timeline = page.getByRole('group', { name: '步骤进度' });
    const steps = timeline.getByRole('button');
    const current = timeline.locator('[aria-current="step"]');
    const global = page.locator('.job-bar').getByRole('link', { name: '作业详情', exact: true });
    const runningNote = page.locator('.home .caption').getByText('作业「项目计划」正在进行。', { exact: false });
    const titles = ['需求顾问', '确认目标', '项目计划'];
    const checkTimeline = async (expected: string[]) => {
      const labels = expected.map((title, i) => `第 ${i + 1} 步：${title}`);
      await expect(steps).toHaveCount(labels.length);
      for (const [i, label] of labels.entries()) {
        await expect(steps.nth(i)).toHaveAccessibleName(label);
      }
      await expect(current).toHaveAccessibleName(labels.at(-1)!);
      return {
        steps: await steps.evaluateAll((buttons) => buttons.map((button) => button.getAttribute('title'))),
        current: await current.getAttribute('aria-label'),
      };
    };

    await page.goto(`${env.url}/`);
    const before = await checkTimeline(titles);
    await expect(page.getByRole('link', { name: '打开项目计划', exact: true })).toHaveAttribute('href', `/plans/${plan.id}`);

    active = foreignJob;
    await page.reload();
    await expect(global).toHaveAttribute('href', `/jobs/${foreignJob.job_id}`);
    const foreign = { ...await checkTimeline(titles), link: await global.getAttribute('href') };
    await expect(runningNote).toHaveCount(0);
    await global.click();
    await expect(page).toHaveURL(`${env.url}/jobs/${foreignJob.job_id}`);
    await expect(page.getByText(`goal_id：${goalB}`, { exact: false })).toBeVisible();

    active = ownJob;
    await page.goto(`${env.url}/`);
    await expect(global).toHaveAttribute('href', `/jobs/${ownJob.job_id}`);
    const own = { ...await checkTimeline([...titles, '项目计划']), link: await global.getAttribute('href') };
    await expect(runningNote).toBeVisible();
    await net.record('home-job-scope', {
      result: 'passed',
      history: { project: projectA, goal: goalA, plan: plan.id },
      foreign_job: { project: projectB, goal: goalB, id: foreignJob.job_id },
      own_job: { project: projectA, goal: goalA, id: ownJob.job_id },
      before, foreign, own,
    });
  } catch (error) {
    await net.record('home-job-scope', { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});
