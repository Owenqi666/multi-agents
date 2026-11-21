// Pieces shared by the task, stage and integration pages: review seat selection, review
// round summaries, test command selection.

import { useCallback, useState } from 'react';
import type { IntegrationRecord, Job, ReviewRound, RoleRequest, TaskView, Workspace } from '../api/types';
import { ASSESSMENT_LABEL, shortId, VERIFICATION_LABEL } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge, type BadgeTone } from '../components/Badge';
import { candidatesFor, ModelPicker, type PickerChoice } from '../components/ModelPicker';
import { Evidence } from './common';

/** One or two review seats with different models, plus the auto-rework switch. */
export function useSeats(ws: Workspace | null, { independent = false } = {}) {
  const [count, setCount] = useState<1 | 2>(2);
  const [a, setA] = useState<PickerChoice | null>(null);
  const [b, setB] = useState<PickerChoice | null>(null);
  const [autoRework, setAutoRework] = useState(true);
  const onA = useCallback((v: PickerChoice | null) => setA(v), []);
  const onB = useCallback((v: PickerChoice | null) => setB(v), []);
  const list = candidatesFor(ws, 'review');
  const req = (v: PickerChoice | null): RoleRequest | null => {
    const c = list.find((x) => x.model === v?.model);
    return c && v ? { provider: c.provider, model: c.model, effort: v.effort } : null;
  };
  const seats = (count === 2 ? [req(a), req(b)] : [req(a)]).filter((x): x is RoleRequest => x !== null);
  const valid = seats.length === count && (count === 1 || seats[0]!.model !== seats[1]!.model);
  const form = (
    <div className="seats">
      <div className="seg small" role="radiogroup" aria-label="审核席位">
        <button type="button" role="radio" aria-checked={count === 1} className={count === 1 ? 'on' : ''} onClick={() => setCount(1)}>单席</button>
        <button type="button" role="radio" aria-checked={count === 2} className={count === 2 ? 'on' : ''} onClick={() => setCount(2)}>双席盲审</button>
      </div>
      <ModelPicker ws={ws} role="review" value={a} onChange={onA} label="审核席位 1" />
      {count === 2 ? <ModelPicker ws={ws} role="review" value={b} onChange={onB} label="审核席位 2" exclude={a?.model ?? null} /> : null}
      {independent ? null : <label className="check"><input type="checkbox" checked={autoRework} onChange={(e) => setAutoRework(e.target.checked)} /> 需要返工时自动返工、重测和复审（最多一次）</label>}
    </div>
  );
  return { form, seats, valid, autoRework };
}

export const ROUND_TONE: Record<string, BadgeTone> = { ready: 'ok', needs_rework: 'warn', user_decision: 'plain', incomplete: 'muted' };

/** Only the round for the current implementation and test may offer task actions. */
export function currentTaskReview(task: TaskView): ReviewRound | undefined {
  const latest = task.reviews.findLast((round) =>
    round.metadata.kind === 'review' && round.implementation_attempt === task.implementation_attempt);
  return latest?.source_current ? latest : undefined;
}

export function currentIntegrationReview(integration: IntegrationRecord): ReviewRound | undefined {
  const latest = integration.reviews?.findLast((round) => round.metadata.kind === 'review');
  if (integration.superseded_by || !integration.target_is_current) return undefined;
  return latest?.source_current && latest.test_attempt === integration.test?.attempt ? latest : undefined;
}

/** Integration review, confirmation and retry may all produce a reworked revision. */
export function openIntegrationRevision(job: Job) {
  const revision = job.result?.revision as { integration_id?: string } | undefined;
  if (revision?.integration_id) navigate(to('integrations', revision.integration_id));
}

function retryableSeats(round: ReviewRound): string[] {
  return round.seats.filter((seat) => seat.current_attempt === null ||
    seat.attempts.find((attempt) => attempt.number === seat.current_attempt)?.status === 'failed').map((seat) => seat.key);
}

/** Retry the failed or unstarted seats in the frozen round that owns them. */
export function ReviewRetry({ round, integrationId, independent = false, onDone }: {
  round: ReviewRound | null | undefined;
  integrationId?: string;
  independent?: boolean;
  onDone?: (job: Job) => void;
}) {
  if (!round) return null;
  const seatKeys = retryableSeats(round);
  if (!seatKeys.length) return null;
  return (
    <ActionButton action={independent ? 'review.retry' : integrationId ? 'integration.retry_review' : 'task.retry_review'}
      label={`补跑失败的席位（${seatKeys.join('、')}）`}
      args={() => ({ ...(integrationId ? { integration_id: integrationId } : {}), round_id: round.round_id, seat_keys: seatKeys })}
      onDone={onDone} />
  );
}

/** A review round in one block: seats with their evidence, the assessment, a link to it. */
export function RoundSummary({ round }: { round: ReviewRound & { verification?: string; source_current?: boolean; confirmation_round_id?: string | null } }) {
  const confirmation = round.metadata.kind === 'confirmation';
  const status = round.assessment?.status ?? 'incomplete';
  const blocking = round.assessment?.groups.filter((g) => g.blocking).length ?? 0;
  const confirmed = round.status === 'ready_for_assessment';
  return (
    <div className="round">
      <div className="round-head">
        <Link href={to('reviews', round.round_id)}>{confirmation ? '交叉确认' : '审核轮'} <code>{shortId(round.round_id)}</code></Link>
        {confirmation ? <Badge tone={confirmed ? 'ok' : 'muted'}>{confirmed ? '确认完成' : '确认未完成'}</Badge> : (
          <>
            <Badge tone={ROUND_TONE[status] ?? 'muted'}>{ASSESSMENT_LABEL[status] ?? status}</Badge>
            {blocking ? <Badge tone="fail">{blocking} 条阻塞意见</Badge> : null}
          </>
        )}
        {round.seats.length > 1 ? <span className="stamp">{confirmation ? '双席' : '双席，互不可见'}</span> : <span className="stamp">单席</span>}
        {round.verification ? <span className="stamp">{VERIFICATION_LABEL[round.verification] ?? round.verification}</span> : null}
        {round.metadata.rework_from_round_id ? <span className="stamp">返工后的复审</span> : null}
      </div>
      <ul className="seat-list">
        {round.seats.map((s) => {
          const at = s.attempts[s.attempts.length - 1];
          return (
            <li key={s.key}>
              <code>{s.key}</code>
              <Badge tone={!at ? 'muted' : at.status === 'succeeded' ? 'ok' : at.status === 'running' ? 'live' : 'fail'}>{!at ? '未开始' : at.status === 'succeeded' ? '完成' : at.status === 'running' ? '进行中' : '失败'}</Badge>
              <Evidence reportedModel={at?.reported_model} reportedEffort={at?.reported_effort} request={{ model: s.requested_model, effort: s.requested_effort }} />
              {s.attempts.length > 1 ? <span className="stamp">第 {s.attempts.length} 次</span> : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Pick the named test commands to run; defaults to the card's required tests. */
export function useCommands(available: string[], required: string[]) {
  const [picked, setPicked] = useState<string[] | null>(null);
  const value = picked ?? required.filter((r) => available.includes(r));
  const form = available.length ? (
    <div className="commands">
      {available.map((name) => (
        <label key={name} className="check">
          <input type="checkbox" checked={value.includes(name)} onChange={(e) => setPicked(e.target.checked ? [...value, name] : value.filter((v) => v !== name))} />
          <code>{name}</code>{required.includes(name) ? <span className="stamp">卡片要求</span> : null}
        </label>
      ))}
    </div>
  ) : <p className="action-why">任务读回里没有登记测试命令，没法提交测试。</p>;
  return { form, commands: value };
}
