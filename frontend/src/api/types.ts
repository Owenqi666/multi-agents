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

export interface JobArtifact {
  artifact_id: string;
  stream: string;
  byte_count: number;
  sha256: string;
}

export interface Job {
  job_id: Id;
  action: string;
  state: JobState;
  exit_code: number | null;
  result: Record<string, unknown> | null;
  error: ApiError | null;
  /** What kind of error `error` is, as the service classifies it; null without an error.
   *  Absent from an older service, and then the page makes no judgement of its own. */
  error_kind?: 'refused' | 'failed' | 'cancelled' | 'interrupted' | string | null;
  resources?: { kind: string; id: Id }[];
  artifacts?: JobArtifact[] | null;
  args?: Record<string, unknown> | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  cancel_requested_at?: string | null;
  recovery_consultation_id?: Id | null;
  /** Last 8 KiB of stderr once the CLI exited; null while running. */
  stderr_tail?: string | null;
}

export const UNFINISHED: readonly JobState[] = ['queued', 'running', 'cancelling'];
export const isUnfinished = (job: Pick<Job, 'state'>): boolean => UNFINISHED.includes(job.state);

export interface ArtifactRef {
  artifact_id: string;
  key?: string;
  coordinates?: Record<string, number | string>;
  stream?: string;
  byte_count?: number;
  sha256?: string;
}

export interface NextAction {
  kind: string;
  detail: string;
  planning_run_id?: Id | null;
  task_run_id?: Id | null;
  implementation_attempt?: number | null;
  test_attempt?: number | null;
  review_round_id?: Id | null;
}

export interface Candidate {
  family: string;
  provider: string;
  model: string;
  efforts: string[] | null;
  source?: string;
  effort_source?: string;
}

export interface Role {
  id: string;
  label: string;
  families: string[];
  candidates: Candidate[];
}

export interface Workspace {
  repository: string;
  target_branch: string | null;
  head: string | null;
  branches?: { name: string; head: string }[];
  defaults?: {
    source: string;
    stage_mode: 'direct' | 'delegate';
    sol: { model: string; effort: string };
    card_review: RoleRequest;
    /** What single-card planning requests when the form names no model. */
    task_plan?: { draft?: RoleRequest | null; plan_review?: RoleRequest | null } | null;
  };
  read_only: boolean;
  presets: { source: string; roles: Role[]; rules: Record<string, unknown> };
  environments: Record<string, { observed_at: string; logged_in: boolean; cli_version: string | null; error: unknown } | null>;
}

export interface RunItem {
  kind: string;
  id: Id;
  label: string;
  parent_id: Id | null;
  updated_at: string;
  scope?: string;
}

export interface Project {
  kind: 'project';
  project_id: Id;
  goals: Goal[];
  plans: { plan_id: Id; goal_id: Id; started_at: string | null; finished_at: string | null; status: string; origin?: 'pm'; source_plan_id?: Id }[];
}

export interface Resource<T> {
  kind: string;
  id: Id;
  data: T | null;
  artifacts: ArtifactRef[];
  lock_busy?: boolean;
}

export interface ArtifactPage {
  artifact_id?: string;
  stream?: string;
  offset: number;
  next_offset: number | null;
  byte_count: number | null;
  sha256?: string | null;
  truncated?: boolean;
  text: string | null;
  raw_base64?: string;
  lock_busy?: boolean;
}

export interface Diff {
  baseline?: string | null;
  candidate?: string | null;
  parent_candidate?: string | null;
  snapshot_id?: Id | null;
  diff: string | null;
  unavailable_reason?: string | null;
  unavailable_code?: string | null;
  test_change_paths?: string[] | null;
  lock_busy?: boolean;
}

export interface FrozenFile {
  side: 'baseline' | 'candidate';
  path: string;
  text: string | null;
  oid: string;
  mode: string;
}

export interface UserTestEvidence {
  required_commands: string[][];
  results: { commit: string; command: string[]; exit_code: number; log: string }[];
}

export interface StandaloneSnapshotInput {
  kind?: string;
  baseline: string | null;
  candidate: string | null;
  diff: string;
  requirements: string;
  acceptance: string[];
  tests: UserTestEvidence['results'];
  required_tests: string[][];
  files: Pick<FrozenFile, 'side' | 'path'>[];
}

export interface ReviewSnapshot {
  snapshot_id: Id;
  input: StandaloneSnapshotInput | {
    kind: 'executed_task_review' | 'executed_integration_review';
    source: Record<string, unknown>;
  };
}

// ---- Project layer -------------------------------------------------------------------

export type SeatStatus = 'pending' | 'running' | 'succeeded' | 'failed';

export interface Advice {
  draft_goal: string;
  background: unknown[];
  assumptions: unknown[];
  questions: unknown[];
  remaining_disagreements: unknown[];
}

export interface ConsultationSeat {
  consultation_id: Id;
  number: number;
  request: RoleRequest;
  started_at: string | null;
  finished_at: string | null;
  advice: Advice | null;
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
  interrupted_at?: string | null;
  draft: Advice | null;
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
  approach?: string;
  workflow?: string;
  acceptance_criteria?: unknown[];
  deliverables?: unknown[];
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
  document: { title: string; architecture?: string; shared_interfaces?: unknown; stages: PlanStage[] } | null;
  markdown?: string | null;
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
  detail?: string | null;
  reported_model: string | null;
  reported_effort: string | null;
  result?: unknown;
  exit_code?: number | null;
}

export interface PlanningAttempt {
  request: { request: RoleRequest; started_at: string | null; kind?: string };
  runtime: Runtime | null;
}

export interface PlanningDependency {
  task_id: string;
  baseline_commit: string;
  source: string;
  detail: string;
}

export interface PlanningDecision {
  question: string;
  resolution: string;
  source: string;
  detail: string;
}

export interface PlanningQuestion {
  question: string;
  blocks_dispatch: boolean;
  resolution?: string | null;
}

export interface PlanningInputView {
  card?: { test_refs?: string[] };
  requirements?: string;
  frozen_rules?: { baseline_commit?: string; rules?: { commands?: { name: string }[] } };
  draft_request?: RoleRequest;
  review_request?: RoleRequest;
  dependencies?: PlanningDependency[];
  decisions?: PlanningDecision[];
}

export interface PlanningView {
  planning_run_id: Id;
  task_run_id: Id | null;
  phase: string;
  verification: string;
  card: Record<string, unknown> | null;
  attempts: { draft: PlanningAttempt[]; review: PlanningAttempt[] };
  next_action: NextAction | null;
  planning_kind?: 'stage_split';
  standalone?: boolean;
  dispatch_configurable?: boolean;
  retry_configurable?: boolean;
  questions?: PlanningQuestion[];
  budget?: Record<string, { limit: number; used: number; remaining: number }> | null;
  details?: { input?: PlanningInputView; validation_error?: string | null; source_errors?: string[] };
  draft_projection?: {
    executor?: RoleRequest;
    dependencies?: {
      card_id: string;
      planning_run_id: Id;
      task_run_id: Id;
      implementation_attempt: number;
      candidate: string;
    }[];
  };
}

export interface ImplementationAttempt {
  request: {
    decision: {
      choice: RoleRequest;
      requested?: RoleRequest | null;
      reason?: string;
      selection_history?: { choice: RoleRequest; outcome: string }[];
    };
    worktree?: string | null;
    started_at: string | null;
    kind: 'task_attempt_started' | 'task_rework_started';
    source?: { review_round_id?: Id };
  };
  runtime: Runtime | null;
  events?: { kind: string; dispatch_policy?: { fallbacks: RoleRequest[] } }[];
  usage_evidence?: { source: string; counts: Record<string, number> } | null;
  runtime_reroutes?: { source: string; from_model: string; to_model: string; reason: string }[] | null;
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
  /** The test ran on the candidate that is still the task's current one. */
  source_current?: boolean;
  commands: TestCommand[];
  details?: {
    is_current?: boolean;
    /** The service would accept a retest of this test now: it is the current test of the
     *  current candidate, has finished as failed, cancelled or awaiting evidence, and has
     *  settled. Absent from an older service. */
    retry_ready?: boolean;
    finished?: boolean;
    cancel_requested?: boolean;
    /** Who declared the commands of this test ("PM" from the page); a retest repeats it. */
    declaration_source?: string;
  } | null;
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
  metadata: { kind: 'review' | 'confirmation'; created_at: string; automatic_rework?: boolean; rework_from_round_id?: Id; parent_round_id?: Id | null };
  seats: ReviewSeat[];
  assessment: Assessment | null;
  implementation_attempt?: number;
  test_attempt?: number;
  candidate?: string | null;
  current_eligible?: boolean;
  confirmation_round_id?: Id | null;
  verification?: string;
  source_current?: boolean;
  independent_review?: boolean;
  can_confirm?: boolean;
}

export interface TaskView {
  planning_run_id: Id;
  task_run_id: Id;
  implementation_attempt: number | null;
  phase: string;
  candidate: string | null;
  verification: string;
  attempts: ImplementationAttempt[];
  tests: TaskTest[];
  reviews: ReviewRound[];
  rework_used: boolean;
  next_action: NextAction | null;
  budget?: { remaining: number } | null;
  details?: { input?: { card?: { test_refs?: string[] }; frozen_rules?: { rules?: { commands?: { name: string }[] } } } };
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
  prior_planning_run_ids?: Id[];
}

export interface IntegrationRework {
  rework_id: Id;
  source_id: Id;
  child_id: Id | null;
  request?: {
    requested_actor?: { decision?: { choice?: RoleRequest } };
    review_round_id?: Id;
    findings?: unknown[];
  };
  result: { status: string; evidence?: Evidence | null } | null;
}

interface IntegrationAcceptance {
  acceptance_id: Id;
  accepted_at: string;
  candidate?: CandidateRef | string | null;
  review_round_id?: Id | null;
}

export interface StageIntegration {
  integration_id: Id;
  candidate: string | null;
  test_attempt: number | null;
  tests_passed: boolean;
  review_satisfied: boolean;
  evidence: {
    rebased_from?: Id | null;
    test: { attempt: number; status: string; candidate: string | CandidateRef | null } | null;
    reviews: ReviewRound[];
    rework: IntegrationRework | null;
    acceptance: IntegrationAcceptance | null;
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

export interface CandidateRef {
  ref: string;
  commit: string;
  tree: string;
}

/** A commit id, whether the record gives a bare string or a {ref, commit, tree} object. */
export function commitOf(c: string | CandidateRef | null | undefined): string | null {
  if (!c) return null;
  return typeof c === 'string' ? c : c.commit;
}

export interface IntegrationRecord {
  integration_id: Id;
  status: 'ready' | 'conflicted' | string;
  verification: string;
  target_ref: string;
  target_baseline?: string;
  target_head?: string;
  target_is_current: boolean;
  members: { task_run_id: Id; planning_run_id: Id; attempt: number; task_input?: { card?: { id?: string; goal?: string } } }[];
  candidate: CandidateRef | null;
  conflict: unknown;
  rebased_from: Id | null;
  test: { attempt: number; status: string; candidate: CandidateRef | string | null } | null;
  test_history?: NonNullable<IntegrationRecord['test']>[];
  test_evidence_verification?: string;
  reviews: ReviewRound[];
  current_eligible: boolean;
  blocking_findings: unknown[];
  acceptance: IntegrationAcceptance | null;
  rework: IntegrationRework | null;
  superseded_by: Id | null;
  source_errors?: string[];
}

// ---- Workbench reads added with the inbox, job activity, finding discussions, first-run
// ---- check and delivery summary (read from the backend's web/ modules) ----------------

/** One thing waiting for the user. `id` is the record's id and may repeat: one plan can
 *  offer several stages, so rows are told apart by `href` as well. */
export interface AttentionItem {
  kind: string;
  id: Id;
  label: string;
  reason: string;
  detail: string;
  href: string;
}

/** `complete: false` means a job holds the worktree: only items the database can confirm
 *  by itself came back, the ones that need live evidence are deferred. */
export interface Attention {
  items: AttentionItem[];
  complete: boolean;
}

export interface RecordRef {
  kind: string;
  id: Id;
}

export interface ActivityEvent {
  /** Unique within one job; repeated polls return the same numbers. */
  sequence: number;
  at: string;
  event: string;
  state: string | null;
  resource: RecordRef;
  seat: string;
  provider: string | null;
  attempt: number;
}

export interface ActivityActor {
  resource: RecordRef;
  seat: string;
  provider: string | null;
  attempt: number;
  /** `unconfirmed` when the job ended while the last observation still said running. */
  state: string;
  last_observed_state?: string | null;
  observed_at?: string | null;
  last_event?: string | null;
}

export interface RecoveryAction {
  resource: RecordRef;
  action: string;
  args: Record<string, unknown>;
}

export interface ActivityReview {
  round_id: Id;
  snapshot_id: Id;
  seats: { seat: string; provider: string; attempt: number | null; state: string; retryable: boolean }[];
  retry: { action: string; args: Record<string, unknown> } | null;
}

export interface JobActivity {
  job_id: Id;
  state: JobState;
  observed_at: string;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  /** null when the job predates activity journals. */
  controller_holds_journal: boolean | null;
  history_truncated: boolean;
  events: ActivityEvent[];
  actors: ActivityActor[];
  reviews: ActivityReview[];
  resources: RecordRef[];
  recovery: RecoveryAction[];
}

/** Identifies one finding of one seat's completed attempt. Two seats may use the same
 *  finding id, so the anchor is always sent back exactly as the service returned it. */
export interface FindingAnchor {
  round_id: Id;
  snapshot_id: Id;
  seat: string;
  attempt: number;
  finding_id: string;
}

export type FindingDecision = 'confirmed' | 'disputed' | 'deferred';

export interface FindingActivityEntry {
  id: number;
  kind: 'decision' | 'answer' | 'linked_plan' | string;
  created_at: string;
  decision?: FindingDecision | string;
  reason?: string;
  question?: string;
  answer?: string;
  evidence?: (Evidence & Record<string, unknown>) | null;
  planning_run_id?: Id;
}

export interface FindingMember {
  seat: string;
  finding: Finding;
  anchor: FindingAnchor;
  activity: FindingActivityEntry[];
}

export interface FindingConfirmation {
  seat: string;
  target_seat: string;
  finding_id: string;
  decision: 'agree' | 'disagree' | 'uncertain' | string;
  reason: string;
}

export interface FindingGroup {
  location?: { side?: string; path?: string; line_start?: number; line_end?: number } | null;
  blocking: boolean;
  independent_reports?: Record<string, 'reported' | 'unconfirmed' | string> | null;
  confirmations?: FindingConfirmation[] | null;
  members: FindingMember[];
}

export interface ReviewFindings {
  round_id: Id;
  snapshot_id: Id;
  status: Assessment['status'] | string;
  reasons: string[];
  source: {
    kind: 'pasted_diff' | 'repository' | string;
    baseline: string | null;
    candidate: string | null;
    /** The reviewed commit a correction must start from; null for a pasted diff. */
    correction_baseline: string | null;
  };
  groups: FindingGroup[];
}

export interface ReworkOwner {
  task_id: string;
  task_run_id: Id;
}

export interface ReworkEligibility {
  integration_id: Id;
  round_id?: Id | null;
  eligible: boolean;
  reason?: string | null;
  owner?: ReworkOwner | null;
  findings?: { seat: string; finding_id: string; path: string; reference_status?: string | null; owners: ReworkOwner[] }[];
  lock_busy?: boolean;
}

export interface OnboardingProvider {
  status: 'unknown' | 'missing_cli' | 'checking' | 'check_failed' | 'login_required' | 'ready' | string;
  cli_version: string | null;
  observed_at: string | null;
  check_job_id: Id | null;
  error: { code?: string; message?: string } | null;
}

export interface Onboarding {
  repository: string;
  read_only: boolean;
  branch: string | null;
  head: string | null;
  branches: { name: string; head: string }[];
  /** The commit the check ran against; null when the repository has no commit. */
  baseline: string | null;
  rules: {
    path: string;
    status: 'no_commit' | 'missing' | 'invalid' | 'no_commands' | 'ready' | string;
    detail: string | null;
    commands: { name: string; argv: string[] }[];
  };
  rules_template: string;
  providers: Record<string, OnboardingProvider>;
  next_steps: { kind: string; provider?: string }[];
}

export interface DeliveryReceipt {
  acceptance_id: Id;
  integration_id: Id;
  target_ref: string;
  target_baseline: string;
  candidate: CandidateRef;
  test_attempt: number;
  test_evidence_hash: string;
  review_round_id: Id;
  snapshot_id: Id;
  review_checkpoint_hash: string;
  accepted_at: string;
}

export interface Delivery {
  receipt: DeliveryReceipt;
  tasks: { id: string; goal: string }[];
  tests: { name: string; exit_code: number | null }[];
  links: { integration: string; diff: string; review: string };
  markdown: string;
}
