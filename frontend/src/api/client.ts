// Typed calls to the bridge, on top of a transport (HTTP, or the mock that replays samples).

import { http, seg, type Query, type Transport } from './transport';
import type {
  ArtifactPage, Attention, Delivery, Diff, FindingActivityEntry, FindingAnchor, FindingDecision, FrozenFile, Job, JobActivity,
  Onboarding, Resource, ReviewFindings, ReworkEligibility, RunItem, Workspace,
} from './types';

let transport: Transport = http;

export function setTransport(t: Transport): void {
  transport = t;
}

export function getTransport(): Transport {
  return transport;
}

/** The name of the recording when the page replays one instead of talking to a service. */
export function demoScenario(): string | null {
  const name = (transport as { demoScenario?: unknown }).demoScenario;
  return typeof name === 'string' ? name : null;
}

/** Reads that take the repository's executor lock. While a CLI job is unfinished the pages
 *  do not send them at all (contract "读回与仓库锁"). */
export const LOCKING_KINDS = new Set(['task', 'task-test', 'task-review', 'integration', 'stage', 'plan']);
export const isLockingKind = (kind: string): boolean => LOCKING_KINDS.has(kind);

export const api = {
  workspace: () => transport.get<Workspace>('/workspace'),
  runs: (kind?: string) => transport.get<{ items: RunItem[] }>('/runs', kind ? { kind } : undefined),
  jobs: () => transport.get<{ items: Job[] }>('/jobs'),
  job: (id: string) => transport.get<Job>(`/jobs/${seg(id)}`),
  submit: (action: string, args: Record<string, unknown>) =>
    transport.post<{ job_id: string; state: string }>('/jobs', { action, args }),
  cancel: (id: string) => transport.post<{ job_id: string; state: string }>(`/jobs/${seg(id)}/cancel`, {}),
  resource: <T>(kind: string, id: string, query?: Query) => transport.get<Resource<T>>(`/resources/${seg(kind)}/${seg(id)}`, query),
  diff: (kind: 'task' | 'integration' | 'snapshot', id: string, against?: 'parent') =>
    transport.get<Diff>(`/diffs/${kind}/${seg(id)}`, against ? { against } : undefined),
  frozenFile: (snapshotId: string, side: 'baseline' | 'candidate', path: string) =>
    transport.get<FrozenFile>(`/snapshots/${seg(snapshotId)}/file`, { side, path }),
  artifact: (id: string, offset = 0, limit = 65536) => transport.get<ArtifactPage>(`/artifacts/${seg(id)}`, { offset, limit }),
  /** What waits for the user across the repository. Safe to ask while a job runs: the answer
   *  then carries `complete: false` and only what the database can confirm. */
  attention: () => transport.get<Attention>('/attention'),
  jobActivity: (id: string) => transport.get<JobActivity>(`/jobs/${seg(id)}/activity`),
  findings: (roundId: string) => transport.get<ReviewFindings>(`/reviews/${seg(roundId)}/findings`),
  /** Records the user's own judgement on one finding; the anchor goes back as it was read. */
  decide: (roundId: string, anchor: FindingAnchor, decision: FindingDecision, reason: string) =>
    transport.post<FindingActivityEntry>(`/reviews/${seg(roundId)}/decisions`, { anchor, decision, reason }),
  /** Takes the repository lock on the service; answers `lock_busy: true` while a job runs. */
  reworkEligibility: (integrationId: string) => transport.get<ReworkEligibility>(`/integrations/${seg(integrationId)}/rework-eligibility`),
  /** Read-only first-run check for HEAD, or for the given branch or commit. */
  onboarding: (baseline?: string | null) => transport.get<Onboarding>('/workspace/onboarding', baseline ? { baseline } : undefined),
  /** 409 until the integration has an acceptance receipt. */
  delivery: (integrationId: string) => transport.get<Delivery>(`/integrations/${seg(integrationId)}/delivery`),
};
