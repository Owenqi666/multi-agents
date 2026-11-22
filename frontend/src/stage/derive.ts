// Turn read-back resources into the relay line, following the 12-step table in
// docs/web-api/contract-2026-11-19.md ("接力线的步骤和数据来源"). Only steps that have a
// record are drawn; a direct stage has no merge or acceptance, a single review seat draws
// one line, and the rework loop only exists when a rework attempt exists.

import {
  commitOf,
  nz,
  type Consultation,
  type DelegateStage,
  type DirectStage,
  type Goal,
  type ImplementationAttempt,
  type Leaf,
  type PlanningAttempt,
  type ProjectPlan,
  type ReviewRound,
  type RoleRequest,
  type Stage,
  type TaskTest,
} from '../api/types';
import { integrationPredecessor, type TimelineIntegration } from './integrationLineage';
import { familyOfTone, reportedShort, parseModel, toneFor, UNCONFIRMED } from './models';
import type { Beat, Fork, Holder, Span, Story } from './story';
import { tx, txc } from '../i18n';

export interface StoryInput {
  consultation?: Consultation | null;
  goal?: Goal | null;
  plan?: ProjectPlan | null;
  stage?: Stage | null;
  /** Earlier integrations of the stage's lineage (a rework's source), oldest first. */
  integrationHistory?: TimelineIntegration[];
}

const shortId = (id: string | null | undefined, n = 7): string => (id ? id.slice(0, n) : '');
const last = <T>(xs: readonly T[] | null | undefined): T | null => (xs && xs.length ? (xs[xs.length - 1] ?? null) : null);

const YOU = (): Holder => plainHolder('you', tx('你'), 'you', 'human');
const BENCH = (): Holder => plainHolder('bench', tx('工作台'), 'bench', 'system');

function plainHolder(key: string, name: string, tone: 'you' | 'bench', family: 'human' | 'system'): Holder {
  return {
    key, family, tone, name, sub: '', role: name, plain: true,
    reportedModel: null, reportedEffort: null, requestedModel: null, requestedEffort: null,
  };
}

/** A model seat. Station text uses only reported values; the request is shown apart. */
function modelHolder(
  key: string,
  role: string,
  request: RoleRequest | null,
  reported: { model: string | null; effort: string | null } | null,
): Holder {
  const reportedModel = nz(reported?.model);
  const reportedEffort = nz(reported?.effort);
  const requestedModel = request?.model ?? null;
  const tone = toneFor(reportedModel, requestedModel, request?.provider ?? null);
  let sub: string;
  if (reportedModel === null) sub = requestedModel ? tx('请求 {0}', parseModel(requestedModel)?.short ?? requestedModel) : '';
  else sub = reportedEffort ?? tx('档位{0}', tx(UNCONFIRMED));
  return {
    key, role, tone, family: familyOfTone(tone), plain: false,
    name: reportedShort(reportedModel), sub,
    reportedModel, reportedEffort, requestedModel, requestedEffort: request?.effort ?? null,
  };
}

const fromRuntime = (a: { runtime: PlanningAttempt['runtime'] } | null) =>
  a?.runtime ? { model: a.runtime.reported_model, effort: a.runtime.reported_effort } : null;

function implementationHolder(key: string, role: string, attempt: ImplementationAttempt): Holder {
  return modelHolder(key, role, attempt.request.decision.choice, fromRuntime(attempt));
}

function reviewBeat(id: string, title: string, round: ReviewRound, firstRound: boolean, candidate?: string | null): Beat {
  const holders = round.seats.map((seat) => {
    const attempt = last(seat.attempts);
    return modelHolder(
      seat.key,
      seat.key,
      { provider: seat.provider, model: seat.requested_model, effort: seat.requested_effort },
      attempt ? { model: attempt.reported_model, effort: attempt.reported_effort } : null,
    );
  });
  const fork: Fork = holders.length > 1 ? 'blind' : null;
  const blocking = blockingCount(round);
  const verdict = round.assessment?.status;
  const opening = holders.length > 1
    ? tx('同一份冻结快照同时交给两位审核者，互相看不到对方的意见。')
    : tx('冻结快照交给一位审核者。');
  const outcome = verdict === 'ready' ? tx('结论：通过。')
    : verdict === 'needs_rework' ? tx('结论：需要返工，{0} 条阻塞意见。', blocking)
    : verdict === 'user_decision' ? tx('结论：需要你判断。')
    : tx('审核进行中。');
  return {
    id, step: 10, title, phase: tx('测试与审核'), holders, fork,
    carry: tx('快照 {0}', shortId(round.snapshot_id)),
    note: (firstRound ? '' : tx('在新的测试回执上重新冻结候选。')) + opening
      + (candidate ? tx('候选 {0}。', shortId(candidate)) : '') + outcome,
  };
}

function blockingCount(round: ReviewRound | null): number {
  if (!round?.assessment) return 0;
  return round.assessment.groups.filter((g) => g.blocking).length;
}

function testBeat(id: string, title: string, test: { candidate: string | null; passed: boolean; commands: number | null }, retest: boolean): Beat {
  const count = test.commands === null ? tx('登记的测试命令') : tx('登记的 {0} 条测试命令', test.commands);
  return {
    id, step: 9, title, phase: tx('测试与审核'), holders: [BENCH()], fork: null,
    carry: `${retest ? tx('修订候选') : tx('候选')} ${shortId(test.candidate)}`,
    note: tx('工作台运行{0}，不用模型。{1}', count, test.passed ? tx('全部通过。') : tx('有命令没有通过。')),
  };
}

const testPassed = (t: TaskTest) => t.verification === 'verified' && t.commands.every((c) => c.status === 'passed');

export function deriveStory(input: StoryInput): Story {
  const beats: Beat[] = [];
  const loops: Span[] = [];
  const { consultation, goal, plan, stage } = input;

  // 1 需求顾问: one or two seats in sequence; the second reads the first.
  for (const seat of consultation?.seats ?? []) {
    if (seat.status === 'pending') continue;
    const evidence = seat.evidence ? { model: nz(seat.evidence.reported_model), effort: nz(seat.evidence.reported_effort) } : null;
    beats.push({
      id: `advisor-${seat.number}`, step: 1, title: txc('step', '需求顾问'), phase: tx('目标'),
      holders: [modelHolder(`advisor-${seat.number}`, tx('顾问 {0}', seat.number), seat.request, evidence)],
      fork: null,
      carry: seat.number === 1 ? tx('原始目标') : tx('第一位意见'),
      note: seat.number === 1
        ? tx('第一位顾问读原始目标，写出建议和目标草稿。')
        : tx('第二位顾问读到第一位的意见，再给出自己的建议。依次进行，不是盲审。'),
    });
  }

  // 2 确认目标
  if (goal) {
    const adoption = goal.consultation?.adoption;
    beats.push({
      id: 'confirm', step: 2, title: txc('step', '确认目标'), phase: tx('目标'), holders: [YOU()], fork: null,
      carry: consultation ? tx('目标草稿') : tx('原始目标'),
      note: adoption === 'accepted' ? tx('你原样采纳了顾问的草稿，确认为项目目标。')
        : adoption === 'edited' ? tx('你改过顾问的草稿，再确认为项目目标。')
        : tx('你直接确认了项目目标。'),
    });
  }

  // 3 项目计划: a model, or you when the plan is a hand revision.
  if (plan) {
    const byYou = plan.origin === 'pm';
    const stages = plan.document?.stages.length ?? 0;
    beats.push({
      id: 'plan', step: 3, title: txc('step', '项目计划'), phase: tx('计划'),
      holders: [byYou ? YOU() : modelHolder('planner', tx('规划'), plan.planner_request, plan.evidence ? { model: nz(plan.evidence.reported_model), effort: nz(plan.evidence.reported_effort) } : null)],
      fork: null, carry: tx('确认的目标'),
      note: byYou ? tx('你手改了计划，保存为新的版本。') : tx('规划者把目标拆成 {0} 个阶段，写明依赖和验收条件。', stages),
    });
  }

  if (stage) {
    const stageTitle = plan?.document?.stages.find((s) => s.stage_id === stage.stage_id)?.title;
    const stageName = stageTitle ? tx('「{0}」', stageTitle) : stage.stage_id;
    // 4 领取阶段
    beats.push({
      id: 'claim', step: 4, title: txc('step', '领取阶段'), phase: tx('阶段'), holders: [YOU()], fork: null,
      carry: plan ? tx('项目计划') : tx('阶段 {0}', stage.stage_id),
      note: tx('你领取了阶段{0}，工作台确认前置阶段都已审核通过。', stageName)
        + (stage.mode === 'direct' ? tx('直接模式：Sol 拟一张卡，自己实施。') : tx('分派模式：Sol 拆成子卡，交给 Terra 和 Sonnet。')),
    });
    if (stage.mode === 'direct') directStage(stage, beats, loops);
    else delegateStage(stage, beats, loops, input.integrationHistory ?? []);
  }

  return { beats, phases: phaseSpans(beats), loops };
}

function draftBeat(stage: Stage): Beat | null {
  const draft = last(stage.planning.attempts.draft);
  if (!draft) return null;
  const split = stage.planning.planning_kind === 'stage_split';
  const leaves = stage.mode === 'delegate' ? stage.leaves.length : 0;
  return {
    id: 'draft', step: 5, title: split ? txc('step', '拆分') : txc('step', '拟卡'), phase: tx('阶段'),
    holders: [modelHolder('sol', tx('阶段负责人'), draft.request.request, fromRuntime(draft))],
    fork: null, carry: tx('阶段 {0}', stage.stage_id),
    note: split ? tx('阶段负责人把阶段拆成 {0} 张子卡，写明依赖和写入范围。', leaves) : tx('阶段负责人把阶段写成一张任务卡：目标、写入范围和验收条件。'),
  };
}

function cardReviewNote(attempt: PlanningAttempt | null): string {
  const result = attempt?.runtime?.result as { decision?: string } | undefined;
  if (result?.decision === 'ready') return tx('复核者对照阶段目标检查卡片，结论：可以实施。');
  if (result?.decision === 'needs_revision') return tx('复核者要求修改卡片。');
  return tx('复核者对照阶段目标检查卡片。');
}

function directStage(stage: DirectStage, beats: Beat[], loops: Span[]): void {
  const draft = draftBeat(stage);
  if (!draft) return;
  beats.push(draft);

  // 6 卡片复核
  const review = last(stage.planning.attempts.review);
  if (!review) return;
  beats.push({
    id: 'card-review', step: 6, title: txc('step', '卡片复核'), phase: tx('阶段'),
    holders: [modelHolder('card-review', tx('卡片复核'), review.request.request, fromRuntime(review))],
    fork: null, carry: tx('任务卡草案'), note: cardReviewNote(review),
  });

  // 7 实施, then 9 测试, 10 代码审核, and at most one 11 返工 that sends the line back to 9.
  const task = stage.implementation;
  const first = task?.attempts.find((a) => a.request.kind === 'task_attempt_started');
  if (!task || !first) return;
  beats.push({
    id: 'implement', step: 7, title: txc('step', '实施'), phase: tx('阶段'),
    holders: [implementationHolder('impl', tx('实施'), first)], fork: null, carry: tx('复核后的卡'),
    note: tx('在独立的 worktree 里按卡实施，产出候选提交。'),
  });

  const rounds = task.reviews.filter((r) => r.metadata.kind === 'review');
  const reworkIndex = task.attempts.findIndex((a) => a.request.kind === 'task_rework_started');
  const attemptNumbers = reworkIndex >= 0 ? [1, reworkIndex + 1] : [1];

  attemptNumbers.forEach((n, pass) => {
    if (pass === 1) {
      const rework = task.attempts[reworkIndex];
      const source = rounds.find((r) => r.round_id === rework?.request.source?.review_round_id) ?? last(rounds.filter((r) => r.implementation_attempt === 1));
      if (!rework) return;
      const testIndex = beats.findIndex((b) => b.id === 'test-1');
      beats.push({
        id: 'rework', step: 11, title: txc('step', '返工'), phase: tx('测试与审核'),
        holders: [implementationHolder('impl', tx('原实施者'), rework)], fork: null,
        carry: tx('{0} 条阻塞意见', blockingCount(source)),
        note: tx('原实施者按阻塞意见修改，返工最多一次。改完回到测试。'),
      });
      if (testIndex >= 0) loops.push({ from: testIndex, to: beats.length - 1, label: tx('返工后回到测试') });
    }
    const test = task.tests.find((t) => t.implementation_attempt === n);
    if (!test) return;
    beats.push(testBeat(`test-${pass + 1}`, pass ? tx('复测') : tx('测试'), { candidate: test.candidate, passed: testPassed(test), commands: test.commands.length }, pass === 1));
    const round = last(rounds.filter((r) => r.implementation_attempt === n));
    if (round) beats.push(reviewBeat(`review-${pass + 1}`, pass ? tx('复审') : tx('代码审核'), round, pass === 0));
  });
}

function leafLabel(index: number): string {
  return tx('子卡 {0}', String.fromCharCode(65 + index));
}

function delegateStage(stage: DelegateStage, beats: Beat[], loops: Span[], history: TimelineIntegration[]): void {
  const draft = draftBeat(stage);
  if (!draft) return;
  beats.push(draft);

  // 6 卡片复核 per leaf, side by side; the parent split has no card review.
  const claimed = stage.leaves.map((leaf, i) => ({ leaf, i })).filter(({ leaf }) => nz(leaf.planning) !== null);
  const reviewed = claimed.filter(({ leaf }) => last(leaf.planning?.attempts.review) !== null);
  if (reviewed.length) {
    beats.push({
      id: 'card-review', step: 6, title: txc('step', '卡片复核'), phase: tx('阶段'),
      holders: reviewed.map(({ leaf, i }) => {
        const attempt = last(leaf.planning?.attempts.review)!;
        return modelHolder(leaf.card_id, leafLabel(i), attempt.request.request, fromRuntime(attempt));
      }),
      fork: reviewed.length > 1 ? 'parallel' : null, carry: tx('子卡草案'),
      note: tx('每张子卡各自复核，复核者沿用阶段的设置。{0}', reviewed.every(({ leaf }) => leaf.review_ready) ? tx('都可以实施。') : ''),
    });
  }

  // 7 实施 per leaf, executor taken from the leaf's frozen draft_projection.
  const implemented = claimed.filter(({ leaf }) => (nz(leaf.implementation)?.attempts.length ?? 0) > 0);
  if (!implemented.length) return;
  beats.push({
    id: 'implement', step: 7, title: txc('step', '实施'), phase: tx('阶段'),
    holders: implemented.map(({ leaf, i }) => leafImplementer(leaf, i)),
    fork: implemented.length > 1 ? 'parallel' : null, carry: tx('复核后的子卡'),
    note: tx('子卡互不依赖时并行实施，各自在独立的 worktree 里产出候选。'),
  });

  // 8 合并
  const integration = stage.integration;
  if (!integration) return;
  beats.push({
    id: 'merge', step: 8, title: txc('step', '合并'), phase: tx('阶段'), holders: [BENCH()], fork: null,
    carry: tx('{0} 个候选', implemented.length),
    note: tx('工作台把 {0} 张子卡的候选合并成一个集成候选。', implemented.length),
  });

  // 9 测试, 10 代码审核 and 11 返工 per integration. A successful rework makes a child
  // integration and the stage then reports the child, so earlier integrations of the same
  // lineage come in through integrationHistory, oldest first.
  [...history, integration].forEach((item, k) => {
    const evidence = item.evidence;
    if (!evidence) return;
    const revised = k > 0 || integrationPredecessor({ integration_id: item.integration_id, ...evidence }) !== null;
    const test = evidence.test;
    const testId = `test-${item.integration_id}`;
    const candidate = commitOf(test?.candidate) ?? item.candidate;
    if (test) {
      beats.push(testBeat(testId, revised ? tx('复测') : tx('测试'), { candidate, passed: test.status === 'succeeded', commands: null }, revised));
    }
    const rework = evidence.rework?.source_id === item.integration_id ? evidence.rework : null;
    const pinnedRound = rework?.request?.review_round_id ?? evidence.acceptance?.review_round_id;
    const reviews = evidence.reviews.filter((r) => r.metadata.kind === 'review');
    const round = pinnedRound
      ? reviews.find((r) => r.round_id === pinnedRound) ?? null
      : last(reviews.filter((r) => test && (r.test_attempt == null || r.test_attempt === test.attempt)));
    if (round) beats.push(reviewBeat(`review-${item.integration_id}`, revised ? tx('复审') : tx('代码审核'), round, !revised, candidate));
    if (rework) {
      const testIndex = beats.findIndex((b) => b.id === testId);
      const choice = rework.request?.requested_actor?.decision?.choice ?? null;
      const reported = rework.result?.evidence;
      const findings = rework.request?.findings?.length ?? (round ? blockingCount(round) : null);
      beats.push({
        id: `rework-${item.integration_id}`, step: 11, title: txc('step', '返工'), phase: tx('测试与审核'),
        holders: [modelHolder('impl', tx('原实施者'), choice, reported ? { model: nz(reported.reported_model), effort: nz(reported.reported_effort) } : null)], fork: null,
        carry: findings === null ? tx('阻塞意见') : tx('{0} 条阻塞意见', findings),
        note: tx('原实施者按阻塞意见修改，成功后生成一个子集成，回到测试。'),
      });
      if (testIndex >= 0) loops.push({ from: testIndex, to: beats.length - 1, label: tx('返工后回到测试') });
    }
  });

  // 12 接受
  const acceptance = integration.evidence?.acceptance;
  if (acceptance) {
    const acceptedCandidate = commitOf(acceptance.candidate) ?? integration.candidate;
    beats.push({
      id: 'accept', step: 12, title: txc('step', '接受'), phase: tx('接受'), holders: [YOU()], fork: null,
      carry: acceptedCandidate ? tx('候选 {0}', shortId(acceptedCandidate)) : tx('审核通过的集成'),
      note: tx('你接受了审核通过的集成，阶段交付完成。'),
    });
  }
}

function leafImplementer(leaf: Leaf, index: number): Holder {
  const attempt = last(nz(leaf.implementation)?.attempts);
  const executor = nz(leaf.planning)?.draft_projection?.executor ?? attempt?.request.decision.choice ?? null;
  return modelHolder(leaf.card_id, leafLabel(index), executor, fromRuntime(attempt));
}

function phaseSpans(beats: Beat[]): Span[] {
  const spans: Span[] = [];
  beats.forEach((b, i) => {
    const prev = spans[spans.length - 1];
    if (prev && prev.label === b.phase) prev.to = i;
    else spans.push({ from: i, to: i, label: b.phase });
  });
  return spans;
}

// ---- While a job runs ----------------------------------------------------------------
// Locking records cannot be read during a job, so the stage shows where the handoff is from
// the job's action alone, with the requested seats from its args. Nothing is reported yet,
// so every model reads 未确认. After the job the stage is read again and redrawn.

type JobLike = { action: string; args?: Record<string, unknown> | null };

const asRequest = (v: unknown): RoleRequest | null => {
  const r = v as Partial<RoleRequest> | null | undefined;
  return r && typeof r.model === 'string' ? { provider: String(r.provider ?? ''), model: r.model, effort: String(r.effort ?? '') } : null;
};

export function runningBeat(job: JobLike): Beat | null {
  const a = job.args ?? {};
  const seat = (key: string, role: string, req: unknown): Holder => modelHolder(key, role, asRequest(req), null);
  const beat = (step: number, title: string, holders: Holder[], fork: Fork = null): Beat => ({
    id: `running-${job.action}`, step, title, phase: tx('进行中'), holders, fork, carry: null,
    note: tx('作业「{0}」正在进行。结束后读回记录，再补画中间各步。', title),
  });
  switch (job.action) {
    case 'project.consult': {
      const advisors = (a.advisors as unknown[] | undefined) ?? [];
      return beat(1, txc('step', '需求顾问'), [seat('advisor-1', tx('顾问 1'), advisors[0])]);
    }
    case 'project.confirm': return beat(2, txc('step', '确认目标'), [YOU()]);
    case 'project.plan': return beat(3, txc('step', '项目计划'), [seat('planner', tx('规划'), a.planner)]);
    case 'project.revise': return beat(3, txc('step', '修订计划'), [YOU()]);
    case 'project.stage.start': return beat(5, txc('step', '拟卡、复核和实施'), [seat('sol', tx('阶段负责人'), a.sol ? { provider: 'codex', ...(a.sol as object) } : null)]);
    case 'project.stage.resume': return beat(5, txc('step', '恢复阶段'), [seat('sol', tx('阶段负责人'), null)]);
    case 'project.stage.leaf': return beat(6, txc('step', '子卡复核'), [seat('card-review', tx('卡片复核'), null)]);
    case 'task.run': case 'task.retry': case 'task.run_batch':
      return beat(7, txc('step', '实施'), [seat('impl', tx('实施'), a.implementation)]);
    case 'project.stage.integrate': case 'integration.create': case 'integration.rebase':
      return beat(8, txc('step', '合并'), [BENCH()]);
    case 'task.test': case 'integration.test': return beat(9, txc('step', '测试'), [BENCH()]);
    case 'task.review': case 'integration.review': {
      const seats = ((a.seats as unknown[] | undefined) ?? []).map((r, i) => seat(`seat-${i + 1}`, tx('审核席位 {0}', i + 1), r));
      return beat(10, txc('step', '代码审核'), seats.length ? seats : [seat('seat-1', tx('审核席位'), null)], seats.length > 1 ? 'blind' : null);
    }
    case 'task.retry_review': case 'integration.retry_review': {
      const keys = (a.seat_keys as string[] | undefined) ?? ['seat'];
      return beat(10, txc('step', '补跑审核'), keys.map((k) => seat(k, k, null)), keys.length > 1 ? 'blind' : null);
    }
    case 'task.confirm': case 'integration.confirm': return beat(10, txc('step', '交叉确认'), [seat('confirm', tx('审核席位'), null)]);
    case 'task.rework': case 'integration.rework': return beat(11, txc('step', '返工'), [seat('impl', tx('原实施者'), null)]);
    case 'integration.accept': return beat(12, txc('step', '接受'), [YOU()]);
    default: return null;
  }
}

export function withRunningJob(story: Story, job: JobLike | null): Story {
  const extra = job ? runningBeat(job) : null;
  if (!extra) return story;
  const beats = [...story.beats, extra];
  return { beats, phases: phaseSpans(beats), loops: story.loops };
}
