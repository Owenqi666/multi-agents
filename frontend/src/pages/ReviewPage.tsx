// 审核轮: seats and their evidence, findings grouped by place, and the frozen input as a
// diff with each finding under its lines. Review rounds and snapshots do not take the
// repository lock, so this page also shows seat progress while a review job runs.

import { api } from '../api/client';
import { useRead } from '../api/reads';
import type { Diff, Finding, ReviewRound } from '../api/types';
import { ASSESSMENT_LABEL, shortId } from '../app/labels';
import { Link, to } from '../app/router';
import { Badge } from '../components/Badge';
import { DiffView, type Annotation } from '../components/DiffView';
import { EmptyState } from '../components/EmptyState';
import { ReadFrame } from '../components/ReadNote';
import { navigate } from '../app/router';
import { ROUND_TONE, RoundSummary } from './bits';
import { Page, Section, useResource } from './common';

function fileLink(snapshot: string, f: Finding): string {
  return `${to('snapshots', snapshot, 'file')}?side=${f.side}&path=${encodeURIComponent(f.path)}&line=${f.line_start}&end=${f.line_end}`;
}

export function ReviewPage({ id }: { id: string }) {
  const read = useResource<ReviewRound>('review', id);
  const round = read.data?.data ?? null;
  const snap = round?.snapshot_id ?? null;
  const diff = useRead<Diff>(snap ? `diff/snapshot/${snap}` : null, () => api.diff('snapshot', snap!), { locking: false });
  const groups = round?.assessment?.groups ?? [];
  const annotations: Annotation[] = groups.flatMap((g) => g.members.map((m) => ({ finding: m.finding, seat: m.seat, anchor: m.finding.side === 'candidate' ? 'new' as const : 'old' as const })));
  return (
    <Page kicker={`审核轮 · ${shortId(id)}`} title={round?.metadata.kind === 'confirmation' ? '交叉确认' : '代码审核'}>
      <ReadFrame read={{ ...read, data: round }} what="审核轮">
        {(r) => (
          <>
            <Section title="席位"><RoundSummary round={r} /></Section>
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
