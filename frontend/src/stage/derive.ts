// Turn read-back resources into the relay line, following the 12-step table in
// docs/web-api/contract-2026-11-19.md ("接力线的步骤和数据来源"). Only steps that have a
// record are drawn; a direct stage has no merge or acceptance, a single review seat draws
// one line, and the rework loop only exists when a rework attempt exists.

import {
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
  type StageIntegration,
  type TaskTest,
} from '../api/types';
import { familyOfTone, reportedShort, parseModel, toneFor, UNCONFIRMED } from './models';
import type { Beat, Fork, Holder, Span, Story } from './story';

export interface StoryInput {
  consultation?: Consultation | null;
  goal?: Goal | null;
  plan?: ProjectPlan | null;
  stage?: Stage | null;
  /** Earlier integrations of the stage's lineage (a rework's source), oldest first. */
  integrationHistory?: StageIntegration[];
}

const shortId = (id: string | null | undefined, n = 7): string => (id ? id.slice(0, n) : '');
const last = <T>(xs: readonly T[] | null | undefined): T | null => (xs && xs.length ? (xs[xs.length - 1] ?? null) : null);

const YOU: Holder = plainHolder('you', '你', 'you', 'human');
const BENCH: Holder = plainHolder('bench', '工作台', 'bench', 'system');

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
  if (reportedModel === null) sub = requestedModel ? `请求 ${parseModel(requestedModel)?.short ?? requestedModel}` : '';
  else sub = reportedEffort ?? `档位${UNCONFIRMED}`;
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

function reviewBeat(id: string, title: string, round: ReviewRound, firstRound: boolean): Beat {
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
    ? '同一份冻结快照同时交给两位审核者，互相看不到对方的意见。'
    : '冻结快照交给一位审核者。';
  const outcome = verdict === 'ready' ? '结论：通过。'
    : verdict === 'needs_rework' ? `结论：需要返工，${blocking} 条阻塞意见。`
    : verdict === 'user_decision' ? '结论：需要你判断。'
    : '审核进行中。';
  return {
    id, step: 10, title, phase: '测试与审核', holders, fork,
    carry: `快照 ${shortId(round.snapshot_id)}`,
    note: (firstRound ? opening : '返工后的候选重新冻结，再审一次。') + outcome,
  };
}

function blockingCount(round: ReviewRound | null): number {
  if (!round?.assessment) return 0;
  return round.assessment.groups.filter((g) => g.blocking).length;
}

function testBeat(id: string, title: string, test: { candidate: string | null; passed: boolean; commands: number | null }, retest: boolean): Beat {
  const count = test.commands === null ? '登记的测试命令' : `登记的 ${test.commands} 条测试命令`;
  return {
    id, step: 9, title, phase: '测试与审核', holders: [BENCH], fork: null,
    carry: `${retest ? '修订候选' : '候选'} ${shortId(test.candidate)}`,
    note: `工作台运行${count}，不用模型。${test.passed ? '全部通过。' : '有命令没有通过。'}`,
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
      id: `advisor-${seat.number}`, step: 1, title: '需求顾问', phase: '目标',
      holders: [modelHolder(`advisor-${seat.number}`, `顾问 ${seat.number}`, seat.request, evidence)],
      fork: null,
      carry: seat.number === 1 ? '原始目标' : '第一位意见',
      note: seat.number === 1
        ? '第一位顾问读原始目标，写出建议和目标草稿。'
        : '第二位顾问读到第一位的意见，再给出自己的建议。依次进行，不是盲审。',
    });
  }

  // 2 确认目标
  if (goal) {
    const adoption = goal.consultation?.adoption;
    beats.push({
      id: 'confirm', step: 2, title: '确认目标', phase: '目标', holders: [YOU], fork: null,
      carry: consultation ? '目标草稿' : '原始目标',
      note: adoption === 'accepted' ? '你原样采纳了顾问的草稿，确认为项目目标。'
        : adoption === 'edited' ? '你改过顾问的草稿，再确认为项目目标。'
        : '你直接确认了项目目标。',
    });
  }

  // 3 项目计划: a model, or you when the plan is a hand revision.
  if (plan) {
    const byYou = plan.origin === 'pm';
    const stages = plan.document?.stages.length ?? 0;
    beats.push({
      id: 'plan', step: 3, title: '项目计划', phase: '计划',
      holders: [byYou ? YOU : modelHolder('planner', '规划', plan.planner_request, plan.evidence ? { model: nz(plan.evidence.reported_model), effort: nz(plan.evidence.reported_effort) } : null)],
      fork: null, carry: '确认的目标',
      note: byYou ? '你手改了计划，保存为新的版本。' : `规划者把目标拆成 ${stages} 个阶段，写明依赖和验收条件。`,
    });
  }

  if (stage) {
    const stageTitle = plan?.document?.stages.find((s) => s.stage_id === stage.stage_id)?.title;
    const stageName = stageTitle ? `「${stageTitle}」` : stage.stage_id;
    // 4 领取阶段
    beats.push({
      id: 'claim', step: 4, title: '领取阶段', phase: '阶段', holders: [YOU], fork: null,
      carry: plan ? '项目计划' : `阶段 ${stage.stage_id}`,
      note: `你领取了阶段${stageName}，工作台确认前置阶段都已审核通过。`
        + (stage.mode === 'direct' ? '直接模式：Sol 拟一张卡，自己实施。' : '分派模式：Sol 拆成子卡，交给 Terra 和 Sonnet。'),
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
    id: 'draft', step: 5, title: split ? '拆分' : '拟卡', phase: '阶段',
    holders: [modelHolder('sol', '阶段负责人', draft.request.request, fromRuntime(draft))],
    fork: null, carry: `阶段 ${stage.stage_id}`,
    note: split ? `阶段负责人把阶段拆成 ${leaves} 张子卡，写明依赖和写入范围。` : '阶段负责人把阶段写成一张任务卡：目标、写入范围和验收条件。',
  };
}

function cardReviewNote(attempt: PlanningAttempt | null): string {
  const result = attempt?.runtime?.result as { decision?: string } | undefined;
  if (result?.decision === 'ready') return '复核者对照阶段目标检查卡片，结论：可以实施。';
  if (result?.decision === 'needs_revision') return '复核者要求修改卡片。';
  return '复核者对照阶段目标检查卡片。';
}

function directStage(stage: DirectStage, beats: Beat[], loops: Span[]): void {
  const draft = draftBeat(stage);
  if (!draft) return;
  beats.push(draft);

  // 6 卡片复核
  const review = last(stage.planning.attempts.review);
  if (!review) return;
  beats.push({
    id: 'card-review', step: 6, title: '卡片复核', phase: '阶段',
    holders: [modelHolder('card-review', '卡片复核', review.request.request, fromRuntime(review))],
    fork: null, carry: '任务卡草案', note: cardReviewNote(review),
  });

  // 7 实施, then 9 测试, 10 代码审核, and at most one 11 返工 that sends the line back to 9.
  const task = stage.implementation;
  const first = task?.attempts.find((a) => a.request.kind === 'task_attempt_started');
  if (!task || !first) return;
  beats.push({
    id: 'implement', step: 7, title: '实施', phase: '阶段',
    holders: [implementationHolder('impl', '实施', first)], fork: null, carry: '复核后的卡',
    note: '在独立的 worktree 里按卡实施，产出候选提交。',
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
        id: 'rework', step: 11, title: '返工', phase: '测试与审核',
        holders: [implementationHolder('impl', '原实施者', rework)], fork: null,
        carry: `${blockingCount(source)} 条阻塞意见`,
        note: '原实施者按阻塞意见修改，返工最多一次。改完回到测试。',
      });
      if (testIndex >= 0) loops.push({ from: testIndex, to: beats.length - 1, label: '返工后回到测试' });
    }
    const test = task.tests.find((t) => t.implementation_attempt === n);
    if (!test) return;
    beats.push(testBeat(`test-${pass + 1}`, pass ? '复测' : '测试', { candidate: test.candidate, passed: testPassed(test), commands: test.commands.length }, pass === 1));
    const round = last(rounds.filter((r) => r.implementation_attempt === n));
    if (round) beats.push(reviewBeat(`review-${pass + 1}`, pass ? '复审' : '代码审核', round, pass === 0));
  });
}

function leafLabel(index: number): string {
  return `子卡 ${String.fromCharCode(65 + index)}`;
}

function delegateStage(stage: DelegateStage, beats: Beat[], loops: Span[], history: StageIntegration[]): void {
  const draft = draftBeat(stage);
  if (!draft) return;
  beats.push(draft);

  // 6 卡片复核 per leaf, side by side; the parent split has no card review.
  const claimed = stage.leaves.map((leaf, i) => ({ leaf, i })).filter(({ leaf }) => nz(leaf.planning) !== null);
  const reviewed = claimed.filter(({ leaf }) => last(leaf.planning?.attempts.review) !== null);
  if (reviewed.length) {
    beats.push({
      id: 'card-review', step: 6, title: '卡片复核', phase: '阶段',
      holders: reviewed.map(({ leaf, i }) => {
        const attempt = last(leaf.planning?.attempts.review)!;
        return modelHolder(leaf.card_id, leafLabel(i), attempt.request.request, fromRuntime(attempt));
      }),
      fork: reviewed.length > 1 ? 'parallel' : null, carry: '子卡草案',
      note: `每张子卡各自复核，复核者沿用阶段的设置。${reviewed.every(({ leaf }) => leaf.review_ready) ? '都可以实施。' : ''}`,
    });
  }

  // 7 实施 per leaf, executor taken from the leaf's frozen draft_projection.
  const implemented = claimed.filter(({ leaf }) => (nz(leaf.implementation)?.attempts.length ?? 0) > 0);
  if (!implemented.length) return;
  beats.push({
    id: 'implement', step: 7, title: '实施', phase: '阶段',
    holders: implemented.map(({ leaf, i }) => leafImplementer(leaf, i)),
    fork: implemented.length > 1 ? 'parallel' : null, carry: '复核后的子卡',
    note: '子卡互不依赖时并行实施，各自在独立的 worktree 里产出候选。',
  });

  // 8 合并
  const integration = stage.integration;
  if (!integration) return;
  beats.push({
    id: 'merge', step: 8, title: '合并', phase: '阶段', holders: [BENCH], fork: null,
    carry: `${implemented.length} 个候选`,
    note: `工作台把 ${implemented.length} 张子卡的候选合并成一个集成候选。`,
  });

  // 9 测试, 10 代码审核 and 11 返工 per integration. A successful rework makes a child
  // integration and the stage then reports the child, so earlier integrations of the same
  // lineage come in through integrationHistory, oldest first.
  const chain = [...history, integration];
  let lastEvidence: NonNullable<StageIntegration['evidence']> | null = null;
  chain.forEach((item, k) => {
    const evidence = item.evidence;
    const test = evidence?.test;
    if (!evidence || !test) return;
    lastEvidence = evidence;
    beats.push(testBeat(`test-${k + 1}`, k ? '复测' : '测试', { candidate: test.candidate, passed: test.status === 'succeeded', commands: null }, k > 0));
    const round = last(evidence.reviews.filter((r) => r.metadata.kind === 'review'));
    if (round) beats.push(reviewBeat(`review-${k + 1}`, k ? '复审' : '代码审核', round, k === 0));
    const rework = evidence.rework;
    if (rework) {
      const testIndex = beats.findIndex((b) => b.id === `test-${k + 1}`);
      const choice = rework.request.requested_actor?.decision?.choice ?? null;
      beats.push({
        id: `rework-${k + 1}`, step: 11, title: '返工', phase: '测试与审核',
        holders: [modelHolder('impl', '原实施者', choice, null)], fork: null,
        carry: `${blockingCount(round)} 条阻塞意见`,
        note: '原实施者按阻塞意见修改，成功后生成一个子集成，回到测试。返工记录里没有运行时报告的模型。',
      });
      if (testIndex >= 0) loops.push({ from: testIndex, to: beats.length - 1, label: '返工后回到测试' });
    }
  });

  // 12 接受
  if ((lastEvidence as NonNullable<StageIntegration['evidence']> | null)?.acceptance) {
    beats.push({
      id: 'accept', step: 12, title: '接受', phase: '接受', holders: [YOU], fork: null,
      carry: '审核通过的集成', note: '你接受了审核通过的集成，阶段交付完成。',
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
