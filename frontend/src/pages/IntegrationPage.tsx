// 集成: the merged candidate of a delegate stage (or a direct stage taken to a branch), its
// test, review, one rework per lineage, rebase when the target moved, and acceptance.

import type { IntegrationRecord } from '../api/types';
import { commitOf } from '../api/types';
import { shortId, timeText, VERIFICATION_LABEL } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { ReadFrame } from '../components/ReadNote';
import { useWorkspace } from './GoalPages';
import { RoundSummary, seatFailed, useSeats } from './bits';
import { Facts, Page, Raw, Section, useResource } from './common';

function Body({ it }: { it: IntegrationRecord }) {
  const ws = useWorkspace();
  const seats = useSeats(ws.data);
  const rounds = it.reviews.filter((r) => r.metadata.kind === 'review');
  const last = rounds[rounds.length - 1];
  const failedSeats = last ? last.seats.filter((s) => seatFailed(last, s.key)).map((s) => s.key) : [];
  const tested = it.test?.status === 'succeeded';
  const accepted = it.acceptance !== null;
  const conflicted = it.status === 'conflicted';
  const goChild = (job: { result: Record<string, unknown> | null }) => {
    const child = (job.result?.revision as { integration_id?: string } | undefined)?.integration_id;
    if (child) navigate(to('integrations', child));
  };
  return (
    <>
      <Section title="候选">
        <Facts rows={[
          ['状态', conflicted ? <Badge tone="fail">合并冲突</Badge> : <Badge tone="ok">已合成</Badge>],
          ['核验', <Badge tone={it.verification === 'verified' ? 'ok' : 'warn'}>{VERIFICATION_LABEL[it.verification] ?? it.verification}</Badge>],
          ['目标', <code>{it.target_ref}</code>],
          ['目标分支', it.target_is_current ? '没有移动' : <Badge tone="warn">目标分支移动过，需要重建</Badge>],
          ['候选', it.candidate ? <code>{shortId(it.candidate.commit, 10)}</code> : '没有'],
          ['改动', it.candidate ? <span className="row"><Link href={to('diffs', 'integration', it.integration_id)}>累计 diff</Link>{it.rebased_from || it.rework ? <Link href={`${to('diffs', 'integration', it.integration_id)}?against=parent`}>返工增量</Link> : null}</span> : '—'],
          ['成员', it.members.map((m) => <Link key={m.task_run_id} href={to('tasks', m.task_run_id)}>{m.task_input?.card?.id ?? shortId(m.task_run_id)} </Link>)],
          ['来源', it.rebased_from ? <Link href={to('integrations', it.rebased_from)}>重建自 <code>{shortId(it.rebased_from)}</code></Link> : '—'],
          ['之后', it.superseded_by ? <Link href={to('integrations', it.superseded_by)}>已被 <code>{shortId(it.superseded_by)}</code> 取代</Link> : '—'],
        ]} />
        {conflicted ? <Raw value={it.conflict} label="冲突详情" /> : null}
        {!it.target_is_current ? (
          <ActionButton action="integration.rebase" label="在新的目标上重建" args={() => ({ integration_id: it.integration_id })}
            onDone={(job) => { const id = job.resources?.find((r) => r.kind === 'integration' && r.id !== it.integration_id)?.id; if (id) navigate(to('integrations', id)); }} />
        ) : null}
      </Section>

      <Section title="测试" note="跑全部登记的命令。">
        {it.test ? <p className="row"><Badge tone={tested ? 'ok' : it.test.status === 'running' ? 'live' : 'warn'}>测试 {it.test.attempt}：{tested ? '通过' : it.test.status}</Badge><span className="stamp">候选 {shortId(commitOf(it.test.candidate), 7)}</span></p> : <p className="muted">还没有测试。</p>}
        {!conflicted && !accepted ? <ActionButton action="integration.test" label={it.test ? '重新测试' : '运行测试'} args={() => ({ integration_id: it.integration_id })} /> : null}
      </Section>

      <Section title="审核" note="集成审核的席位键是 seat-1、seat-2。">
        {it.reviews.length ? it.reviews.map((r) => <RoundSummary key={r.round_id} round={r} />) : <p className="muted">还没有审核。</p>}
        {tested && !accepted && !last ? (
          <div className="form-actions column">
            {seats.form}
            <ActionButton action="integration.review" primary label="开始审核" disabled={!seats.valid} reason="两席的模型要不同。"
              args={() => ({ integration_id: it.integration_id, seats: seats.seats, ...(seats.autoRework ? {} : { auto_rework: false }) })} onDone={goChild} />
          </div>
        ) : null}
        <div className="form-actions">
          {last && failedSeats.length && !accepted ? (
            <ActionButton action="integration.retry_review" label={`补跑失败的席位（${failedSeats.join('、')}）`} args={() => ({ integration_id: it.integration_id, round_id: last.round_id, seat_keys: failedSeats })} />
          ) : null}
          {last && last.seats.length === 2 && !last.confirmation_round_id && !failedSeats.length && !accepted && last.assessment?.status !== 'ready' ? (
            <ActionButton action="integration.confirm" label="请两席交叉确认" args={() => ({ integration_id: it.integration_id, round_id: last.round_id })} />
          ) : null}
          {last && last.assessment?.status === 'needs_rework' && !it.rework && !accepted ? (
            <ActionButton action="integration.rework" primary label="按阻塞意见返工（每条谱系一次）" args={() => ({ integration_id: it.integration_id, round_id: last.round_id })}
              onDone={(job) => { const id = (job.result?.child_id as string | undefined) ?? job.resources?.find((r) => r.kind === 'integration' && r.id !== it.integration_id)?.id; if (id) navigate(to('integrations', id)); }} />
          ) : null}
          {it.rework?.child_id ? <Link className="btn" href={to('integrations', it.rework.child_id)}>打开返工后的集成</Link> : null}
        </div>
      </Section>

      <Section title="接受" note="接受不影响后面阶段的解锁，只把审核通过的候选记为交付。">
        {accepted ? <p className="row"><Badge tone="ok">已接受</Badge><span className="stamp">{timeText(it.acceptance!.accepted_at)}</span></p> : (
          <ActionButton action="integration.accept" primary label="接受" disabled={!it.current_eligible || !last} reason="审核结论还不是当前测试的通过结论，不能接受。"
            confirm="接受这个集成？" args={() => ({ integration_id: it.integration_id, round_id: last!.round_id })} />
        )}
      </Section>
    </>
  );
}

export function IntegrationPage({ id }: { id: string }) {
  const read = useResource<IntegrationRecord>('integration', id);
  return (
    <Page kicker={`集成 · ${shortId(id)}`} title="集成">
      <ReadFrame read={{ ...read, data: read.data?.data ?? null }} what="集成">
        {(it) => <Body it={it} />}
      </ReadFrame>
    </Page>
  );
}
