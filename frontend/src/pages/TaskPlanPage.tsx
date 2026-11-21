import { useCallback, useRef, useState } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import type { PlanningView, RunItem, Workspace } from '../api/types';
import { useArchiveMode, useWorkspace, useWorkspaceWritesAllowed } from '../api/workspace';
import { shortId, timeText, VERIFICATION_LABEL } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { candidatesFor, ModelPicker, requestFrom, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { EnvironmentHint } from './GoalPages';
import { Facts, NextNote, Page, Raw, Section, useResource } from './common';
import { PlanningAttempts } from './PlanningAttempts';

const PHASE: Record<string, string> = {
  pending: '等待规划', drafting: '草拟中', reviewing: '复核中', awaiting_validation: '等待核验',
  awaiting_review: '等待复核', revision_required: '需要修改', reconciliation_required: '待对账',
  failed: '失败', cancelling: '正在取消', cancelled: '已取消', ready: '已复核', bound: '已绑定实施',
};

export function OpenTaskPlanPage() {
  const [id, setId] = useState('');
  const writesAllowed = useWorkspaceWritesAllowed();
  const history = useRead<{ items: RunItem[] }>('runs/plan', () => api.runs('plan'), { locking: false, pollWhileJob: true });
  return (
    <Page title="打开单卡规划" aside={writesAllowed ? <Link className="btn primary" href="/task-plans/new">新建单卡规划</Link> : undefined}>
      <Section title="已有规划" note="输入在这个仓库中生成的规划运行 ID。">
        <form className="task-plan-open form-actions" onSubmit={(event) => {
          event.preventDefault();
          if (id.trim()) navigate(to('task-plans', id.trim()));
        }}>
          <label>规划运行 ID
            <input className="field mono" value={id} onChange={(event) => setId(event.target.value)} required />
          </label>
          <button className="btn primary" type="submit" disabled={!id.trim()}>打开规划</button>
        </form>
      </Section>
      <Section title="规划历史">
        <ReadFrame read={history} what="规划历史">
          {({ items }) => {
            const plans = items.filter((item) => item.kind === 'plan');
            return plans.length ? <ul className="plain-list" aria-label="规划历史">
              {plans.map((plan) => <li key={plan.id}>
                <Link href={to('task-plans', plan.id)}>{plan.label} <code>{shortId(plan.id)}</code></Link>
                <span className="stamp">{timeText(plan.updated_at)}</span>
              </li>)}
            </ul> : <p className="muted">工作台还没有登记规划。</p>;
          }}
        </ReadFrame>
      </Section>
    </Page>
  );
}

interface Backup {
  id: number;
  choice: PickerChoice | null;
}

function BackupPicker({ backup, index, length, ws, choose, move, remove }: {
  backup: Backup;
  index: number;
  length: number;
  ws: Workspace | null;
  choose: (id: number, value: PickerChoice | null) => void;
  move: (index: number, direction: -1 | 1) => void;
  remove: (id: number) => void;
}) {
  const label = `备选 ${index + 1}`;
  const onChange = useCallback((value: PickerChoice | null) => choose(backup.id, value), [backup.id, choose]);
  return (
    <li className="task-backup">
      <ModelPicker ws={ws} role="fallback" value={backup.choice} onChange={onChange} label={label} />
      <div className="row">
        <button type="button" className="btn small quiet" aria-label={`${label} 上移`} disabled={index === 0} onClick={() => move(index, -1)}>上移</button>
        <button type="button" className="btn small quiet" aria-label={`${label} 下移`} disabled={index === length - 1} onClick={() => move(index, 1)}>下移</button>
        <button type="button" className="btn small quiet" aria-label={`删除${label}`} onClick={() => remove(backup.id)}>删除</button>
      </div>
    </li>
  );
}

function DispatchForm({ planId, stale }: { planId: string; stale: boolean }) {
  const { data: ws } = useWorkspace();
  const { active, settling, submitting } = useJobs();
  const [explicit, setExplicit] = useState(false);
  const [primary, setPrimary] = useState<PickerChoice | null>(null);
  const [backups, setBackups] = useState<Backup[]>([]);
  const nextId = useRef(0);
  const choose = useCallback((id: number, value: PickerChoice | null) => {
    setBackups((items) => items.map((item) => item.id === id ? { ...item, choice: value } : item));
  }, []);
  const remove = (id: number) => setBackups((items) => items.filter((item) => item.id !== id));
  const add = () => {
    const id = nextId.current++;
    setBackups((items) => [...items, { id, choice: null }]);
  };
  const move = (index: number, direction: -1 | 1) => {
    setBackups((items) => {
      const picked = items[index];
      if (!picked) return items;
      return items.toSpliced(index, 1).toSpliced(index + direction, 0, picked);
    });
  };

  const implementation = explicit ? requestFrom(ws, 'implementation', primary) : null;
  const fallbacks = backups.map((backup) => requestFrom(ws, 'fallback', backup.choice));
  const incomplete = (explicit && !implementation) || fallbacks.some((fallback) => fallback === null);
  const busy = stale || active !== null || settling || submitting !== null;

  return (
    <Section title="实施配置" note="主选默认取冻结规则。备选按列表顺序检查，规则不允许的型号会被跳过。">
      {ws?.read_only ? <p className="muted">当前工作区为只读。</p> : (
        <>
          <EnvironmentHint ws={ws} />
          <fieldset className="task-dispatch" aria-label="实施配置" disabled={busy}>
            <label className="check">
              <input type="checkbox" checked={explicit} onChange={(event) => setExplicit(event.target.checked)} /> 指定实施模型
            </label>
            {explicit ? <ModelPicker ws={ws} role="implementation" value={primary} onChange={setPrimary} label="实施模型" /> : null}
            {backups.length ? (
              <ol className="task-backups" aria-label="备选模型链">
                {backups.map((backup, index) => (
                  <BackupPicker key={backup.id} backup={backup} index={index} length={backups.length} ws={ws} choose={choose} move={move} remove={remove} />
                ))}
              </ol>
            ) : null}
            <div className="row">
              <button className="btn" type="button" onClick={add} disabled={!candidatesFor(ws, 'fallback').length}>添加备选</button>
              <ActionButton action="task.run" primary label="开始实施" disabled={incomplete} reason="先选好各项模型和档位。"
                args={() => ({
                  planning_run_id: planId,
                  ...(implementation ? { implementation } : {}),
                  ...(fallbacks.length ? { fallbacks } : {}),
                })}
                onDone={(job) => {
                  if (window.location.pathname !== to('task-plans', planId)) return;
                  const task = job.resources?.find((resource) => resource.kind === 'task');
                  navigate(task ? to('tasks', task.id) : to('jobs', job.job_id));
                }} />
            </div>
          </fieldset>
        </>
      )}
    </Section>
  );
}

export function TaskPlanPage({ id }: { id: string }) {
  const read = useResource<PlanningView>('plan', id, { implementation: 1 });
  const plan = read.data?.id === id ? read.data.data : null;
  const archive = useArchiveMode();
  const writesAllowed = useWorkspaceWritesAllowed();
  return (
    <Page title="单卡规划" aside={<Link className="btn small" href="/task-plans">打开其他规划</Link>}>
      <ReadFrame read={{ ...read, data: plan }} what="单卡规划">
        {(record) => {
          const input = record.details?.input;
          const questions = record.questions ?? [];
          const blocked = questions.some((question) => question.blocks_dispatch && !question.resolution);
          const next = record.next_action;
          return <>
            <Section title="卡片" note={archive ? undefined : blocked ? '回答阻塞问题后新建一份规划。'
              : next?.kind === 'plan_retry' ? next.detail : <NextNote next={next} />}>
              <Facts rows={[
                ['规划运行 ID', <code className="task-data">{record.planning_run_id}</code>],
                ['目标', typeof record.card?.goal === 'string' ? record.card.goal : '—'],
                ['状态', PHASE[record.phase] ?? record.phase],
                ['核验', <Badge tone={record.verification === 'verified' ? 'ok' : 'warn'}>{VERIFICATION_LABEL[record.verification] ?? record.verification}</Badge>],
              ]} />
              {record.details?.validation_error ? <p className="goal-text">{record.details.validation_error}</p> : null}
              {record.details?.source_errors?.length ? <ul className="plain-list">{record.details.source_errors.map((error, index) => <li className="task-data" key={index}>{error}</li>)}</ul> : null}
              {record.card ? <Raw value={record.card} label="完整任务卡" /> : null}
              {record.task_run_id ? <div><Link className="btn" href={to('tasks', record.task_run_id)}>实施详情</Link></div> : null}
              {writesAllowed && record.retry_configurable === true && !record.task_run_id ? (
                <ActionButton action="plan.retry" label="重试规划" disabled={read.stale} args={() => ({ planning_run_id: record.planning_run_id })}
                  onDone={(job) => {
                    if (window.location.pathname === to('task-plans', id) && job.exit_code !== 0 && !job.resources?.some((resource) => resource.kind === 'plan' && resource.id === id)) {
                      navigate(to('jobs', job.job_id));
                    }
                  }} />
              ) : null}
            </Section>
            {questions.length ? (
              <Section title="规划问题">
                <ul className="plan-question-list">
                  {questions.map((question, index) => (
                    <li key={index}>
                      <p className="row"><Badge tone={question.blocks_dispatch && !question.resolution ? 'warn' : 'muted'}>{question.resolution ? '已回答' : question.blocks_dispatch ? '阻塞实施' : '待回答'}</Badge>
                        <span className="task-data">{question.question}</span></p>
                      {question.resolution ? <p className="goal-text">{question.resolution}</p> : null}
                      {writesAllowed && question.blocks_dispatch && !question.resolution && input && record.standalone ? (
                        <Link className="btn small" href={`/task-plans/new?${new URLSearchParams({ from: id, question: String(index) })}`}>回答后新建规划</Link>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </Section>
            ) : null}
            {writesAllowed && !blocked && record.dispatch_configurable === true && !record.task_run_id ? <DispatchForm planId={id} stale={read.stale} /> : null}
            <Section title="规划尝试">
              <PlanningAttempts label="草拟尝试" attempts={record.attempts.draft} request={input?.draft_request} />
              <PlanningAttempts label="复核尝试" attempts={record.attempts.review} request={input?.review_request} />
            </Section>
            {input ? (
              <Section title="冻结输入">
                {input.requirements ? <pre className="goal-text">{input.requirements}</pre> : null}
                {input.frozen_rules?.baseline_commit ? <Facts rows={[[ '基线提交', <code className="task-data">{input.frozen_rules.baseline_commit}</code> ]]} /> : null}
                <Raw value={input} label="完整冻结输入" />
              </Section>
            ) : null}
          </>;
        }}
      </ReadFrame>
    </Page>
  );
}
