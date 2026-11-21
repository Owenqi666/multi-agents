// Component tests for the states T-0070 lists. Data are the hand-written samples in
// frontend/ (contract shapes); after T-0069 lands they move to docs/web-api/samples.

import { render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { setTransport } from '../api/client';
import { jobs } from '../api/jobs';
import type { Job, ReviewRound, Workspace } from '../api/types';
import { candidatesFor, effortsFor } from '../components/ModelPicker';
import { collabHistory, collabStage, delegateStage, directStage, jobs as jobSamples } from '../fixtures/specimen';
import { RoundSummary } from '../pages/bits';
import { JobPage } from '../pages/JobPages';
import { StagePage } from '../pages/StagePage';
import { fakeTransport } from './fake';

const workspace: Workspace = {
  repository: '/repo', target_branch: 'main', head: 'abc', read_only: false, environments: { codex: null, claude: null },
  branches: [{ name: 'main', head: 'abc' }],
  presets: {
    source: 'workbench_model_presets', rules: {},
    roles: [
      { id: 'review', label: '审核', families: [], candidates: [
        { family: 'Sol', provider: 'codex', model: 'gpt-6.2-sol', efforts: ['low', 'high'] },
        { family: 'Luna', provider: 'codex', model: 'gpt-6-luna', efforts: ['low'] },
        { family: 'Opus', provider: 'claude', model: 'claude-opus-5-5', efforts: ['low', 'high', 'max'] },
        { family: 'Haiku', provider: 'claude', model: 'claude-haiku-5', efforts: ['low'] },
      ] },
      { id: 'implementation', label: '实施', families: [], candidates: [
        { family: 'Sol', provider: 'codex', model: 'gpt-6.2-sol', efforts: ['high'] },
        { family: 'Terra', provider: 'codex', model: 'gpt-6.2-terra', efforts: ['low', 'medium', 'high'] },
        { family: 'Sonnet', provider: 'claude', model: 'claude-sonnet-5-5', efforts: null },
      ] },
    ],
  },
};

const resource = (kind: string, id: string, data: unknown) => ({ kind, id, data, artifacts: [] });
const job = (label: string): Job => jobSamples.find((j) => j.label === label)!.job;

describe('作业期间的读取', () => {
  it('does not send a locking read while a CLI job is unfinished', async () => {
    const fake = fakeTransport({
      '/jobs': { items: [job('running')] },
      '/jobs/4be1c0d29f8a7e6b5c4d3e2f1a0b9c8d': job('running'),
      '/workspace': workspace,
      '/resources/stage/sr-job-panel-01': resource('stage', 'sr-job-panel-01', directStage),
    });
    setTransport(fake);
    await jobs.init();
    render(<StagePage id="sr-job-panel-01" />);
    expect(await screen.findByText('作业运行中，结束后可读')).toBeTruthy();
    expect(fake.calls).not.toContain('GET /resources/stage/sr-job-panel-01');
  });

  it('treats a lock_busy answer like the lock, keeping nothing it did not read', async () => {
    setTransport(fakeTransport({
      '/workspace': workspace,
      '/jobs': { items: [] },
      '/resources/stage/s1': { kind: 'stage', id: 's1', data: null, artifacts: [], lock_busy: true },
    }));
    await jobs.init();
    render(<StagePage id="s1" />);
    expect(await screen.findByText('作业运行中，结束后可读')).toBeTruthy();
  });
});

describe('作业状态', () => {
  const show = async (j: Job) => {
    setTransport(fakeTransport({ [`/jobs/${j.job_id}`]: j }));
    render(<JobPage id={j.job_id} />);
    await screen.findByText(/概况/);
  };

  it('shows stderr as written when exit code 2 came without a result', async () => {
    await show({ ...job('finished · 2，没有结果'), stderr_tail: 'error: integration is not eligible for acceptance\n' });
    expect(screen.getAllByText('被拒绝').length).toBeGreaterThan(0);
    expect(screen.getByText('被拒绝的原因')).toBeTruthy();
    expect(screen.getByText(/integration is not eligible for acceptance/)).toBeTruthy();
  });

  it('says cancellation was requested and offers no cancel button', async () => {
    await show(job('cancelling'));
    expect(screen.getAllByText('已请求取消').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: '取消作业' })).toBeNull();
  });

  it('offers cancel only while running', async () => {
    await show(job('running'));
    expect(screen.getByRole('button', { name: '取消作业' })).toBeTruthy();
  });

  it('explains an interrupted job and points to recovery', async () => {
    await show(job('interrupted'));
    expect(screen.getAllByText('已中断').length).toBeGreaterThan(0);
    expect(screen.getByText(/服务重启时这个作业还没结束/)).toBeTruthy();
  });

  it('reads a cancelled plan from its receipt even though it exits with 1', async () => {
    await show({ ...job('finished · 1，带结果'), action: 'project.plan', result: { status: 'failed', error: { failure_category: 'cancelled' } } });
    expect(screen.getAllByText('已取消').length).toBeGreaterThan(0);
    expect(screen.queryByText('结果不满足')).toBeNull();
  });

  it('reads a cancelled finish from the exit code, not from error being null', async () => {
    await show(job('finished · 130'));
    expect(screen.getAllByText('已取消').length).toBeGreaterThan(0);
  });
});

describe('审核席位和报告值', () => {
  const rounds = directStage.implementation!.reviews as ReviewRound[];

  it('marks a missing report 未确认 and keeps the request apart, in a dual blind round', () => {
    render(<RoundSummary round={rounds[0]!} />);
    expect(screen.getByText('双席，互不可见')).toBeTruthy();
    const seat2 = screen.getByText('reviewer-2').closest('li')!;
    expect(within(seat2).getAllByText('未确认').length).toBe(2);
    expect(within(seat2).getByText('请求')).toBeTruthy();
    expect(within(seat2).getByText(/gpt-6\.2-astra/)).toBeTruthy();
  });

  it('draws a single seat as one', () => {
    const single = (delegateStage.integration!.evidence!.reviews[0]) as ReviewRound;
    render(<RoundSummary round={single} />);
    expect(screen.getByText('单席')).toBeTruthy();
  });
});

describe('阶段：直接和分派，有和没有返工', () => {
  const stagePage = async (id: string, data: unknown) => {
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/workspace': workspace, [`/resources/stage/${id}`]: resource('stage', id, data) }));
    await jobs.init();
    const view = render(<StagePage id={id} />);
    await screen.findByText('门槛');
    return view;
  };

  it('direct stage with a rework: loop on the stage, rework increment offered', async () => {
    const { container } = await stagePage('sr-job-panel-01', directStage);
    expect(screen.getByText('直接模式')).toBeTruthy();
    expect(screen.getByText('返工增量')).toBeTruthy();
    expect(screen.getByText('已用过一次')).toBeTruthy();
    expect(container.querySelector('svg.track')?.textContent).toContain('返工后回到测试');
  });

  it('delegate stage without rework: leaves listed, no loop drawn', async () => {
    const { container } = await stagePage('sr-review-pages-01', delegateStage);
    expect(screen.getByText('分派模式')).toBeTruthy();
    expect(screen.getByText('review-list')).toBeTruthy();
    expect(screen.getByText('diff-viewer')).toBeTruthy();
    expect(container.querySelector('svg.track')?.textContent).not.toContain('返工后回到测试');
    expect(collabHistory.length + (collabStage.integration ? 1 : 0)).toBe(2);
  });
});

describe('模型选择器', () => {
  it('offers only the agreed families even when the catalog lists Luna and Haiku', () => {
    const families = candidatesFor(workspace, 'review').map((c) => c.family);
    expect(families).toEqual(['Sol', 'Opus']);
  });

  it('keeps low when the chosen model supports it', () => {
    const opus = candidatesFor(workspace, 'review').find((c) => c.family === 'Opus');
    expect(effortsFor(opus)).toContain('low');
  });

  it('lets leaf executors be only Terra or Sonnet', () => {
    expect(candidatesFor(workspace, 'implementation', ['Terra', 'Sonnet']).map((c) => c.family)).toEqual(['Terra', 'Sonnet']);
  });

  it('keeps the card reviewer off Sol’s model', () => {
    expect(candidatesFor(workspace, 'review', undefined, 'gpt-6.2-sol').map((c) => c.model)).not.toContain('gpt-6.2-sol');
  });
});

describe('重复提交保护', () => {
  it('sends nothing while a CLI job is unfinished and points at it instead', async () => {
    const fake = fakeTransport({ '/jobs': { items: [job('running')] } }, { '/jobs': { job_id: 'x', state: 'queued' } });
    setTransport(fake);
    await jobs.init();
    const r = await jobs.submit('project.stage.integrate', { stage_run_id: 's' });
    expect(r).toEqual({ busy: job('running').job_id });
    expect(fake.calls.filter((c) => c.startsWith('POST'))).toEqual([]);
  });

  it('follows a 409 busy to the job that holds the lock', async () => {
    const { ApiError } = await import('../api/transport');
    const fake = fakeTransport({ '/jobs/held': job('running') }, { '/jobs': () => new ApiError(409, 'busy', 'busy', { active_job_id: 'held' }) });
    setTransport(fake);
    const r = await jobs.submit('integration.create', { task_run_ids: ['t'], target: 'main' });
    expect(r).toEqual({ busy: 'held' });
    await waitFor(() => expect(jobs.getState().active?.job_id).toBe(job('running').job_id));
  });
});
