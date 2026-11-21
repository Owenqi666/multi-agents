import { api } from '../api/client';
import { jobs } from '../api/jobs';
import { lockBusy, useRead } from '../api/reads';
import { commitOf, type IntegrationRecord, type IntegrationRework, type Stage, type StageIntegration } from '../api/types';

export type TimelineIntegration = Pick<StageIntegration, 'integration_id' | 'candidate' | 'evidence'>;

export function integrationPredecessor({
  integration_id, rebased_from, rework,
}: {
  integration_id: string;
  rebased_from?: string | null;
  rework?: IntegrationRework | null;
}): string | null {
  if (rebased_from && rebased_from !== integration_id) return rebased_from;
  return rework?.child_id === integration_id && rework.source_id !== integration_id ? rework.source_id : null;
}

function historyEntry(record: IntegrationRecord): TimelineIntegration {
  const sourceRoundId = record.rework?.source_id === record.integration_id
    ? record.rework.request?.review_round_id : null;
  const sourceRound = sourceRoundId ? record.reviews.find((round) => round.round_id === sourceRoundId) : null;
  const test = sourceRound?.test_attempt != null && sourceRound.test_attempt !== record.test?.attempt
    ? record.test_history?.find((attempt) => attempt.attempt === sourceRound.test_attempt) ?? null
    : record.test;
  return {
    integration_id: record.integration_id,
    candidate: commitOf(record.candidate),
    evidence: {
      test,
      rebased_from: record.rebased_from,
      reviews: record.reviews,
      rework: record.rework,
      acceptance: record.acceptance,
    },
  };
}

const NO_HISTORY: TimelineIntegration[] = [];

/** Stage embeds the current integration only. Follow explicit parents using the same
 * locking-read queue as the stage itself. A rework reservation is shared by the lineage. */
export function useIntegrationHistory(stage: Stage | null): TimelineIntegration[] {
  const current = stage?.mode === 'delegate' ? stage.integration : null;
  const parent = current && integrationPredecessor({ ...current.evidence, integration_id: current.integration_id });
  const key = parent ? `integration-lineage/${current.integration_id}/${parent}` : null;
  const read = useRead(key, async () => {
    const history: TimelineIntegration[] = [];
    const visited = new Set([current?.integration_id]);
    let id = parent;
    while (id && !visited.has(id)) {
      if (!jobs.idle) return { lock_busy: true as const };
      const response = await api.resource<IntegrationRecord>('integration', id, { history: 1 });
      if (lockBusy(response)) return { lock_busy: true as const };
      if (!response.data) break;
      history.push(historyEntry(response.data));
      visited.add(id);
      id = integrationPredecessor(response.data);
    }
    return { key, history: history.reverse() };
  }, { locking: true });
  const data = read.data;
  return data && 'history' in data && data.key === key ? data.history : NO_HISTORY;
}
