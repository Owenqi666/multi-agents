// The same page states as states.test.tsx, rendered from the bridge's recorded samples
// (docs/web-api/samples, T-0069). Skipped where the samples are not present.

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { setTransport } from '../api/client';
import { jobs } from '../api/jobs';
import type { Job, ReviewRound } from '../api/types';
import { RoundSummary } from '../pages/bits';
import { IntegrationPage } from '../pages/IntegrationPage';
import { JobPage } from '../pages/JobPages';
import { StagePage } from '../pages/StagePage';
import { DiffPage, LogPage } from '../pages/ViewerPages';
import { fakeTransport } from './fake';

const files = import.meta.glob<unknown>('@samples/*/*.json', { eager: true, import: 'default' });

function sample<T>(scenario: string, file: string): T {
  const key = Object.keys(files).find((k) => k.endsWith(`/${scenario}/${file}`));
  if (!key) throw new Error(`missing sample ${scenario}/${file}`);
  return structuredClone(files[key]) as T;
}

const have = Object.keys(files).some((k) => k.endsWith('/cancel/050.json'));
const workspace = () => sample('accept', '006.json');

describe.skipIf(!have)('录制样例里的页面状态', () => {
  const showJob = async (job: Job) => {
    setTransport(fakeTransport({ [`/jobs/${job.job_id}`]: job }));
    render(<JobPage id={job.job_id} />);
    await screen.findByText(/概况/);
  };

  it('exit code 2 without a result: refused, stderr as written', async () => {
    const job = sample<Job>('accept', '043.json');
    expect(job.exit_code).toBe(2);
    expect(job.result).toBeNull();
    await showJob(job);
    expect(screen.getByText('被拒绝的原因')).toBeTruthy();
    expect(screen.getByText((job.stderr_tail ?? '').trim().split('\n')[0]!, { exact: false })).toBeTruthy();
  });

  it('cancelling, then cancelled with exit code 130', async () => {
    const done = sample<Job>('cancel', '044.json');
    const receipt = sample<{ state: Job['state'] }>('cancel', '043.json');
    await showJob({ ...done, state: receipt.state, exit_code: null, result: null, finished_at: null });
    expect(screen.getAllByText('已请求取消').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: '取消作业' })).toBeNull();
  });

  it('a finished cancel reads as 已取消', async () => {
    await showJob(sample<Job>('cancel', '044.json'));
    expect(screen.getAllByText('已取消').length).toBeGreaterThan(0);
  });

  it('interrupted after a hard restart, with guidance', async () => {
    await showJob(sample<Job>('cancel', '050.json'));
    expect(screen.getAllByText('已中断').length).toBeGreaterThan(0);
    expect(screen.getByText(/服务重启时这个作业还没结束/)).toBeTruthy();
  });

  it('lock_busy stage, diff and log answers say they are readable after the job', async () => {
    setTransport(fakeTransport({
      '/jobs': { items: [] },
      '/workspace': workspace(),
      '/resources/stage/stage-100001c0000000000000000000000000': sample('cancel', '040.json'),
      '/diffs/task/10000110000000000000000000000000': sample('cancel', '041.json'),
    }));
    await jobs.init();
    const stage = render(<StagePage id="stage-100001c0000000000000000000000000" />);
    expect(await screen.findByText('作业运行中，结束后可读')).toBeTruthy();
    stage.unmount();
    render(<DiffPage kind="task" id="10000110000000000000000000000000" />);
    expect(await screen.findByText(/作业运行中，结束后可读/)).toBeTruthy();
  });

  it('a lock_busy log page has no text to show', async () => {
    const page = sample<{ text: string | null; lock_busy?: boolean }>('cancel', '042.json');
    expect(page.lock_busy).toBe(true);
    setTransport(fakeTransport({ '/artifacts/x': page }));
    render(<LogPage id="x" title="测试日志" locking={false} />);
    expect(await screen.findByText('作业运行中，结束后可读')).toBeTruthy();
  });

  it('dual review with an unreported model, and a single review', () => {
    const dual = sample<{ data: ReviewRound }>('dual-partial', '049.json').data;
    const view = render(<RoundSummary round={dual} />);
    expect(screen.getByText('双席，互不可见')).toBeTruthy();
    const seat2 = screen.getByText('seat-2').closest('li')!;
    expect(within(seat2).getAllByText('未确认').length).toBeGreaterThan(0);
    expect(within(seat2).getByText('请求')).toBeTruthy();
    view.unmount();
    render(<RoundSummary round={sample<{ data: ReviewRound }>('rework', '046.json').data} />);
    expect(screen.getByText('单席')).toBeTruthy();
  });

  it('direct and delegate stages', async () => {
    const direct = sample<{ data: { stage_run_id: string } }>('accept', '027.json');
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/workspace': workspace(), [`/resources/stage/${direct.data.stage_run_id}`]: direct }));
    await jobs.init();
    const a = render(<StagePage id={direct.data.stage_run_id} />);
    expect(await screen.findByText('直接模式')).toBeTruthy();
    a.unmount();
    const delegate = sample<{ data: { stage_run_id: string } }>('accept', '053.json');
    setTransport(fakeTransport({ '/workspace': workspace(), [`/resources/stage/${delegate.data.stage_run_id}`]: delegate }));
    render(<StagePage id={delegate.data.stage_run_id} />);
    expect(await screen.findByText('分派模式')).toBeTruthy();
    expect(screen.getByText('T-0800')).toBeTruthy();
  });

  it('an integration with a rework offers its increment; one without does not', async () => {
    const reworked = sample<{ data: { integration_id: string } }>('rework', '055.json');
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/workspace': workspace(), [`/resources/integration/${reworked.data.integration_id}`]: reworked }));
    await jobs.init();
    const a = render(<IntegrationPage id={reworked.data.integration_id} />);
    expect(await screen.findByText('返工增量')).toBeTruthy();
    a.unmount();
    const plain = sample<{ data: { integration_id: string } }>('accept', '052.json');
    setTransport(fakeTransport({ '/workspace': workspace(), [`/resources/integration/${plain.data.integration_id}`]: plain }));
    render(<IntegrationPage id={plain.data.integration_id} />);
    await screen.findByText('累计 diff');
    expect(screen.queryByText('返工增量')).toBeNull();
  });
});
