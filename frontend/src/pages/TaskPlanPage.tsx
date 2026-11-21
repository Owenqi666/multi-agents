import { useCallback, useRef, useState } from 'react';
import { useJobs } from '../api/jobs';
import type { PlanningView, Workspace } from '../api/types';
import { useWorkspace } from '../api/workspace';
import { VERIFICATION_LABEL } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { candidatesFor, ModelPicker, requestFrom, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { EnvironmentHint } from './GoalPages';
import { Facts, NextNote, Page, Raw, Section, useResource } from './common';

export function OpenTaskPlanPage() {
  const [id, setId] = useState('');
  return (
    <Page title="打开单卡规划">
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
  return (
    <Page title="单卡规划" aside={<Link className="btn small" href="/task-plans">打开其他规划</Link>}>
      <ReadFrame read={{ ...read, data: plan }} what="单卡规划">
        {(record) => (
          <>
            <Section title="卡片" note={<NextNote next={record.next_action} />}>
              <Facts rows={[
                ['规划运行 ID', <code className="task-data">{record.planning_run_id}</code>],
                ['目标', typeof record.card?.goal === 'string' ? record.card.goal : '—'],
                ['核验', <Badge tone={record.verification === 'verified' ? 'ok' : 'warn'}>{VERIFICATION_LABEL[record.verification] ?? record.verification}</Badge>],
              ]} />
              {record.card ? <Raw value={record.card} label="完整任务卡" /> : null}
              {record.task_run_id ? <div><Link className="btn" href={to('tasks', record.task_run_id)}>实施详情</Link></div> : null}
            </Section>
            {record.dispatch_configurable === true && !record.task_run_id ? <DispatchForm planId={id} stale={read.stale} /> : null}
          </>
        )}
      </ReadFrame>
    </Page>
  );
}
