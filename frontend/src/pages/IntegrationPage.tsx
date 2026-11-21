// 集成: the merged candidate of a delegate stage (or a direct stage taken to a branch), its
// test, review, one rework per lineage, rebase when the target moved, and acceptance.

import type { ArtifactRef, IntegrationRecord, ReviewRound } from '../api/types';
import { commitOf } from '../api/types';
import { shortId, timeText, VERIFICATION_LABEL } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { ReadFrame } from '../components/ReadNote';
import { integrationPredecessor } from '../stage/integrationLineage';
import { useWorkspace } from './GoalPages';
import { currentIntegrationReview, openIntegrationRevision, ReviewRetry, RoundSummary, useSeats } from './bits';
import { ArtifactLinks, Facts, Page, Raw, Section, useResource } from './common';

// Retesting takes the old round out of play. Resolve an existing confirmation before
// retesting, because the bridge requires both its original test and a resolved assessment.
const UNRESOLVED_CONFIRMATION = new Set(['confirmation_round_incomplete', 'confirmation_targets_unanswered', 'blocking_confirmation_unresolved']);

function Body({ it, artifacts }: { it: IntegrationRecord; artifacts: ArtifactRef[] }) {
  const ws = useWorkspace();
  const seats = useSeats(ws.data);
  const test = it.test;
  const tested = test?.status === 'succeeded';
  const acceptance = it.acceptance;
  const conflicted = it.status === 'conflicted';
  const actionable = !conflicted && !acceptance && !it.superseded_by && it.target_is_current;
  const parent = integrationPredecessor(it);
  const sourceReview = currentIntegrationReview(it);
  const current = actionable ? sourceReview : undefined;
  const confirmationId = sourceReview?.confirmation_round_id;
  const confirmationRead = useResource<ReviewRound>('review', confirmationId);
  const readRound = confirmationRead.data?.data;
  const confirmation = readRound?.round_id === confirmationId ? readRound ?? null : null;
  const retryRound = current && confirmationId ? confirmation : current;
  const confirmationBlocked = it.reviews.some((round) =>
    round.confirmation_round_id && round.assessment?.reasons.some((reason) => UNRESOLVED_CONFIRMATION.has(reason)));
  const canReview = actionable && test && tested && it.test_evidence_verification === 'verified' && !current && !confirmationBlocked;
  const acceptRound = it.current_eligible && current?.current_eligible ? current : null;
  return (
    <>
      <Section title="候选">
        <Facts rows={[
          ['状态', conflicted ? <Badge tone="fail">合并冲突</Badge> : <Badge tone="ok">已合成</Badge>],
          ['核验', <Badge tone={it.verification === 'verified' ? 'ok' : 'warn'}>{VERIFICATION_LABEL[it.verification] ?? it.verification}</Badge>],
          ['目标', <code>{it.target_ref}</code>],
          ['目标分支', it.target_is_current ? '没有移动' : <Badge tone="warn">目标分支移动过，需要重建</Badge>],
          ['候选', it.candidate ? <code>{shortId(it.candidate.commit, 10)}</code> : '没有'],
          ['改动', it.candidate ? <span className="row"><Link href={to('diffs', 'integration', it.integration_id)}>累计 diff</Link>{parent ? <Link href={`${to('diffs', 'integration', it.integration_id)}?against=parent`}>返工增量</Link> : null}</span> : '—'],
          ['成员', it.members.map((m) => <Link key={m.task_run_id} href={to('tasks', m.task_run_id)}>{m.task_input?.card?.id ?? shortId(m.task_run_id)} </Link>)],
          ['来源', parent ? <Link href={to('integrations', parent)}>{it.rebased_from ? '重建自' : '返工自'} <code>{shortId(parent)}</code></Link> : '—'],
          ['之后', it.superseded_by ? <Link href={to('integrations', it.superseded_by)}>已被 <code>{shortId(it.superseded_by)}</code> 取代</Link> : '—'],
        ]} />
        {conflicted ? <Raw value={it.conflict} label="冲突详情" /> : null}
        {!it.target_is_current ? (
          <ActionButton action="integration.rebase" label="在新的目标上重建" args={() => ({ integration_id: it.integration_id })}
            onDone={(job) => { const id = job.resources?.find((r) => r.kind === 'integration' && r.id !== it.integration_id)?.id; if (id) navigate(to('integrations', id)); }} />
        ) : null}
      </Section>

      <Section title="测试" note="跑全部登记的命令。">
        {test ? <p className="row"><Badge tone={tested ? 'ok' : test.status === 'running' ? 'live' : 'warn'}>测试 {test.attempt}：{tested ? '通过' : test.status}</Badge><span className="stamp">候选 {shortId(commitOf(test.candidate), 7)}</span></p> : <p className="muted">还没有测试。</p>}
        <ArtifactLinks artifacts={artifacts} locking
          label={(a) => `测试 ${a.coordinates?.test_attempt ?? ''} · 命令 ${a.coordinates?.sequence ?? ''} · ${a.stream ?? a.key ?? '日志'}`} />
        {actionable ? <ActionButton action="integration.test" label={test ? '重新测试' : '运行测试'} disabled={confirmationBlocked}
          reason="请先解决交叉确认。" args={() => ({ integration_id: it.integration_id })} /> : null}
      </Section>

      <Section title="审核" note="集成审核的席位键是 seat-1、seat-2。">
        {it.reviews.length ? it.reviews.map((r) => (
          <div key={r.round_id}>
            <p className="stamp">测试 {r.test_attempt}{r.source_current === false ? ' · 历史审核' : ''}</p>
            <RoundSummary round={r} />
          </div>
        )) : <p className="muted">还没有审核。</p>}
        {confirmationId ? (
          <ReadFrame read={{ ...confirmationRead, data: confirmation }} what="交叉确认">
            {(round) => <RoundSummary round={round} />}
          </ReadFrame>
        ) : null}
        {canReview ? (
          <div className="form-actions column">
            {seats.form}
            <ActionButton action="integration.review" primary label="开始审核" disabled={!seats.valid} reason="两席的模型要不同。"
              args={() => ({ integration_id: it.integration_id, test_attempt: test.attempt, seats: seats.seats, ...(seats.autoRework ? {} : { auto_rework: false }) })} onDone={openIntegrationRevision} />
          </div>
        ) : null}
        <div className="form-actions">
          <ReviewRetry round={retryRound} integrationId={it.integration_id} onDone={openIntegrationRevision} />
          {current && current.seats.length === 2 && !confirmationId && current.status === 'ready_for_assessment' && current.assessment?.status !== 'ready' ? (
            <ActionButton action="integration.confirm" label="请两席交叉确认" args={() => ({ integration_id: it.integration_id, round_id: current.round_id })} onDone={openIntegrationRevision} />
          ) : null}
          {current && current.assessment?.status === 'needs_rework' && !it.rework ? (
            <ActionButton action="integration.rework" primary label="按阻塞意见返工（每条谱系一次）" args={() => ({ integration_id: it.integration_id, round_id: current.round_id })}
              onDone={(job) => { const id = (job.result?.child_id as string | undefined) ?? job.resources?.find((r) => r.kind === 'integration' && r.id !== it.integration_id)?.id; if (id) navigate(to('integrations', id)); }} />
          ) : null}
          {it.rework?.child_id && it.rework.child_id !== it.integration_id ? <Link className="btn" href={to('integrations', it.rework.child_id)}>打开返工后的集成</Link> : null}
        </div>
      </Section>

      <Section title="接受" note="接受不影响后面阶段的解锁，只把审核通过的候选记为交付。">
        {acceptance ? (
          <p className="row">
            <Badge tone="ok">已接受</Badge>
            <span className="stamp">{timeText(acceptance.accepted_at)}</span>
            {acceptance.review_round_id ? <Link href={to('reviews', acceptance.review_round_id)}>接受依据 <code>{shortId(acceptance.review_round_id)}</code></Link> : null}
          </p>
        ) : (
          <ActionButton action="integration.accept" primary label="接受" disabled={!acceptRound} reason="审核结论还不是当前测试的通过结论，不能接受。"
            confirm="接受这个集成？" args={() => acceptRound && ({ integration_id: it.integration_id, round_id: acceptRound.round_id })} />
        )}
      </Section>
    </>
  );
}

export function IntegrationPage({ id }: { id: string }) {
  const read = useResource<IntegrationRecord>('integration', id);
  const resource = read.data?.id === id ? read.data : null;
  const data = resource?.data ? { integration: resource.data, artifacts: resource.artifacts } : null;
  return (
    <Page kicker={`集成 · ${shortId(id)}`} title="集成">
      <ReadFrame read={{ ...read, data }} what="集成">
        {({ integration, artifacts }) => <Body key={id} it={integration} artifacts={artifacts} />}
      </ReadFrame>
    </Page>
  );
}
