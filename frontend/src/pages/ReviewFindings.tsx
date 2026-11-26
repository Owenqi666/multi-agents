// 意见: the findings of an original review round, grouped by frozen location, with what the
// two seats said about each other and what the user did with each finding afterwards.
//
// A finding is identified by its anchor (round, snapshot, seat, attempt, finding id). Two
// reviewers may give their findings the same id, so everything here (the user's decision, a
// follow-up question, a linked correction) is keyed by the anchor of the chosen seat and
// sends back the anchor exactly as the service returned it.

import { useRef, useState } from 'react';
import { api } from '../api/client';
import { useRead, type ReadState } from '../api/reads';
import { ApiError, errorText } from '../api/transport';
import type {
  Finding, FindingActivityEntry, FindingAnchor, FindingConfirmation, FindingDecision, FindingGroup, FindingMember, Job, ReviewFindings, ReviewRound,
} from '../api/types';
import { useWorkspaceWritesAllowed } from '../api/workspace';
import { shortId, timeText } from '../app/labels';
import { Link, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge, jobEnding, type BadgeTone } from '../components/Badge';
import { joinList, pair, translated, tx } from '../i18n';
import { Evidence } from './common';

export const anchorKey = (a: FindingAnchor): string => `${a.seat}/${a.attempt}/${a.finding_id}`;

/** The address of the single-card planning form prefilled from one finding. */
export function correctionHref(a: FindingAnchor): string {
  return `/task-plans/new?${new URLSearchParams({ review: a.round_id, seat: a.seat, attempt: String(a.attempt), finding: a.finding_id })}`;
}

export function useFindings(roundId: string | null) {
  // Not a locking read: answers and linked plans arrive through jobs, so it is read again when one ends.
  return useRead<ReviewFindings>(roundId ? `findings/${roundId}` : null, () => api.findings(roundId!), { locking: false });
}

const DECISION_LABEL: Record<string, string> = translated({ confirmed: '确认存在', disputed: '存疑', deferred: '暂缓处理' });
const DECISION_TONE: Record<string, BadgeTone> = { confirmed: 'ok', disputed: 'warn', deferred: 'muted' };
const CONFIRM_LABEL: Record<string, string> = translated({ agree: '同意', disagree: '不同意', uncertain: '不确定' });
const CONFIRM_TONE: Record<string, BadgeTone> = { agree: 'ok', disagree: 'fail', uncertain: 'warn' };
const DECISIONS: FindingDecision[] = ['confirmed', 'disputed', 'deferred'];

function lines(f: Pick<Finding, 'line_start' | 'line_end'>): string {
  return f.line_end !== f.line_start ? `${f.line_start}–${f.line_end}` : String(f.line_start);
}

function Entry({ entry }: { entry: FindingActivityEntry }) {
  if (entry.kind === 'decision') {
    return (
      <li className="thread-entry">
        <p className="row">
          <span className="stamp">{tx('你的判断')}</span>
          <Badge tone={DECISION_TONE[entry.decision ?? ''] ?? 'plain'}>{DECISION_LABEL[entry.decision ?? ''] ?? entry.decision}</Badge>
          <span className="stamp">{timeText(entry.created_at)}</span>
        </p>
        <p className="goal-text">{entry.reason}</p>
      </li>
    );
  }
  if (entry.kind === 'answer') {
    return (
      <li className="thread-entry">
        <p className="row"><span className="stamp">{tx('追问')}</span><span className="stamp">{timeText(entry.created_at)}</span></p>
        <p className="goal-text">{entry.question}</p>
        <p className="row">
          <span className="stamp">{tx('原审核者的回答')}</span>
          <Evidence reportedModel={entry.evidence?.reported_model} reportedEffort={entry.evidence?.reported_effort} />
        </p>
        <p className="goal-text">{entry.answer}</p>
      </li>
    );
  }
  if (entry.kind === 'linked_plan' && entry.planning_run_id) {
    return (
      <li className="thread-entry">
        <p className="row">
          <span className="stamp">{tx('关联修复')}</span>
          <Link href={to('task-plans', entry.planning_run_id)}>{tx('单卡规划')} <code>{shortId(entry.planning_run_id)}</code></Link>
          <span className="stamp">{timeText(entry.created_at)}</span>
        </p>
      </li>
    );
  }
  return <li className="thread-entry"><p className="row"><code>{entry.kind}</code><span className="stamp">{timeText(entry.created_at)}</span></p></li>;
}

/** `about` names the finding (its title and seat): every card has the same three controls,
 *  and a list of controls must still tell them apart. */
function DecisionForm({ anchor, about, onRecorded }: { anchor: FindingAnchor; about: string; onRecorded: () => void }) {
  const [decision, setDecision] = useState<FindingDecision>('confirmed');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const submit = async (): Promise<void> => {
    if (busy || !reason.trim()) return;
    setBusy(true);
    setProblem(null);
    try {
      await api.decide(anchor.round_id, anchor, decision, reason.trim());
      setReason('');
      onRecorded();
    } catch (e) {
      // The service says why in its own words (the seat was retried, the archive is read-only).
      setProblem(e instanceof ApiError && e.code === 'invalid_request' ? `${errorText(e)} ${e.message}` : errorText(e));
      onRecorded();
    } finally {
      setBusy(false);
    }
  };
  return (
    <details className="raw finding-form">
      <summary aria-label={pair(tx('记录你的判断'), about)}>{tx('记录你的判断')}</summary>
      <div className="finding-form-body">
        <div className="seg small" role="radiogroup" aria-label={tx('你的判断')}>
          {DECISIONS.map((d) => <button key={d} type="button" role="radio" aria-checked={decision === d} className={decision === d ? 'on' : ''} onClick={() => setDecision(d)}>{DECISION_LABEL[d]}</button>)}
        </div>
        <label className="plan-label">{tx('判断的理由')}
          <textarea className="field big" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
        <div className="form-actions">
          <button type="button" className="btn" disabled={busy || !reason.trim()} onClick={() => void submit()}>{busy ? tx('正在记录…') : tx('记录判断')}</button>
          {!reason.trim() ? <span className="action-why">{tx('先写下理由。')}</span> : null}
          {problem ? <span className="action-why problem" role="alert">{problem}</span> : null}
        </div>
      </div>
    </details>
  );
}

/** Why a follow-up got no answer. A request that was queued and then turned down by the CLI
 *  (the seat was retried meanwhile, so this finding belongs to an attempt that was replaced)
 *  is said apart from a session that failed. */
function followupProblem(job: Job): string {
  const said = job.error?.message ?? tx('退出码 {0}', job.exit_code ?? '—');
  switch (jobEnding(job)) {
    case 'refused': return tx('追问被服务拒绝：{0}。这条意见可能属于已经被补跑替换的那次尝试，下面是重新读取的意见。', said);
    case 'cancelled': return tx('追问已取消，没有回答。');
    case 'interrupted': return tx('追问被中断，没有观测到回答。');
    default: return tx('追问没有得到回答：{0}', said);
  }
}

function FollowupForm({ anchor, about, onChanged }: { anchor: FindingAnchor; about: string; onChanged: () => void }) {
  const [question, setQuestion] = useState('');
  const [failed, setFailed] = useState<Job | null>(null);
  const sent = useRef('');
  return (
    <details className="raw finding-form">
      <summary aria-label={pair(tx('追问原审核者'), about)}>{tx('追问原审核者')}</summary>
      <div className="finding-form-body">
        <p className="section-note">{tx('追问用这个席位原来的模型和同一份冻结资料开一轮新的只读会话。它看不到之前的问答，请把问题写完整；回答不改变原报告和结论。')}</p>
        <label className="plan-label">{tx('要问的问题')}
          <textarea className="field big" rows={2} value={question} onChange={(e) => setQuestion(e.target.value)} />
        </label>
        <div className="form-actions">
          <ActionButton action="review.followup" label={tx('提交追问')} disabled={!question.trim()} reason={tx('先写下问题。')}
            args={() => { sent.current = question; return { review_anchor: anchor, question }; }}
            onSubmitted={() => setFailed(null)}
            onRejected={onChanged}
            // Only the question that was sent is cleared; one typed while the job ran stays.
            // Whatever the ending, the findings are read again: the answer, or a seat that moved on.
            onDone={(job) => { if (jobEnding(job) !== 'done') setFailed(job); else setQuestion((now) => (now === sent.current ? '' : now)); onChanged(); }} />
          {failed ? <span className="action-why problem" role="alert">{followupProblem(failed)} <Link href={to('jobs', failed.job_id)}>{tx('查看那个作业')}</Link></span> : null}
        </div>
      </div>
    </details>
  );
}

function Member({ member, confirmations, fileHref, onChanged, actions }: {
  member: Pick<FindingMember, 'seat' | 'finding'> & Partial<FindingMember>;
  confirmations: FindingConfirmation[];
  fileHref: ((finding: Finding) => string | null) | undefined;
  onChanged: () => void;
  actions: boolean;
}) {
  const f = member.finding;
  const blocking = f.severity === 'P0' || f.severity === 'P1';
  const href = fileHref?.(f) ?? null;
  // The link reads path:first-line, as findings are cited everywhere else.
  const place = `${f.path}:${f.line_start}`;
  const anchor = member.anchor;
  const activity = member.activity ?? [];
  const about = tx('{0}（{1}）', f.title, member.seat);
  return (
    <li className="finding-card" data-seat={member.seat} data-finding={f.id}>
      <div className="finding-card-head">
        <Badge tone={blocking ? 'fail' : 'warn'}>{f.severity}</Badge>
        <span className="finding-title">{f.title}</span>
        <span className="stamp">{tx('发现者')} <code>{member.seat}</code></span>
        <span className="stamp">{tx('意见 ID')} <code>{f.id}</code></span>
        {href ? <Link href={href}>{place}</Link> : <code className="task-data">{place}</code>}
        {f.line_end !== f.line_start ? <span className="stamp">{tx('到第 {0} 行', f.line_end)}</span> : null}
        {f.reference_status === 'unverified' ? <span className="finding-ref">{tx('引用没有对上冻结文件')}</span> : null}
      </div>
      <p className="finding-body">{f.impact}</p>
      {f.rationale ? <p className="muted">{f.rationale}</p> : null}
      {confirmations.length ? (
        <ul className="plain-list" aria-label={tx('交叉确认')}>
          {confirmations.map((c, i) => (
            <li key={i}>
              <span className="stamp">{tx('交叉确认')} <code>{c.seat}</code></span>
              <Badge tone={CONFIRM_TONE[c.decision] ?? 'plain'}>{CONFIRM_LABEL[c.decision] ?? c.decision}</Badge>
              <span>{c.reason}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {activity.length ? <ol className="finding-thread" aria-label={tx('处理记录')}>{activity.map((entry) => <Entry key={entry.id} entry={entry} />)}</ol> : null}
      {actions && anchor ? (
        <div className="finding-actions">
          <DecisionForm anchor={anchor} about={about} onRecorded={onChanged} />
          <FollowupForm anchor={anchor} about={about} onChanged={onChanged} />
          <Link className="btn small" href={correctionHref(anchor)} aria-label={pair(tx('发起关联修复'), about)}>{tx('发起关联修复')}</Link>
        </div>
      ) : null}
    </li>
  );
}

function reports(group: Pick<FindingGroup, 'independent_reports'>): string | null {
  const entries = Object.entries(group.independent_reports ?? {});
  if (entries.length < 2) return null;
  const reported = entries.filter(([, v]) => v === 'reported').map(([seat]) => seat);
  if (reported.length === entries.length) return tx('各席都独立报告了这个位置（相同证据）。');
  return tx('只有 {0} 独立报告了这个位置。', joinList(reported));
}

/** The findings of a review round. `findings` is the discussion read; until it arrives (or
 *  when the service does not have it) the groups of the round's own assessment are shown,
 *  without the per-finding actions. */
export function FindingsPanel({ round, findings, fileHref }: {
  round: ReviewRound;
  findings: ReadState<ReviewFindings> & { reload: () => void };
  fileHref?: (finding: Finding) => string | null;
}) {
  const writesAllowed = useWorkspaceWritesAllowed();
  const data = findings.data?.round_id === round.round_id ? findings.data : null;
  const groups: (Omit<FindingGroup, 'members'> & { members: (Pick<FindingMember, 'seat' | 'finding'> & Partial<FindingMember>)[] })[] =
    data?.groups ?? round.assessment?.groups ?? [];
  const missing = findings.error instanceof ApiError && findings.error.status === 404;
  if (!groups.length) return <p className="muted">{tx('没有意见。')}</p>;
  return (
    <>
      {data ? <p className="section-note wide">{tx('上面是审核结论；下面每条意见里「你的判断」只作记录，不改变结论。结论随之后的测试和复审更新。')}</p> : null}
      {findings.error && !missing ? <p className="stale-note problem">{tx('读不到意见的处理记录：{0}', errorText(findings.error))}</p> : null}
      <ul className="finding-groups">
        {groups.map((group, index) => {
          const members = group.members ?? [];
          const first = members[0]?.finding;
          const disputed = (group.confirmations ?? []).some((c) => c.decision !== 'agree');
          const summary = reports(group);
          return (
            <li key={index} className="finding-group">
              <div className="finding-group-head">
                {first ? <code className="task-data">{group.location?.path ?? first.path}:{lines({ line_start: group.location?.line_start ?? first.line_start, line_end: group.location?.line_end ?? first.line_end })}</code> : null}
                {group.blocking ? <Badge tone="fail">{tx('阻塞')}</Badge> : null}
                {members.length > 1 ? <span className="stamp">{tx('{0} 条意见指向这里', members.length)}</span> : null}
                {disputed ? <Badge tone="warn">{tx('交叉确认有分歧')}</Badge> : null}
                {summary ? <span className="stamp">{summary}</span> : null}
              </div>
              <ul className="finding-list">
                {members.map((member) => (
                  <Member key={member.anchor ? anchorKey(member.anchor) : `${member.seat}/${member.finding.id}`} member={member} fileHref={fileHref}
                    confirmations={(group.confirmations ?? []).filter((c) => c.target_seat === member.seat && c.finding_id === member.finding.id)}
                    onChanged={findings.reload} actions={writesAllowed && data !== null} />
                ))}
              </ul>
            </li>
          );
        })}
      </ul>
    </>
  );
}
