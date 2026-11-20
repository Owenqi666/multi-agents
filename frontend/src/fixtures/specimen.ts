// Hand-written samples in the shapes of the contract ("接口条件") for the step-0 specimen.
// They stand in for the recorded samples under docs/web-api/samples/ until T-0069 lands;
// the ids, hashes and times are placeholders. reviewer-2 in the first review round reports
// no model on purpose, to show 未确认.

import type {
  ArtifactPage, Consultation, DelegateStage, DirectStage, Goal, Job, ProjectPlan, ReviewRound,
} from '../api/types';

const BASE = '2417007646b03301b65b7a38f1af0b1d13ff8b4f';
const T0 = '2026-11-19T19:02:11Z';

const advice = (draft_goal: string) => ({ draft_goal, background: [], assumptions: [], questions: [], remaining_disagreements: [] });

const astra = { provider: 'codex', model: 'gpt-6.2-astra', effort: 'high' };
const fable = { provider: 'claude', model: 'claude-fable-5-2', effort: 'high' };
const sol = { provider: 'codex', model: 'gpt-6.2-sol', effort: 'high' };
const opus = { provider: 'claude', model: 'claude-opus-5-5', effort: 'high' };

export const consultation: Consultation = {
  kind: 'goal_consultation',
  consultation_id: '7c1e0b5a9d2f4e3a8b6c1d0e9f8a7b6c',
  baseline_commit: BASE,
  original_goal: '在本机网页上看出哪个模型在工作，并能从提出目标一路操作到接受。',
  project_id: null,
  started_at: T0,
  finished_at: '2026-11-19T19:07:40Z',
  status: 'succeeded',
  draft: advice('本机网页工作台：首页是分工舞台，另有从目标到接受的项目流程页面。'),
  seats: [
    {
      consultation_id: '7c1e0b5a9d2f4e3a8b6c1d0e9f8a7b6c', number: 1, request: astra,
      started_at: T0, finished_at: '2026-11-19T19:04:52Z', advice: advice('先做只读舞台，再做操作页。'), error: null,
      evidence: { reported_model: 'gpt-6.2-astra', reported_effort: 'high' }, status: 'succeeded',
    },
    {
      consultation_id: '7c1e0b5a9d2f4e3a8b6c1d0e9f8a7b6c', number: 2, request: fable,
      started_at: '2026-11-19T19:04:53Z', finished_at: '2026-11-19T19:07:40Z', advice: advice('同意，并把作业期间的读取规则放进第一版。'), error: null,
      evidence: { reported_model: 'claude-fable-5-2', reported_effort: 'high' }, status: 'succeeded',
    },
  ],
};

export const goal: Goal = {
  kind: 'confirmed_goal',
  project_id: 'p-4f0c9a1e2b7d',
  goal_id: 'g-91d3e0a7c6b2',
  baseline_commit: BASE,
  original_goal: consultation.original_goal,
  confirmed_goal: '本机网页工作台：首页是分工舞台；项目流程页面覆盖目标、计划、阶段、审核和接受。',
  confirmed_at: '2026-11-19T19:09:03Z',
  consultation: { consultation_id: consultation.consultation_id, adoption: 'edited', source_snapshot_id: 'snap-c0n5' },
};

export const plan: ProjectPlan = {
  kind: 'project_plan',
  plan_id: 'pl-2b8e61f0d4a9',
  goal,
  status: 'succeeded',
  planner_request: astra,
  started_at: '2026-11-19T19:09:20Z',
  finished_at: '2026-11-19T19:13:58Z',
  evidence: { reported_model: 'gpt-6.2-astra', reported_effort: 'high' },
  error: null,
  document: {
    title: '本机网页工作台',
    stages: [
      { stage_id: 'stage-shell', title: '页面骨架和分工舞台', goal: '', depends_on: [] },
      { stage_id: 'job-panel', title: '作业面板', goal: '', depends_on: ['stage-shell'] },
      { stage_id: 'review-pages', title: '审核和 diff 页面', goal: '', depends_on: ['job-panel'] },
    ],
  },
};

const stageRun = {
  plan_id: plan.plan_id,
  execution_baseline: BASE,
  dependencies: [],
  created_at: '2026-11-19T19:15:02Z',
  dispatch_error: null,
};

const finding = {
  id: 'f1',
  severity: 'P1' as const,
  category: 'correctness',
  side: 'candidate' as const,
  path: 'frontend/src/jobs/poll.ts',
  line_start: 39,
  line_end: 40,
  quote: '  if (job.error === null) {\n    markSucceeded(job);',
  title: '把 error 为 null 当成了成功',
  impact: '退出码 1 的作业带着 JSON 结果，error 也是 null。按现在的判断，需要返工的审核会显示成功，按钮也会提前解锁。',
  rationale: '约定要求以 exit_code 和读回的领域字段判断结果。',
  reference_status: 'verified' as const,
};

function round(id: string, snapshot: string, attempt: number, test: number, ready: boolean, astraReported: boolean): ReviewRound {
  return {
    round_id: id,
    snapshot_id: snapshot,
    status: 'ready_for_assessment',
    metadata: { kind: 'review', created_at: '2026-11-19T19:31:00Z', automatic_rework: true, ...(ready ? { rework_from_round_id: 'rr-1a2b3c4d' } : {}) },
    seats: [
      {
        key: 'reviewer-1', provider: 'claude', requested_model: 'claude-opus-5-5', requested_effort: 'high', current_attempt: 1,
        attempts: [{ number: 1, status: 'succeeded', reported_model: 'claude-opus-5-5', reported_effort: 'high' }],
      },
      {
        key: 'reviewer-2', provider: 'codex', requested_model: 'gpt-6.2-astra', requested_effort: 'xhigh', current_attempt: 1,
        attempts: [{
          number: 1, status: 'succeeded',
          reported_model: astraReported ? 'gpt-6.2-astra' : null,
          reported_effort: astraReported ? 'xhigh' : null,
        }],
      },
    ],
    assessment: ready
      ? { status: 'ready', reasons: [], groups: [] }
      : { status: 'needs_rework', reasons: ['blocking_findings_present'], groups: [{ blocking: true, members: [{ seat: 'reviewer-1', finding }] }] },
    implementation_attempt: attempt,
    test_attempt: test,
    candidate: null,
    current_eligible: ready,
  };
}

export const directStage: DirectStage = {
  ...stageRun,
  kind: 'project_stage',
  stage_run_id: 'sr-job-panel-01',
  stage_id: 'job-panel',
  planning_run_id: 'pr-5e7a0c3d',
  mode: 'direct',
  goal_id: goal.goal_id,
  task_run_id: 'tr-8f2d61b0',
  candidate: '7b21d04e3c95a1f0b8d6e2c4a7f91306d5b8e2aa',
  candidate_verified: true,
  tests_passed: true,
  review_satisfied: true,
  test_attempt: 2,
  review_round_id: 'rr-5e6f7a8b',
  planning: {
    planning_run_id: 'pr-5e7a0c3d',
    task_run_id: 'tr-8f2d61b0',
    phase: 'bound',
    verification: 'verified',
    card: { id: 'T-0071' },
    attempts: {
      draft: [{ request: { request: sol, started_at: '2026-11-19T19:15:10Z' }, runtime: { number: 1, status: 'succeeded', reported_model: 'gpt-6.2-sol', reported_effort: 'high' } }],
      review: [{ request: { request: opus, started_at: '2026-11-19T19:18:44Z' }, runtime: { number: 1, status: 'succeeded', reported_model: 'claude-opus-5-5', reported_effort: 'high', result: { decision: 'ready' } } }],
    },
    next_action: null,
  },
  implementation: {
    planning_run_id: 'pr-5e7a0c3d',
    task_run_id: 'tr-8f2d61b0',
    implementation_attempt: 2,
    phase: 'delivered',
    candidate: '7b21d04e3c95a1f0b8d6e2c4a7f91306d5b8e2aa',
    verification: 'verified',
    attempts: [
      { request: { decision: { choice: { ...sol, provider: 'codex' } }, started_at: '2026-11-19T19:20:31Z', kind: 'task_attempt_started' }, runtime: { number: 1, status: 'succeeded', reported_model: 'gpt-6.2-sol', reported_effort: 'high' } },
      { request: { decision: { choice: { ...sol, provider: 'codex' } }, started_at: '2026-11-19T19:38:12Z', kind: 'task_rework_started', source: { review_round_id: 'rr-1a2b3c4d' } }, runtime: { number: 2, status: 'succeeded', reported_model: 'gpt-6.2-sol', reported_effort: 'high' } },
    ],
    tests: [
      { task_run_id: 'tr-8f2d61b0', implementation_attempt: 1, test_attempt: 1, candidate: '3f9a1c2d8e7b6a5f4c3d2e1f0a9b8c7d6e5f4a3b', phase: 'finished', verification: 'verified',
        commands: [{ sequence: 1, name: 'lint', status: 'passed', exit_code: 0 }, { sequence: 2, name: 'unit', status: 'passed', exit_code: 0 }, { sequence: 3, name: 'frontend-check', status: 'passed', exit_code: 0 }] },
      { task_run_id: 'tr-8f2d61b0', implementation_attempt: 2, test_attempt: 2, candidate: '7b21d04e3c95a1f0b8d6e2c4a7f91306d5b8e2aa', phase: 'finished', verification: 'verified',
        commands: [{ sequence: 1, name: 'lint', status: 'passed', exit_code: 0 }, { sequence: 2, name: 'unit', status: 'passed', exit_code: 0 }, { sequence: 3, name: 'frontend-check', status: 'passed', exit_code: 0 }] },
    ],
    reviews: [
      round('rr-1a2b3c4d', '9f3c2e1a7b6d', 1, 1, false, false),
      round('rr-5e6f7a8b', 'c41e0a8d2f93', 2, 2, true, true),
    ],
    rework_used: true,
    next_action: null,
  },
};

const terra = { provider: 'codex', model: 'gpt-6.2-terra', effort: 'high' };
const sonnet = { provider: 'claude-code', model: 'claude-sonnet-5-5', effort: 'high' };

function leafPlanning(id: string, executor: typeof terra) {
  return {
    planning_run_id: id, task_run_id: `t${id}`, phase: 'bound', verification: 'verified', card: { id },
    attempts: {
      draft: [{ request: { request: sol, started_at: T0 }, runtime: { number: 1, status: 'succeeded', reported_model: 'gpt-6.2-sol', reported_effort: 'high' } }],
      review: [{ request: { request: opus, started_at: T0 }, runtime: { number: 1, status: 'succeeded', reported_model: 'claude-opus-5-5', reported_effort: 'high', result: { decision: 'ready' } } }],
    },
    next_action: null,
    draft_projection: { executor },
  };
}

function leafTask(id: string, choice: typeof terra, reported: string) {
  return {
    planning_run_id: `p${id}`, task_run_id: id, implementation_attempt: 1, phase: 'delivered', candidate: 'aa11bb2', verification: 'verified',
    attempts: [{ request: { decision: { choice }, started_at: T0, kind: 'task_attempt_started' as const }, runtime: { number: 1, status: 'succeeded', reported_model: reported, reported_effort: 'high' } }],
    tests: [], reviews: [], rework_used: false, next_action: null,
  };
}

export const delegateStage: DelegateStage = {
  ...stageRun,
  kind: 'project_stage',
  stage_run_id: 'sr-review-pages-01',
  stage_id: 'review-pages',
  planning_run_id: 'pr-d1e2f3a4',
  mode: 'delegate',
  goal_id: goal.goal_id,
  candidate: 'e0c4b19',
  candidate_verified: true,
  tests_passed: true,
  review_satisfied: true,
  test_attempt: 1,
  review_round_id: 'ir-0c9d8e7f',
  split_verified: true,
  can_integrate: true,
  planning: {
    planning_run_id: 'pr-d1e2f3a4', task_run_id: null, phase: 'ready', verification: 'verified', card: null,
    attempts: { draft: [{ request: { request: sol, started_at: T0 }, runtime: { number: 1, status: 'succeeded', reported_model: 'gpt-6.2-sol', reported_effort: 'high' } }], review: [] },
    next_action: null, planning_kind: 'stage_split',
  },
  leaves: [
    { card_id: 'review-list', definition: {}, unmet_dependencies: [], planning_run_id: 'pl-a', task_run_id: 'tl-a', candidate: 'aa11bb2', implementation_attempt: 1, candidate_verified: true, review_ready: true, blocked_reason: null, planning: leafPlanning('pl-a', terra), implementation: leafTask('tl-a', terra, 'gpt-6.2-terra') },
    { card_id: 'diff-viewer', definition: {}, unmet_dependencies: [], planning_run_id: 'pl-b', task_run_id: 'tl-b', candidate: 'cc33dd4', implementation_attempt: 1, candidate_verified: true, review_ready: true, blocked_reason: null, planning: leafPlanning('pl-b', sonnet), implementation: leafTask('tl-b', sonnet, 'claude-sonnet-5-5') },
  ],
  integration: {
    integration_id: 'in-6a7b8c9d',
    candidate: 'e0c4b19f2a3d',
    test_attempt: 1,
    tests_passed: true,
    review_satisfied: true,
    evidence: {
      test: { attempt: 1, status: 'succeeded', candidate: 'e0c4b19f2a3d' },
      reviews: [{
        round_id: 'ir-0c9d8e7f', snapshot_id: '5d0b7e2c', status: 'ready_for_assessment',
        metadata: { kind: 'review', created_at: T0 },
        seats: [{ key: 'seat-1', provider: 'claude', requested_model: 'claude-opus-5-5', requested_effort: 'high', current_attempt: 1, attempts: [{ number: 1, status: 'succeeded', reported_model: 'claude-opus-5-5', reported_effort: 'high' }] }],
        assessment: { status: 'ready', reasons: [], groups: [] },
      }],
      rework: null,
      acceptance: { acceptance_id: 'acc-1', accepted_at: '2026-11-19T20:41:09Z' },
    },
  },
};

// ---- Jobs, as GET /api/jobs/{id} returns them (with the 补 2 fields) ----

const job = (over: Partial<Job>): Job => ({
  job_id: '4be1c0d29f8a7e6b5c4d3e2f1a0b9c8d', action: 'task.review', state: 'finished', exit_code: 0,
  result: {}, error: null, resources: [], artifacts: null, ...over,
});

export const jobs: { label: string; job: Job }[] = [
  { label: 'queued', job: job({ state: 'queued', exit_code: null, result: null }) },
  { label: 'running', job: job({ state: 'running', exit_code: null, result: null }) },
  { label: 'cancelling', job: job({ state: 'cancelling', exit_code: null, result: null, cancel_requested_at: '2026-11-19T20:14:03Z' }) },
  { label: 'finished · 0', job: job({}) },
  { label: 'finished · 1，带结果', job: job({ exit_code: 1, result: { workflow: { outcome: 'needs_rework' } } }) },
  { label: 'finished · 2，没有结果', job: job({ action: 'integration.accept', exit_code: 2, result: null, error: { code: 'cli_failed', message: 'exit code 2' } }) },
  { label: 'finished · 130', job: job({ exit_code: 130, result: null, error: { code: 'cli_cancelled', message: 'cancelled' } }) },
  { label: 'interrupted', job: job({ state: 'interrupted', exit_code: null, result: null, error: { code: 'service_interrupted', message: 'service restarted' } }) },
];

// ---- Diff, as the text inside GET /api/diffs/task/{id} ----

export const reworkDiff = `diff --git a/frontend/src/jobs/poll.ts b/frontend/src/jobs/poll.ts
index 3f9a1c2..7b21d04 100644
--- a/frontend/src/jobs/poll.ts
+++ b/frontend/src/jobs/poll.ts
@@ -36,11 +36,12 @@ export async function settle(job: Job): Promise<Outcome> {
   const stderr = await tail(job, 'stderr');
   if (job.state !== 'finished') return pending(job);

-  if (job.error === null) {
-    markSucceeded(job);
-    return { kind: 'ok' };
+  // A finished job is judged by its exit code and the domain state read back.
+  if (job.exit_code === 2 && job.result === null) {
+    return { kind: 'rejected', stderr };
   }
-  return { kind: 'failed', stderr };
+  const domain = await readBack(job.resources ?? []);
+  return domain.ok ? { kind: 'ok' } : { kind: 'unsatisfied', domain };
 }

 export function pending(job: Job): Outcome {
@@ -88,2 +89,2 @@ export function cancellable(job: Job): boolean {
-  return job.state === 'running' || job.state === 'cancelling';
+  return job.state === 'running';
 }
\\ No newline at end of file
diff --git a/frontend/src/jobs/labels.ts b/frontend/src/jobs/state-labels.ts
similarity index 92%
rename from frontend/src/jobs/labels.ts
rename to frontend/src/jobs/state-labels.ts
index 1c2d3e4..5f6a7b8 100644
--- a/frontend/src/jobs/labels.ts
+++ b/frontend/src/jobs/state-labels.ts
@@ -4,3 +4,3 @@
   queued: '排队中',
-  cancelling: '取消中',
+  cancelling: '已请求取消',
   interrupted: '已中断',
diff --git a/frontend/public/plate-grain.png b/frontend/public/plate-grain.png
new file mode 100644
index 0000000..9e8d7c6
Binary files /dev/null and b/frontend/public/plate-grain.png differ
diff --git "a/docs/\\346\\240\\267\\345\\274\\240.md" "b/docs/\\346\\240\\267\\345\\274\\240.md"
index 2a3b4c5..6d7e8f9 100644
--- "a/docs/\\346\\240\\267\\345\\274\\240.md"
+++ "b/docs/\\346\\240\\267\\345\\274\\240.md"
@@ -1,2 +1,2 @@
 # 样张
-待确认
+已确认
`;

/** The blocking finding from the first round, anchored on the old side of the rework diff
 *  (the candidate that was reviewed). */
export const reworkFinding = { finding, seat: 'reviewer-1 · Claude Opus 5.5', anchor: 'old' as const };

// ---- Log pages, as GET /api/artifacts/{id} returns them ----

export const testLog: ArtifactPage = {
  artifact_id: 'art-dGVzdC0xLWNtZC0zLWxvZy0x',
  offset: 0,
  next_offset: 4096,
  byte_count: 11873,
  sha256: '6c0f2b9e4d1a8c7b3e5f9a2d0c4b6e8f1a3d5c7e9b0f2a4c6e8d0b2f4a6c8e0d',
  text: `$ pnpm --dir frontend run check
> multi-agent-frontend@0.0.0 check
> tsc --noEmit && vitest run

 RUN  v5.1.1 /work/frontend

 ✓ src/stage/derive.test.ts (9 tests) 41ms
 ✓ src/diff/parse.test.ts (7 tests) 12ms
 ✓ src/jobs/poll.test.ts (11 tests) 23ms

 Test Files  3 passed (3)
      Tests  27 passed (27)
   Duration  1.18s
`,
};

export const rejectedStderr: ArtifactPage = {
  artifact_id: '4be1c0d29f8a7e6b5c4d3e2f1a0b9c8d.stderr',
  offset: 0,
  next_offset: null,
  byte_count: 214,
  text: `error: integration in-6a7b8c9d is not eligible for acceptance
  review round ir-0c9d8e7f is not current for test attempt 2
  run integration.review again, then accept the new round
`,
};

export const lockBusyPage: ArtifactPage & { lock_busy: boolean } = {
  artifact_id: 'art-dGVzdC0yLWNtZC0xLWxvZy0x', offset: 0, next_offset: null, byte_count: 0, text: null, lock_busy: true,
};

// ---- The weave sample: GPT and Claude working side by side ----
// A delegate stage: Sol splits it, Opus reviews both cards, Terra and Sonnet build the two
// leaves in parallel, the workbench merges, Opus and Astra review blind, Terra reworks once,
// and the child integration passes and is accepted.

const seat = (key: string, provider: string, model: string, effort: string, reported: boolean) => ({
  key, provider, requested_model: model, requested_effort: effort, current_attempt: 1,
  attempts: [{ number: 1, status: 'succeeded', reported_model: reported ? model : null, reported_effort: reported ? effort : null }],
});

const crossReview = (id: string, snapshot: string, ready: boolean): ReviewRound => ({
  round_id: id, snapshot_id: snapshot, status: 'ready_for_assessment',
  metadata: { kind: 'review', created_at: T0, automatic_rework: true },
  seats: [seat('seat-1', 'claude', 'claude-opus-5-5', 'high', true), seat('seat-2', 'codex', 'gpt-6.2-astra', 'xhigh', true)],
  assessment: ready
    ? { status: 'ready', reasons: [], groups: [] }
    : { status: 'needs_rework', reasons: ['blocking_findings_present'], groups: [{ blocking: true, members: [{ seat: 'seat-2', finding: { ...finding, path: 'frontend/src/review/list.tsx', line_start: 58, line_end: 60, title: '审核列表没有过滤确认轮' } }] }] },
});

export const collabHistory = [{
  integration_id: 'in-1f2e3d4c',
  candidate: 'b7c6d5e4f3a2',
  test_attempt: 1,
  tests_passed: true,
  review_satisfied: false,
  evidence: {
    test: { attempt: 1, status: 'succeeded', candidate: 'b7c6d5e4f3a2' },
    reviews: [crossReview('ir-11aa22bb', '6e1f0a9c', false)],
    rework: {
      rework_id: 'rw-01', source_id: 'in-1f2e3d4c', child_id: 'in-9a8b7c6d',
      request: { requested_actor: { decision: { choice: terra } } },
      result: { status: 'succeeded' },
    },
    acceptance: null,
  },
}];

export const collabStage: DelegateStage = {
  ...delegateStage,
  stage_run_id: 'sr-review-pages-02',
  integration: {
    integration_id: 'in-9a8b7c6d',
    candidate: 'd4e5f60718a9',
    test_attempt: 1,
    tests_passed: true,
    review_satisfied: true,
    evidence: {
      test: { attempt: 1, status: 'succeeded', candidate: 'd4e5f60718a9' },
      reviews: [crossReview('ir-33cc44dd', 'a0b1c2d3', true)],
      rework: null,
      acceptance: { acceptance_id: 'acc-2', accepted_at: '2026-11-19T21:12:40Z' },
    },
  },
};
