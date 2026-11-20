// Mock mode replays the bridge's recorded samples. Runs when samples are present
// (docs/web-api/samples after T-0069, or SAMPLES_DIR); skipped otherwise.
import { describe, expect, it } from 'vitest';
import type { Job } from '../api/types';
import { MockTransport, scenarios } from './transport';

const have = scenarios().includes('accept');

describe.skipIf(!have)('mock mode', () => {
  it('answers recorded reads and plays a job through its fixed timeline', async () => {
    const t = new MockTransport('accept');
    const ws = await t.get<{ head: string }>('/workspace');
    expect(ws.head).toBeTruthy();
    const receipt = await t.post<{ job_id: string }>('/jobs', { action: 'integration.test', args: {} });
    const states: string[] = [];
    for (let i = 0; i < 6; i++) states.push((await t.get<Job>(`/jobs/${receipt.job_id}`)).state);
    expect(states).toEqual(['queued', 'running', 'running', 'running', 'finished', 'finished']);
  });

  it('turns a cancel into cancelling and then a finish with exit code 130', async () => {
    const t = new MockTransport('accept');
    const { job_id } = await t.post<{ job_id: string }>('/jobs', { action: 'task.test', args: {} });
    await t.get<Job>(`/jobs/${job_id}`);
    await t.get<Job>(`/jobs/${job_id}`);
    await t.post(`/jobs/${job_id}/cancel`, {});
    expect((await t.get<Job>(`/jobs/${job_id}`)).state).toBe('cancelling');
    await t.get<Job>(`/jobs/${job_id}`);
    const done = await t.get<Job>(`/jobs/${job_id}`);
    expect(done.state).toBe('finished');
    expect(done.exit_code).toBe(130);
  });
});
