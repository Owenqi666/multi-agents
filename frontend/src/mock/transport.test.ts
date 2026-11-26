// Mock mode replays the bridge's recorded samples. Runs when samples are present
// (docs/web-api/samples after T-0069, or SAMPLES_DIR); skipped otherwise.
import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/transport';
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

const ROUND = '10000360000000000000000000000000';
const PARENT = '10000330000000000000000000000000';
type Findings = { groups: { members: { seat: string; anchor: unknown; activity: { kind: string; decision?: string; reason?: string }[] }[] }[] };
const seat1 = (f: Findings) => f.groups.flatMap((g) => g.members).find((m) => m.seat === 'seat-1')!;

describe.skipIf(!scenarios().includes('rework'))('mock mode: writes move the replay forward', () => {
  it('replays only the recorded judgement, and then reads what the recording read after it', async () => {
    const t = new MockTransport('rework', 56);
    const before = seat1(await t.get<Findings>(`/reviews/${ROUND}/findings`));
    expect(before.activity).toEqual([]);
    // Not what was recorded: refused, in words that say this is a demonstration and what it holds.
    const refused = await t.post(`/reviews/${ROUND}/decisions`, { anchor: before.anchor, decision: 'disputed', reason: 'Something else.' }).catch((e: unknown) => e);
    expect(refused).toBeInstanceOf(ApiError);
    expect((refused as ApiError).status).toBe(400);
    expect((refused as ApiError).message).toContain('The candidate still shows draft.');
    expect(seat1(await t.get<Findings>(`/reviews/${ROUND}/findings`)).activity).toEqual([]);
    // The recorded one: its 201 answer, and the findings as they were read after it.
    const entry = await t.post<{ kind: string; decision: string }>(`/reviews/${ROUND}/decisions`, { anchor: before.anchor, decision: 'confirmed', reason: 'The candidate still shows draft.' });
    expect(entry).toMatchObject({ kind: 'decision', decision: 'confirmed' });
    expect(seat1(await t.get<Findings>(`/reviews/${ROUND}/findings`)).activity.map((a) => a.kind)).toEqual(['decision']);
    // A judgement on a finding the recording never judged.
    const unknown = await t.post(`/reviews/${ROUND}/decisions`, { anchor: { ...(before.anchor as object), seat: 'seat-9' }, decision: 'confirmed', reason: 'x' }).catch((e: unknown) => e);
    expect((unknown as ApiError).code).toBe('invalid_request');
  });

  it('replays a follow-up only for the recorded finding and question, and leaves the reads where they are', async () => {
    const early = new MockTransport('rework', 58);
    const anchor = seat1(await early.get<Findings>(`/reviews/${ROUND}/findings`)).anchor;
    const recorded = 'Which status does the frozen candidate expose?';
    // The recording has not reached that submission yet.
    const tooEarly = await early.post('/jobs', { action: 'review.followup', args: { review_anchor: anchor, question: recorded } }).catch((e: unknown) => e);
    expect((tooEarly as ApiError).message).toContain('这是演示。');
    const t = new MockTransport('rework', 61);
    const other = await t.post('/jobs', { action: 'review.followup', args: { review_anchor: anchor, question: 'Something else?' } }).catch((e: unknown) => e);
    expect((other as ApiError).code).toBe('invalid_request');
    expect((other as ApiError).message).toContain(recorded);
    const elsewhere = await t.post('/jobs', { action: 'review.followup', args: { review_anchor: { ...(anchor as object), seat: 'seat-9' }, question: recorded } }).catch((e: unknown) => e);
    expect((elsewhere as ApiError).code).toBe('invalid_request');
    const { job_id } = await t.post<{ job_id: string }>('/jobs', { action: 'review.followup', args: { review_anchor: anchor, question: recorded } });
    let job = await t.get<Job>(`/jobs/${job_id}`);
    for (let i = 0; i < 5; i++) job = await t.get<Job>(`/jobs/${job_id}`);
    expect(job).toMatchObject({ state: 'finished', exit_code: 0 });
    // A job does not move the replay: the findings are those of the position, which hold that answer.
    expect(seat1(await t.get<Findings>(`/reviews/${ROUND}/findings`)).activity.map((a) => a.kind)).toEqual(['decision', 'answer']);
  });

  it('keeps an environment check with its provider, and gives a job cancelled here nothing of the recorded result', async () => {
    const t = new MockTransport('rework');
    const codex = await t.post<{ job_id: string }>('/jobs', { action: 'environment.refresh', args: { provider: 'codex' } });
    const claude = await t.post<{ job_id: string }>('/jobs', { action: 'environment.refresh', args: { provider: 'claude' } });
    expect(claude.job_id).not.toBe(codex.job_id);
    const none = await t.post('/jobs', { action: 'environment.refresh', args: { provider: 'gemini' } }).catch((e: unknown) => e);
    expect((none as ApiError).code).toBe('invalid_request');
    const { job_id } = await t.post<{ job_id: string }>('/jobs', { action: 'integration.rework', args: {} });
    await t.get<Job>(`/jobs/${job_id}`);
    await t.get<Job>(`/jobs/${job_id}`);
    await t.post(`/jobs/${job_id}/cancel`, {});
    let job = await t.get<Job>(`/jobs/${job_id}`);
    for (let i = 0; i < 4; i++) job = await t.get<Job>(`/jobs/${job_id}`);
    expect(job).toMatchObject({ state: 'finished', exit_code: 130, error_kind: 'cancelled', result: null, resources: [] });
  });

  it('starts at the end of the recording by default and stays there', async () => {
    const t = new MockTransport('rework');
    expect(seat1(await t.get<Findings>(`/reviews/${ROUND}/findings`)).activity.map((a) => a.kind)).toEqual(['decision', 'answer', 'linked_plan']);
    expect((await t.get<{ eligible: boolean }>(`/integrations/${PARENT}/rework-eligibility`)).eligible).toBe(false);
    expect(t.demoScenario).toBe('rework');
    // An `at` that is not a positive number is the whole recording too.
    for (const at of [0, Number.NaN, -3]) {
      expect(seat1(await new MockTransport('rework', at).get<Findings>(`/reviews/${ROUND}/findings`)).activity).toHaveLength(3);
    }
  });
});

