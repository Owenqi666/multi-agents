// One implementation run (a direct stage's card, or a leaf): attempts, tests, reviews, and
// the next step the readback offers. Buttons follow next_action and the gate fields only.

import { useState } from 'react';
import type { TaskView } from '../api/types';
import { useArchiveMode, useWorkspace } from '../api/workspace';
import { shortId, timeText, VERIFICATION_LABEL } from '../app/labels';
import { Link, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { ReadFrame } from '../components/ReadNote';
import { TaskAttemptEvidence } from './TaskAttemptEvidence';
import { currentTaskReview, ReviewRetry, RoundSummary, useCommands, useSeats } from './bits';
import { ArtifactLinks, Evidence, Facts, NextNote, Page, Section, useResource } from './common';

const PHASE: Record<string, string> = {
  ready: '等待实施', implementing: '实施中', awaiting_delivery: '等待交付', delivered: '已交付', failed: '失败', cancelled: '已取消',
  planning_required: '需要先规划',
};

export function TaskPanel({ task, detail = false }: { task: TaskView; detail?: boolean }) {
  const ws = useWorkspace();
  const archive = useArchiveMode();
  const next = archive ? null : task.next_action;
  const kind = next?.kind ?? null;
  const card = task.details?.input?.card;
  const available = task.details?.input?.frozen_rules?.rules?.commands?.map((c) => c.name) ?? [];
  const cmd = useCommands(available, card?.test_refs ?? []);
  const seats = useSeats(ws.data);
  const lastRound = currentTaskReview(task);
  const retryRound = lastRound?.confirmation_round_id
    ? task.reviews.find((round) => round.round_id === lastRound.confirmation_round_id)
    : lastRound;
  const attempt = task.implementation_attempt;
  const reworked = task.attempts.some((a) => a.request.kind === 'task_rework_started');
  return (
    <>
      <Section title="实施" note={<NextNote next={next} />}>
        <Facts rows={[
          ['阶段', PHASE[task.phase] ?? task.phase],
          ['核验', <Badge tone={task.verification === 'verified' ? 'ok' : 'warn'}>{VERIFICATION_LABEL[task.verification] ?? task.verification}</Badge>],
          ['候选', task.candidate ? <code>{shortId(task.candidate, 10)}</code> : '还没有'],
          ['改动', task.candidate ? <span className="row"><Link href={to('diffs', 'task', task.task_run_id)}>累计 diff</Link>{reworked ? <Link href={`${to('diffs', 'task', task.task_run_id)}?against=parent`}>返工增量</Link> : null}</span> : '—'],
          ['预算', task.budget ? `还剩 ${task.budget.remaining} 次` : '—'],
          ['返工', task.rework_used ? '已用过一次' : '还没用'],
        ]} />
        {task.attempts.length ? (
          <ol className={detail ? 'task-attempts' : 'attempt-list'} aria-label="实施尝试">
            {task.attempts.map((a, i) => detail ? (
              <TaskAttemptEvidence key={i} attempt={a} number={a.runtime?.number ?? i + 1} />
            ) : (
              <li key={i}>
                <span>{a.request.kind === 'task_rework_started' ? '返工' : `第 ${i + 1} 次实施`}</span>
                <Evidence reportedModel={a.runtime?.reported_model} reportedEffort={a.runtime?.reported_effort} request={a.request.decision.choice} />
                <span className="stamp">{timeText(a.request.started_at)}</span>
                {a.runtime?.status ? <span className="stamp">{a.runtime.status}</span> : null}
              </li>
            ))}
          </ol>
        ) : null}
        <div className="form-actions">
          {detail ? null : <Link className="btn" href={to('tasks', task.task_run_id)}>实施详情</Link>}
          {kind === 'run' && !task.attempts.length ? (
            <ActionButton action="task.run" primary label="开始实施" args={() => ({ planning_run_id: task.planning_run_id })} />
          ) : null}
          {kind === 'run' && task.attempts.length ? (
            <ActionButton action="task.retry" primary label="重试实施" args={() => ({ planning_run_id: task.planning_run_id })} />
          ) : null}
          {kind === 'budget_exhausted' || kind === 'reconciliation_required' || (kind === 'user_decision' && !lastRound) ? <p className="action-why"><NextNote next={next} /></p> : null}
        </div>
      </Section>

      <Section title="测试" note="工作台运行登记过的命令，不用模型。">
        {task.tests.length ? (
          <ul className="plain-list">
            {task.tests.map((t) => (
              <li key={`${t.implementation_attempt}-${t.test_attempt}`} className="row">
                <span>实施 {t.implementation_attempt} · 测试 {t.test_attempt}</span>
                <Badge tone={t.verification === 'verified' && t.commands.every((c) => c.status === 'passed') ? 'ok' : 'warn'}>{t.commands.map((c) => `${c.name} ${c.status === 'passed' ? '通过' : c.status}`).join('，')}</Badge>
                <TestLogs taskRunId={task.task_run_id} implementation={t.implementation_attempt} test={t.test_attempt} />
              </li>
            ))}
          </ul>
        ) : <p className="muted">还没有测试。</p>}
        {kind === 'task_test' && attempt !== null ? (
          <div className="form-actions column">
            {cmd.form}
            <ActionButton action="task.test" primary label="运行测试" disabled={!cmd.commands.length} reason="至少选一条命令。"
              args={() => ({ task_run_id: task.task_run_id, implementation_attempt: attempt, commands: cmd.commands, declaration_source: 'PM' })} />
          </div>
        ) : null}
      </Section>

      <Section title="代码审核" note="两席同时开始、互不可见；结论按两席的意见汇总。">
        {task.reviews.length ? task.reviews.map((r) => <RoundSummary key={r.round_id} round={r} />) : <p className="muted">还没有审核。</p>}
        {kind === 'task_review' && attempt !== null && next?.test_attempt != null ? (
          <div className="form-actions column">
            {seats.form}
            <ActionButton action="task.review" primary label="开始审核" disabled={!seats.valid} reason="两席的模型要不同。"
              args={() => ({ task_run_id: task.task_run_id, implementation_attempt: attempt, test_attempt: next.test_attempt, seats: seats.seats, ...(seats.autoRework ? {} : { auto_rework: false }) })} />
          </div>
        ) : null}
        <div className="form-actions">
          <ReviewRetry round={retryRound} />
          {lastRound && lastRound.seats.length === 2 && !lastRound.confirmation_round_id && lastRound.status === 'ready_for_assessment' && lastRound.assessment && lastRound.assessment.status !== 'ready' ? (
            <ActionButton action="task.confirm" label="请两席交叉确认" args={() => ({ round_id: lastRound.round_id })} />
          ) : null}
          {kind === 'rework' && next?.review_round_id ? (
            <ActionButton action="task.rework" primary label="按阻塞意见返工" args={() => ({ round_id: next.review_round_id })} />
          ) : null}
          {kind === 'user_decision' && lastRound ? <p className="action-why"><NextNote next={next} /></p> : null}
        </div>
      </Section>
    </>
  );
}

/** Test logs come from the task-test record, which takes the repository lock: read only on
 *  demand and never while a job runs. */
function TestLogs({ taskRunId, implementation, test }: { taskRunId: string; implementation: number; test: number }) {
  const [open, setOpen] = useState(false);
  const read = useResource<unknown>('task-test', taskRunId, { implementation_attempt: implementation, test_attempt: test }, { enabled: open });
  if (!open) return <button type="button" className="link" onClick={() => setOpen(true)}>日志</button>;
  return (
    <ReadFrame read={read} what="测试日志">
      {(r) => <ArtifactLinks artifacts={r.artifacts} locking
        label={(a) => a.coordinates ? `命令 ${String(a.coordinates.sequence ?? '')} ${a.stream ?? ''}` : a.key ?? a.stream ?? '日志'} />}
    </ReadFrame>
  );
}

export function TaskPage({ id }: { id: string }) {
  const read = useResource<TaskView>('task', id, { implementation: 1 });
  const task = read.data?.id === id ? read.data.data : null;
  return (
    <Page kicker={`实施 · ${shortId(id)}`} title="实施和审核">
      <ReadFrame read={{ ...read, data: task }} what="实施记录">
        {(t) => <TaskPanel key={id} task={t} detail />}
      </ReadFrame>
    </Page>
  );
}
