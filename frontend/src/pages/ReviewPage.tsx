// 审核轮: seats and their evidence, findings grouped by place with their discussion (the
// user's decisions, follow-up questions, linked corrections), and the frozen input as a diff
// with each finding under its lines. Review rounds and snapshots do not take the repository
// lock, so this page also shows seat progress while a review job runs.

import { useMemo } from 'react';
import { api } from '../api/client';
import { useRead } from '../api/reads';
import type { Diff, Finding, IntegrationRecord, ReviewRound, ReviewSnapshot, TaskView } from '../api/types';
import { useWorkspaceWritesAllowed } from '../api/workspace';
import { ASSESSMENT_LABEL, shortId } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { DiffView, type Annotation, type PatchLocation } from '../components/DiffView';
import { EmptyState } from '../components/EmptyState';
import { ReadFrame } from '../components/ReadNote';
import { currentIntegrationReview, currentTaskReview, openIntegrationRevision, ReviewRetry, ROUND_TONE, RoundSummary } from './bits';
import { ArtifactLinks, Page, Section, useResource } from './common';
import { FindingsPanel, useFindings } from './ReviewFindings';
import { UserTests } from './StandaloneReviewPages';
import { ReviewSessionLogs } from './SessionLogs';
import { tx } from '../i18n';

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
      {integrationId ? <Link href={to('integrations', integrationId)}>{tx('返回集成')}</Link> : null}
      {taskId ? <Link href={to('tasks', taskId)}>{tx('返回实施和审核')}</Link> : null}
      <ReviewRetry round={retry ? round : null} integrationId={integrationId} onDone={integrationId ? openIntegrationRevision : undefined} />
    </div>
  );
}

function fileLink(snapshot: string, f: Finding): string {
  return `${to('snapshots', snapshot, 'file')}?side=${f.side}&path=${encodeURIComponent(f.path)}&line=${f.line_start}&end=${f.line_end}`;
}

function IndependentActions({ round, stale }: { round: ReviewRound; stale: boolean }) {
  const writesAllowed = useWorkspaceWritesAllowed();
  const parent = round.metadata.parent_round_id;
  return (
    <div className="form-actions">
      <Link href={to('snapshots', round.snapshot_id)}>{tx('查看冻结输入')}</Link>
      {parent ? <Link href={to('reviews', parent)}>{tx('返回原审核轮')}</Link> : null}
      {writesAllowed && !stale ? <>
        <ReviewRetry round={round} independent />
        {round.can_confirm && !round.confirmation_round_id ? (
          <ActionButton action="review.confirm" label={tx('发起交叉确认')} args={() => ({ round_id: round.round_id })}
            onDone={(job) => {
              if (window.location.pathname !== to('reviews', round.round_id)) return;
              const confirmation = job.resources?.find((record) => record.kind === 'review' && record.id !== round.round_id);
              navigate(confirmation ? to('reviews', confirmation.id) : to('jobs', job.job_id));
            }} />
        ) : null}
      </> : null}
    </div>
  );
}

function parseLocation(search: string): PatchLocation | undefined {
  const query = new URLSearchParams(search);
  const side = query.get('side');
  const path = query.get('path');
  const start = Number(query.get('line'));
  const end = Number(query.get('end') ?? start);
  if ((side === 'baseline' || side === 'candidate') && path && Number.isSafeInteger(start) && start > 0 && Number.isSafeInteger(end) && end >= start) {
    return { side, path, start, end };
  }
  return undefined;
}

export function ReviewPage({ id, patchLocation }: { id: string; patchLocation?: URLSearchParams }) {
  const read = useResource<ReviewRound>('review', id, { session_logs: 1 });
  const round = read.data?.id === id ? read.data.data : null;
  const snap = round?.snapshot_id ?? null;
  const snapshot = useResource<ReviewSnapshot>('snapshot', round?.independent_review ? snap : null, undefined, { pollWhileJob: false });
  const input = snapshot.data?.id === snap ? snapshot.data?.data?.input : undefined;
  const standalone = input && 'requirements' in input ? input : undefined;
  const pasted = standalone?.files.length === 0 && standalone.baseline === null && standalone.candidate === null;
  const diff = useRead<Diff>(snap ? `diff/snapshot/${snap}` : null, () => api.diff('snapshot', snap!), { locking: false });
  const search = patchLocation?.toString() ?? '';
  const location = useMemo(() => parseLocation(search), [search]);
  const groups = round?.assessment?.groups ?? [];
  // Finding discussions hang off the original round; a confirmation round points back to it.
  const original = round?.metadata.kind === 'review';
  const findings = useFindings(original ? id : null);
  const annotations: Annotation[] = groups.flatMap((g) => g.members.map((m) => ({ finding: m.finding, seat: m.seat, anchor: m.finding.side === 'candidate' ? 'new' as const : 'old' as const })));
  const findingLink = (snapshotId: string, finding: Finding) => pasted
    ? `${to('reviews', id)}?${new URLSearchParams({ side: finding.side, path: finding.path, line: String(finding.line_start), end: String(finding.line_end) })}`
    : fileLink(snapshotId, finding);
  return (
    <Page kicker={tx('审核轮 · {0}', shortId(id))} title={round?.metadata.kind === 'confirmation' ? tx('交叉确认') : tx('代码审核')}>
      <ReadFrame read={{ ...read, data: round }} what={tx('审核轮')}>
        {(r) => (
          <>
            <Section title={tx('席位')}>
              <RoundSummary round={r} />
              {r.independent_review ? <IndependentActions round={r} stale={read.stale} />
                : r.metadata.kind === 'confirmation' ? <ConfirmationActions key={r.round_id} round={r} /> : null}
            </Section>
            <Section title={tx('意见')} note={pasted ? tx('点击意见的文件名和行号，跳到补丁中的对应位置。') : tx('点击意见的文件名和行号，打开冻结文件。')}>
              {r.assessment ? <p className="row"><span className="stamp">{tx('审核结论')}</span><Badge tone={ROUND_TONE[r.assessment.status] ?? 'muted'}>{ASSESSMENT_LABEL[r.assessment.status] ?? r.assessment.status}</Badge>{r.assessment.reasons.map((x) => <code key={x} className="stamp">{x}</code>)}</p> : null}
              {r.metadata.kind === 'review' ? (
                <FindingsPanel round={r} findings={findings}
                  fileHref={r.independent_review && !standalone ? undefined : (finding) => findingLink(r.snapshot_id, finding)} />
              ) : (
                <p className="muted">
                  {tx('交叉确认轮不单独列意见。意见、追问和判断都在原审核轮。')}
                  {r.metadata.parent_round_id ? <> <Link href={to('reviews', r.metadata.parent_round_id)}>{tx('返回原审核轮')}</Link></> : null}
                </p>
              )}
              {r.confirmation_round_id ? <p><Link href={to('reviews', r.confirmation_round_id)}>{tx('查看交叉确认')}</Link></p> : null}
            </Section>
            <ReviewSessionLogs round={r} artifacts={read.data?.artifacts ?? []} />
            {standalone ? <UserTests input={standalone} /> : null}
            <Section title={tx('冻结的审核输入')} note={r.seats.length > 1 ? tx('两位审核者看到的是同一份。') : undefined}>
              <ArtifactLinks artifacts={read.data?.artifacts.filter((artifact) => !artifact.key?.startsWith('session-')) ?? []}
                label={(a) => tx('冻结日志 · 命令 {0} · {1}', a.coordinates?.frozen_index ?? '', a.stream ?? a.key ?? tx('日志'))} />
              <ReadFrame read={diff} what={tx('冻结快照的 diff')}>
                {(d) => <>
                  <DiffView diff={d.diff} annotations={annotations} location={pasted ? location : undefined}
                    onOpenFinding={pasted || (r.independent_review && !standalone) ? undefined : (a) => navigate(fileLink(r.snapshot_id, a.finding))}
                    emptyNote={<EmptyState compact title={tx('没有可看的 diff')} body={d.unavailable_reason ?? tx('这份快照没有 diff。')} />} />
                  {pasted && d.diff !== null ? <details className="raw"><summary>{tx('补丁原文')}</summary><pre>{d.diff}</pre></details> : null}
                </>}
              </ReadFrame>
            </Section>
          </>
        )}
      </ReadFrame>
    </Page>
  );
}
