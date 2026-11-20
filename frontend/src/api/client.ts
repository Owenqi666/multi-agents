// Typed calls to the bridge, on top of a transport (HTTP, or the mock that replays samples).

import { http, seg, type Query, type Transport } from './transport';
import type { ArtifactPage, Diff, FrozenFile, Job, Resource, RunItem, Workspace } from './types';

let transport: Transport = http;

export function setTransport(t: Transport): void {
  transport = t;
}

export function getTransport(): Transport {
  return transport;
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
};
