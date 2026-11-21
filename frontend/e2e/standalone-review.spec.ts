import { expect, test, type Page } from '@playwright/test';
import type { ReviewRound, Workspace } from '../src/api/types';
import { checkSessionLog, sessionArtifacts, sessionResource } from './session-logs';
import { act, jobRequest, NetworkLog, OfflineEnv, pick, readback, refreshEnvironments } from './support';

const AMOUNT = 'backend/amount.py';
const REQUIREMENTS = 'Check the amount threshold in this isolated change.';
const ACCEPTANCE = 'The amount meets the declared threshold.';
const CODEX = { provider: 'codex', model: 'gpt-6.2-astra', effort: 'low' };
const CLAUDE = { provider: 'claude', model: 'claude-opus-5-5', effort: 'low' };
const PATCH = [
  `--- a/${AMOUNT}`,
  `+++ b/${AMOUNT}`,
  '@@ -3 +5 @@',
  '-AMOUNT = 0',
  '+AMOUNT = 1',
  '',
].join('\n');

interface Snapshot {
  snapshot_id: string;
  input: {
    baseline: string | null;
    candidate: string | null;
    diff: string;
    files: { side: string; path: string; text: string | null }[];
    requirements: string;
    acceptance: string[];
    tests: { commit: string; command: string[]; exit_code: number; log: string }[];
    required_tests: string[][];
  };
}

async function beginSnapshot(page: Page, origin: string, mode: string) {
  await page.goto(origin);
  await page.getByRole('link', { name: '审核', exact: true }).click();
  await page.getByRole('link', { name: '新建独立审核', exact: true }).click();
  await page.getByRole('radio', { name: mode, exact: true }).check();
  await page.getByLabel('审核需求', { exact: true }).fill(REQUIREMENTS);
  await page.getByLabel('验收标准', { exact: true }).fill(ACCEPTANCE);
}

async function freeze(page: Page, origin: string, source: object) {
  const request = jobRequest(page, 'review.freeze');
  await act(page, '冻结审核输入');
  expect((await request).postDataJSON().args).toEqual({
    ...source, requirements: REQUIREMENTS, acceptance: [ACCEPTANCE],
  });
  await expect(page).toHaveURL(/\/snapshots\/[0-9a-f]{64}$/);
  const id = page.url().split('/').at(-1)!;
  const snapshot = await readback<Snapshot>(page, `${origin}/api/resources/snapshot/${id}`);
  expect(snapshot.snapshot_id).toBe(id);
  expect(snapshot.input).toMatchObject({ requirements: REQUIREMENTS, acceptance: [ACCEPTANCE] });
  return snapshot;
}

function roundAt(page: Page, origin: string, id: string) {
  return sessionResource<ReviewRound>(page, origin, 'review', id);
}

async function startReview(page: Page, origin: string, snapshot: Snapshot, seats: (typeof CODEX)[]) {
  await refreshEnvironments(page);
  await page.getByRole('radio', { name: seats.length === 1 ? '单席' : '双席盲审', exact: true }).check();
  for (const [index, seat] of seats.entries()) {
    await pick(page, `审核席位 ${index + 1}`, seat.model, seat.effort);
  }
  const request = jobRequest(page, 'review.run');
  await act(page, '开始审核');
  expect((await request).postDataJSON().args).toEqual({ snapshot_id: snapshot.snapshot_id, seats });
  await expect(page).toHaveURL(/\/reviews\/[a-f0-9]{32}$/);
  const resource = await roundAt(page, origin, page.url().split('/').at(-1)!);
  expect(resource.data.snapshot_id).toBe(snapshot.snapshot_id);
  expect(resource.data.seats.map((seat) => ({
    provider: seat.provider, model: seat.requested_model, effort: seat.requested_effort,
  }))).toEqual(seats);
  return resource;
}

async function record(net: NetworkLog, name: string, evidence: Record<string, unknown>) {
  if (process.env.WORKBENCH_E2E_EVIDENCE) await net.record(name, evidence);
}

test('提交对审核标记用户测试，成功的席位仍报告可核验的阻塞问题', async ({ page }) => {
  const env = await OfflineEnv.start('standalone-review');
  const net = new NetworkLog(page);
  const scenario = 'standalone-review-commits';
  try {
    await beginSnapshot(page, env.url, '提交对');
    const workspace = await page.request.get(`${env.url}/api/workspace`);
    expect(workspace.ok()).toBe(true);
    const candidate = ((await workspace.json()) as Workspace).head!;
    const baseline = 'HEAD~1';
    expect(candidate).toMatch(/^[0-9a-f]{40}$/);
    await page.getByLabel('基线提交', { exact: true }).fill(baseline);
    await page.getByLabel('候选提交', { exact: true }).fill(candidate);
    const command = ['python', 'checks/test.py'];
    const tests = {
      required_commands: [command],
      results: [{ commit: candidate, command, exit_code: 0, log: 'Offline supplied test log.' }],
    };
    await page.getByText('添加测试证据（可选）', { exact: true }).click();
    await page.getByLabel('用户提供的测试证据（JSON）', { exact: true }).fill(JSON.stringify(tests));
    const snapshot = await freeze(page, env.url, { baseline, candidate, tests });
    expect(snapshot.input.baseline).toMatch(/^[0-9a-f]{40}$/);
    expect(snapshot.input.baseline).not.toBe(candidate);
    expect(snapshot.input).toMatchObject({ candidate, tests: tests.results, required_tests: tests.required_commands });
    expect(snapshot.input.diff).toContain('-AMOUNT = 0');
    expect(snapshot.input.diff).toContain('+AMOUNT = 1');
    await expect(page.getByText('用户提供', { exact: true }).first()).toBeVisible();
    await page.getByText('查看测试输出', { exact: true }).click();
    await expect(page.getByText(tests.results[0]!.log, { exact: true })).toBeVisible();

    const round = await startReview(page, env.url, snapshot, [CODEX]);
    expect(round.data.seats[0]!.attempts).toMatchObject([{ number: 1, status: 'succeeded', exit_code: 0 }]);
    expect(round.data.assessment).toMatchObject({
      status: 'needs_rework',
      reasons: ['blocking_findings_present'],
      groups: [{ blocking: true, members: [{ finding: {
        side: 'candidate', path: AMOUNT, line_start: 1, severity: 'P1', reference_status: 'verified',
      } }] }],
    });
    await expect(page.getByText('需要返工', { exact: true }).first()).toBeVisible();
    await page.getByRole('link', { name: `${AMOUNT}:1`, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/snapshots/${snapshot.snapshot_id}/file\\?side=candidate`));
    await expect(page.locator('tr#F1.hot')).toContainText('AMOUNT = 1');
    await page.goBack();
    const log = await checkSessionLog(page, env.url, sessionArtifacts(round, { seat: 1, review_attempt: 1 }), 'codex independent review payload end', true);

    const before = net.posts().length;
    await env.hardRestart();
    await page.goto(`${env.url}/reviews`);
    await page.getByRole('list', { name: '快照历史' }).locator(`a[href="/snapshots/${snapshot.snapshot_id}"]`).click();
    await expect(page).toHaveURL(new RegExp(`/snapshots/${snapshot.snapshot_id}$`));
    await page.getByRole('list', { name: '审核历史' }).locator(`a[href="/reviews/${round.data.round_id}"]`).click();
    await expect(page).toHaveURL(`${env.url}/reviews/${round.data.round_id}`);
    await page.goto(`${env.url}/reviews/${round.data.round_id}`);
    await expect(page.getByText('Amount blocker on the candidate side', { exact: true }).first()).toBeVisible();
    expect(net.posts().length).toBe(before);

    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await record(net, scenario, {
      result: 'passed', snapshot_id: snapshot.snapshot_id, round_id: round.data.round_id,
      baseline: snapshot.input.baseline, candidate, outcome: round.data.assessment?.status, session_log: log,
    });
  } catch (error) {
    await record(net, scenario, { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});

test('粘贴补丁双审仅补失败席，双侧定位并从同一父轮交叉确认', async ({ page }) => {
  const env = await OfflineEnv.start('standalone-review');
  const net = new NetworkLog(page);
  const scenario = 'standalone-review-patch';
  try {
    await beginSnapshot(page, env.url, '粘贴补丁');
    await page.getByLabel('补丁原文', { exact: true }).fill(PATCH);
    const snapshot = await freeze(page, env.url, { diff: PATCH });
    expect(snapshot.input).toMatchObject({ baseline: null, candidate: null, diff: PATCH, files: [], tests: [] });
    const raw = page.locator('details').filter({ hasText: '补丁原文' });
    await raw.getByText('补丁原文', { exact: true }).click();
    expect(await raw.locator('pre').textContent()).toBe(PATCH);
    const parent = await startReview(page, env.url, snapshot, [CODEX, CLAUDE]);
    const [codex, failed] = parent.data.seats;
    expect(codex!.attempts).toMatchObject([{ number: 1, status: 'succeeded' }]);
    expect(failed!.attempts).toMatchObject([{ number: 1, status: 'failed', failure_category: 'model_unavailable' }]);
    expect(parent.data.assessment?.status).toBe('incomplete');

    const submitted = jobRequest(page, 'review.retry');
    await act(page, `补跑失败的席位（${failed!.key}）`);
    expect((await submitted).postDataJSON().args).toEqual({ round_id: parent.data.round_id, seat_keys: [failed!.key] });
    const repaired = await roundAt(page, env.url, parent.data.round_id);
    expect(repaired.data.snapshot_id).toBe(snapshot.snapshot_id);
    expect(repaired.data.seats[0]).toEqual(codex);
    expect(repaired.data.seats[1]!.attempts).toMatchObject([
      { number: 1, status: 'failed' }, { number: 2, status: 'succeeded' },
    ]);
    expect(repaired.data.seats[1]!.attempts[0]).toEqual(failed!.attempts[0]);
    expect(repaired.data.assessment?.status).toBe('user_decision');
    expect(repaired.data.assessment?.reasons).toEqual(expect.arrayContaining(['report_only_input', 'blocking_findings_present']));
    const findings = repaired.data.assessment!.groups.flatMap((group) => group.members.map((member) => member.finding));
    expect(findings).toMatchObject([
      { side: 'baseline', path: AMOUNT, line_start: 3, quote: 'AMOUNT = 0', reference_status: 'unverified' },
      { side: 'candidate', path: AMOUNT, line_start: 5, quote: 'AMOUNT = 1', reference_status: 'unverified' },
    ]);
    for (const finding of findings) {
      await page.getByRole('link', { name: `${AMOUNT}:${finding.line_start}`, exact: true }).click();
      const query = new URLSearchParams({ side: finding.side, path: finding.path, line: String(finding.line_start), end: String(finding.line_end) });
      await expect(page).toHaveURL(`${env.url}/reviews/${parent.data.round_id}?${query}`);
      const selected = page.locator('tr[aria-current="location"]');
      await expect(selected).toContainText(finding.quote);
      await expect(selected).toBeFocused();
    }
    await expect(page.getByText('需要你判断', { exact: true }).first()).toBeVisible();

    const confirm = jobRequest(page, 'review.confirm');
    await act(page, '发起交叉确认');
    expect((await confirm).postDataJSON().args).toEqual({ round_id: parent.data.round_id });
    const reread = await roundAt(page, env.url, parent.data.round_id);
    const childId = reread.data.confirmation_round_id!;
    expect(childId).toMatch(/^[a-f0-9]{32}$/);
    const child = await roundAt(page, env.url, childId);
    expect(child.data).toMatchObject({
      snapshot_id: snapshot.snapshot_id,
      metadata: { kind: 'confirmation', parent_round_id: parent.data.round_id },
    });
    expect(child.data.seats).toHaveLength(2);
    expect(child.data.seats.map((seat) => seat.attempts.map((attempt) => attempt.status))).toEqual([['succeeded'], ['succeeded']]);
    expect(reread.data).toMatchObject({
      seats: repaired.data.seats,
      assessment: { status: 'user_decision' },
      confirmations: [
        { seat: codex!.key, target_seat: failed!.key, finding_id: findings[1]!.id, decision: 'agree' },
        { seat: failed!.key, target_seat: codex!.key, finding_id: findings[0]!.id, decision: 'agree' },
      ],
      unanswered_confirmations: [],
    });

    await page.goto(`${env.url}/reviews/${parent.data.round_id}`);
    const logs = [];
    for (const attempt of [1, 2]) {
      logs.push(await checkSessionLog(page, env.url, sessionArtifacts(reread, { seat: 2, review_attempt: attempt }), 'claude independent review payload end', attempt === 2));
    }
    await page.getByRole('link', { name: '查看交叉确认', exact: true }).click();
    await expect(page).toHaveURL(`${env.url}/reviews/${childId}`);
    logs.push(await checkSessionLog(page, env.url, sessionArtifacts(child, { seat: 1, review_attempt: 1 }), 'codex confirmed the frozen patch line.'));
    await net.ready();
    expect(net.lockViolations()).toEqual([]);
    await record(net, scenario, {
      result: 'passed', snapshot_id: snapshot.snapshot_id, round_id: parent.data.round_id,
      confirmation_round_id: childId, retry_seat: failed!.key, findings, session_logs: logs,
    });
  } catch (error) {
    await record(net, scenario, { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});
