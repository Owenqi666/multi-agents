// Shapes of the bridge's JSON, as written in docs/web-api/contract-2026-11-19.md
// ("接口条件") and read from the backend at 77991ab. Only the fields the pages use are
// typed. Unknown fields are ignored, and a missing key is read as null (see nz below):
// placeholder leaves, for example, have no implementation key at all.

export type Id = string;

/** Read a key that may be absent and treat absence as null. */
export function nz<T>(value: T | null | undefined): T | null {
  return value === undefined ? null : value;
}

export interface RoleRequest {
  provider: string;
  model: string;
  effort: string;
}

/** Runtime evidence. Null fields mean the runtime did not report the value. */
export interface Evidence {
  reported_model?: string | null;
  reported_effort?: string | null;
}

export type JobState = 'queued' | 'running' | 'cancelling' | 'finished' | 'interrupted';

export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface Job {
  job_id: Id;
  action: string;
  state: JobState;
  exit_code: number | null;
  result: Record<string, unknown> | null;
  error: ApiError | null;
  resources?: { kind: string; id: Id }[];
  artifacts?: Record<string, string> | null;
  args?: Record<string, unknown>;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  cancel_requested_at?: string | null;
}

export interface ArtifactRef {
  artifact_id: string;
  key?: string;
  [coordinate: string]: unknown;
}

export interface Resource<T> {
  kind: string;
  id: Id;
  data: T | null;
  artifacts: ArtifactRef[];
  lock_busy?: boolean;
}

export interface ArtifactPage {
  artifact_id: string;
  offset: number;
  next_offset: number | null;
  byte_count: number;
  sha256?: string;
  text: string | null;
}

export interface Diff {
  kind: string;
  id: Id;
  diff: string | null;
  unavailable_reason?: string | null;
  unavailable_code?: string | null;
  test_change_paths?: string[] | null;
}

// ---- Project layer -------------------------------------------------------------------

export type SeatStatus = 'pending' | 'running' | 'succeeded' | 'failed';

export interface ConsultationSeat {
  consultation_id: Id;
  number: number;
  request: RoleRequest;
  started_at: string | null;
  finished_at: string | null;
  advice: string | null;
  error: unknown;
  evidence: Evidence | null;
  status: SeatStatus;
}

export interface Consultation {
  kind: 'goal_consultation';
  consultation_id: Id;
  baseline_commit: string;
  original_goal: string;
  project_id: Id | null;
  started_at: string | null;
  finished_at: string | null;
  status: 'running' | 'succeeded' | 'failed';
  draft: string | null;
  seats: ConsultationSeat[];
}

export interface Goal {
  kind: 'confirmed_goal';
  project_id: Id;
  goal_id: Id;
  baseline_commit: string;
  original_goal: string;
  confirmed_goal: string;
  confirmed_at: string;
  consultation?: { consultation_id: Id; adoption: 'accepted' | 'edited'; source_snapshot_id: Id };
}

export interface PlanStage {
  stage_id: string;
  title: string;
  goal: string;
  depends_on: string[];
}

export interface ProjectPlan {
  kind: 'project_plan';
  plan_id: Id;
  goal: Goal;
  status: 'running' | 'succeeded' | 'failed';
  planner_request: RoleRequest | null;
  started_at: string | null;
  finished_at: string | null;
  evidence: Evidence | null;
  error: unknown;
  document: { title: string; stages: PlanStage[] } | null;
  origin?: 'pm';
  source_plan_id?: Id;
}

export interface StageRun {
  stage_run_id: Id;
  plan_id: Id;
  stage_id: string;
  planning_run_id: Id;
  mode: 'direct' | 'delegate';
  execution_baseline: string;
  dependencies: unknown[];
  created_at: string;
  dispatch_error: string | null;
}

// ---- Stage layer ---------------------------------------------------------------------

export interface Runtime {
  number: number;
  status: string;
  failure_category?: string | null;
  reported_model: string | null;
  reported_effort: string | null;
  result?: unknown;
  exit_code?: number | null;
}

export interface PlanningAttempt {
  request: { request: RoleRequest; started_at: string | null; kind?: string };
  runtime: Runtime | null;
}

export interface PlanningView {
  planning_run_id: Id;
  task_run_id: Id | null;
  phase: string;
  verification: string;
  card: Record<string, unknown> | null;
  attempts: { draft: PlanningAttempt[]; review: PlanningAttempt[] };
  next_action: string | null;
  planning_kind?: 'stage_split';
  draft_projection?: { executor?: RoleRequest };
}

export interface ImplementationAttempt {
  request: {
    decision: { choice: RoleRequest };
    started_at: string | null;
    kind: 'task_attempt_started' | 'task_rework_started';
    source?: { review_round_id?: Id };
  };
  runtime: Runtime | null;
}

export interface TestCommand {
  sequence: number;
  name: string;
  status: string;
  exit_code: number | null;
}

export interface TaskTest {
  task_run_id: Id;
  implementation_attempt: number;
  test_attempt: number;
  candidate: string | null;
  phase: string;
  verification: string;
  commands: TestCommand[];
}

export interface ReviewSeatAttempt {
  number: number;
  status: string;
  reported_model: string | null;
  reported_effort: string | null;
}

export interface ReviewSeat {
  key: string;
  provider: string;
  requested_model: string;
  requested_effort: string;
  current_attempt: number | null;
  attempts: ReviewSeatAttempt[];
}

export interface Finding {
  id: string;
  severity: 'P0' | 'P1' | 'P2' | 'P3';
  category: string;
  side: 'baseline' | 'candidate';
  path: string;
  line_start: number;
  line_end: number;
  quote: string;
  title: string;
  impact: string;
  rationale: string;
  reference_status?: 'verified' | 'unverified';
}

export interface Assessment {
  status: 'incomplete' | 'user_decision' | 'needs_rework' | 'ready';
  reasons: string[];
  groups: { members: { seat: string; finding: Finding }[]; blocking: boolean }[];
}

export interface ReviewRound {
  round_id: Id;
  snapshot_id: Id;
  status: string;
  metadata: { kind: 'review' | 'confirmation'; created_at: string; automatic_rework?: boolean; rework_from_round_id?: Id };
  seats: ReviewSeat[];
  assessment: Assessment | null;
  implementation_attempt?: number;
  test_attempt?: number;
  candidate?: string | null;
  current_eligible?: boolean;
}

export interface TaskView {
  task_run_id: Id;
  implementation_attempt: number | null;
  phase: string;
  candidate: string | null;
  verification: string;
  attempts: ImplementationAttempt[];
  tests: TaskTest[];
  reviews: ReviewRound[];
  rework_used: boolean;
  next_action: string | null;
}

export interface Leaf {
  card_id: string;
  definition: Record<string, unknown>;
  unmet_dependencies: string[];
  planning_run_id: Id | null;
  task_run_id: Id | null;
  candidate: string | null;
  implementation_attempt?: number | null;
  candidate_verified: boolean;
  review_ready: boolean;
  blocked_reason: string | null;
  planning?: PlanningView | null;
  implementation?: TaskView | null;
}

export interface StageIntegration {
  integration_id: Id;
  candidate: string | null;
  test_attempt: number | null;
  tests_passed: boolean;
  review_satisfied: boolean;
  evidence: {
    test: { attempt: number; status: string; candidate: string | null } | null;
    reviews: ReviewRound[];
    rework: {
      rework_id: Id;
      source_id: Id;
      child_id: Id | null;
      request: { requested_actor?: { decision?: { choice?: RoleRequest } } };
      result: { status: string } | null;
    } | null;
    acceptance: { acceptance_id: Id; accepted_at: string } | null;
  } | null;
}

interface StageGates {
  kind: 'project_stage';
  goal_id: Id;
  candidate: string | null;
  candidate_verified: boolean;
  tests_passed: boolean;
  review_satisfied: boolean;
  test_attempt: number | null;
  review_round_id: Id | null;
}

export interface DirectStage extends StageRun, StageGates {
  mode: 'direct';
  task_run_id: Id | null;
  planning: PlanningView;
  implementation: TaskView | null;
}

export interface DelegateStage extends StageRun, StageGates {
  mode: 'delegate';
  planning: PlanningView;
  split_verified: boolean;
  leaves: Leaf[];
  can_integrate: boolean;
  integration: StageIntegration | null;
}

export type Stage = DirectStage | DelegateStage;
