import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { setTransport } from '../api/client';
import type { Consultation, Job, Workspace } from '../api/types';
import { App } from '../app/App';
import { fakeTransport } from './fake';

const workspace: Workspace = {
  repository: '/repo', target_branch: 'main', head: 'base', read_only: false,
  environments: { claude: null, codex: null },
  presets: { source: 'test', rules: {}, roles: [] },
};
const baseJob: Job = {
  job_id: 'job-1', action: 'project.consult', state: 'interrupted', exit_code: null,
  result: null, error: null,
};

function openApp(path: string) {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
  window.history.replaceState(null, '', path);
  render(<App />);
}

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/');
});

describe('归档工作区', () => {
  it('opens the archive chooser from the service start page and hides launch controls', async () => {
    const fake = fakeTransport({
      '/workspace': { ...workspace, read_only: true },
      '/jobs': { items: [] }, '/runs': { items: [] },
    });
    setTransport(fake);
    openApp('/');
    expect(await screen.findByRole('heading', { name: '归档' })).toBeTruthy();
    expect(window.location.pathname).toBe('/archive');
    expect(screen.getByText('只读', { exact: true })).toBeTruthy();
    expect(screen.queryByRole('link', { name: '提出目标' })).toBeNull();
    expect(screen.getByLabelText('记录 ID')).toBeTruthy();
    expect(fake.calls.every((call) => !call.startsWith('POST'))).toBe(true);
  });

  it('hides both cancel controls for a job shown in archive mode', async () => {
    const job = { ...baseJob, state: 'running' };
    setTransport(fakeTransport({
      '/workspace': { ...workspace, read_only: true },
      '/jobs': { items: [job] },
      '/jobs/job-1': job,
    }));
    openApp('/jobs/job-1');
    await screen.findByRole('heading', { name: '概况' });
    await screen.findByText('只读', { exact: true });
    expect(screen.queryByRole('button', { name: '取消作业' })).toBeNull();
  });
});

it('shows a structured refusal with its code, exit code, and original stderr', async () => {
  const job: Job = {
    ...baseJob, action: 'integration.accept', state: 'finished', exit_code: 2,
    error: { code: 'integration_not_eligible', message: 'The selected candidate has no eligible review.' },
    stderr_tail: 'multi-agent: the review does not allow acceptance',
  };
  setTransport(fakeTransport({ '/workspace': workspace, '/jobs': { items: [] }, '/jobs/job-1': job }));
  openApp('/jobs/job-1');
  expect(await screen.findByText('The selected candidate has no eligible review.')).toBeTruthy();
  expect(screen.getByText('integration_not_eligible')).toBeTruthy();
  expect(screen.getByText('2', { exact: true })).toBeTruthy();
  expect(screen.getByText('multi-agent: the review does not allow acceptance')).toBeTruthy();
});

it('opens the interrupted consultation from the job and preserves the first seat after reconciliation', async () => {
  const identifier = 'consultation-1';
  const request = { provider: 'codex', model: 'gpt-6.2-astra', effort: 'high' };
  const started = '2026-11-21T12:00:00Z';
  const interrupted = '2026-11-21T12:04:00Z';
  const firstAdvice = { draft_goal: 'The first seat drafted this.', background: [], assumptions: [], questions: [], remaining_disagreements: [] };
  const initial: Consultation = {
    kind: 'goal_consultation', consultation_id: identifier, status: 'running', started_at: started,
    original_goal: 'Describe the archived work.', project_id: null, baseline_commit: 'base',
    finished_at: null, draft: null,
    seats: [
      { consultation_id: identifier, number: 1, request, started_at: started, finished_at: started, status: 'succeeded', advice: firstAdvice, evidence: null, error: null },
      { consultation_id: identifier, number: 2, request, started_at: started, finished_at: null, status: 'running', advice: null, evidence: null, error: null },
    ],
  };
  let consultation = initial;
  const post = vi.fn(() => {
    consultation = {
      ...initial, status: 'failed', interrupted_at: interrupted,
      seats: [initial.seats[0]!, { ...initial.seats[1]!, status: 'failed', error: { detail: 'The consultation owner stopped.' } }],
    };
    return { job_id: 'reconcile-1', state: 'queued' };
  });
  setTransport(fakeTransport({
    '/workspace': workspace, '/jobs': { items: [] },
    '/jobs/job-1': { ...baseJob, recovery_consultation_id: identifier },
    '/jobs/reconcile-1': { ...baseJob, job_id: 'reconcile-1', action: 'project.consult.reconcile', state: 'finished', exit_code: 0 },
    '/resources/consultation/consultation-1': () => ({ kind: 'consultation', id: identifier, data: consultation, artifacts: [] }),
  }, { '/jobs': post }));

  openApp('/jobs/job-1');
  fireEvent.click(await screen.findByRole('link', { name: '打开中断咨询' }));
  const run = await screen.findByRole('button', { name: '检查并对账' });
  await waitFor(() => expect((run as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(run);
  await waitFor(() => expect(post).toHaveBeenCalledWith({ action: 'project.consult.reconcile', args: { consultation_id: identifier } }));
  expect(await screen.findByText('咨询已中断', { exact: true })).toBeTruthy();
  const first = within(screen.getByRole('heading', { name: '顾问 1' }).closest('section')!);
  expect(first.getByText('The first seat drafted this.')).toBeTruthy();
  expect(screen.getByText('The consultation owner stopped.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: '检查并对账' })).toBeNull();
});
