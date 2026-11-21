// 审核轮: seats and their evidence, findings grouped by place, and the frozen input as a
// diff with each finding under its lines. Review rounds and snapshots do not take the
// repository lock, so this page also shows seat progress while a review job runs.

import { api } from '../api/client';
import { useRead } from '../api/reads';
import type { Diff, Finding, IntegrationRecord, ReviewRound, TaskView } from '../api/types';
import { ASSESSMENT_LABEL, shortId } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { Badge } from '../components/Badge';
import { DiffView, type Annotation } from '../components/DiffView';
import { EmptyState } from '../components/EmptyState';
import { ReadFrame } from '../components/ReadNote';
import { currentIntegrationReview, currentTaskReview, openIntegrationRevision, ReviewRetry, ROUND_TONE, RoundSummary } from './bits';
import { ArtifactLinks, Page, Section, useResource } from './common';

type SnapshotSource =
  | { kind: 'executed_integration_review'; integration_id: string }
  | { kind: 'executed_task_review'; executed_suite: { prepared: { checkout: { input: { task_run_id: string } } } } };

/** Confirmations reuse the initial round's frozen source. A historical round remains
 *  readable, but its seats are only actionable while that source is current. */
function ConfirmationActions({ round }: { round: ReviewRound }) {
  const snapshot = useResource<{ input: SnapshotSource }>('snapshot', round.snapshot_id, undefined, { pollWhileJob: false });
  const source = snapshot.data?.data?.input;
  const integrationId = source?.kind === 'executed_integration_review' ? source.integration_id : undefined;
  const taskId = source?.kind === 'executed_task_review' ? source.executed_suite.prepared.checkout.input.task_run_id : undefined;
  const integration = useResource<IntegrationRecord>('integration', integrationId).data?.data;
  const task = useResource<TaskView>('task', taskId).data?.data;
  const parent = integration
    ? (integration.acceptance ? undefined : currentIntegrationReview(integration))
    : task ? currentTaskReview(task) : undefined;
  const retry = parent?.round_id === round.metadata.parent_round_id &&
    parent?.confirmation_round_id === round.round_id && parent.snapshot_id === round.snapshot_id;
  return (
    <div className="form-actions">
      {integrationId ? <Link href={to('integrations', integrationId)}>返回集成</Link> : null}
      {taskId ? <Link href={to('tasks', taskId)}>返回实施和审核</Link> : null}
      <ReviewRetry round={retry ? round : null} integrationId={integrationId} onDone={integrationId ? openIntegrationRevision : undefined} />
    </div>
  );
}

function fileLink(snapshot: string, f: Finding): string {
  return `${to('snapshots', snapshot, 'file')}?side=${f.side}&path=${encodeURIComponent(f.path)}&line=${f.line_start}&end=${f.line_end}`;
}

export function ReviewPage({ id }: { id: string }) {
  const read = useResource<ReviewRound>('review', id);
  const round = read.data?.id === id ? read.data.data : null;
  const snap = round?.snapshot_id ?? null;
  const diff = useRead<Diff>(snap ? `diff/snapshot/${snap}` : null, () => api.diff('snapshot', snap!), { locking: false });
  const groups = round?.assessment?.groups ?? [];
  const annotations: Annotation[] = groups.flatMap((g) => g.members.map((m) => ({ finding: m.finding, seat: m.seat, anchor: m.finding.side === 'candidate' ? 'new' as const : 'old' as const })));
  return (
    <Page kicker={`审核轮 · ${shortId(id)}`} title={round?.metadata.kind === 'confirmation' ? '交叉确认' : '代码审核'}>
      <ReadFrame read={{ ...read, data: round }} what="审核轮">
        {(r) => (
          <>
            <Section title="席位">
              <RoundSummary round={r} />
              {r.metadata.kind === 'confirmation' ? <ConfirmationActions key={r.round_id} round={r} /> : null}
            </Section>
            <Section title="意见" note="P0、P1 是阻塞意见。点「在冻结文件里查看」跳到它指向的行。">
              {r.assessment ? <p className="row"><Badge tone={ROUND_TONE[r.assessment.status] ?? 'muted'}>{ASSESSMENT_LABEL[r.assessment.status] ?? r.assessment.status}</Badge>{r.assessment.reasons.map((x) => <code key={x} className="stamp">{x}</code>)}</p> : null}
              {groups.length ? (
                <ul className="finding-list">
                  {groups.flatMap((g) => g.members).map((m) => (
                    <li key={`${m.seat}-${m.finding.id}`}>
                      <Badge tone={m.finding.severity === 'P0' || m.finding.severity === 'P1' ? 'fail' : 'warn'}>{m.finding.severity}</Badge>
                      <span className="finding-title">{m.finding.title}</span>
                      <code className="stamp">{m.seat}</code>
                      <Link href={fileLink(r.snapshot_id, m.finding)}>{m.finding.path}:{m.finding.line_start}</Link>
                    </li>
                  ))}
                </ul>
              ) : <p className="muted">没有意见。</p>}
              {r.confirmation_round_id ? <p><Link href={to('reviews', r.confirmation_round_id)}>查看交叉确认</Link></p> : null}
            </Section>
            <Section title="冻结的审核输入" note="两位审核者看到的是同一份。">
              <ArtifactLinks artifacts={read.data?.artifacts ?? []}
                label={(a) => `冻结日志 · 命令 ${a.coordinates?.frozen_index ?? ''} · ${a.stream ?? a.key ?? '日志'}`} />
              <ReadFrame read={diff} what="冻结快照的 diff">
                {(d) => <DiffView diff={d.diff} annotations={annotations} onOpenFinding={(a) => navigate(fileLink(r.snapshot_id, a.finding))}
                  emptyNote={<EmptyState compact title="没有可看的 diff" body={d.unavailable_reason ?? '这份快照没有 diff。'} />} />}
              </ReadFrame>
            </Section>
          </>
        )}
      </ReadFrame>
    </Page>
  );
}
