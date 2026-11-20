import { useCallback, useState } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import type { Goal, Project, RunItem } from '../api/types';
import { shortId, timeText } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { EmptyState } from '../components/EmptyState';
import { ModelPicker, requestFrom, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { EnvironmentHint, useWorkspace } from './GoalPages';
import { Facts, Page, Section, useResource } from './common';

export function ProjectsPage() {
  const all = useRead<{ items: RunItem[] }>('runs/project', () => api.runs('project'), { locking: false, pollWhileJob: true });
  // Filter here as well: older bridges ignored the kind query.
  const runs = { ...all, data: all.data ? { items: all.data.items.filter((i) => i.kind === 'project') } : null };
  return (
    <Page kicker="项目" title="项目" aside={<Link className="btn small primary" href="/new">提出目标</Link>}>
      <ReadFrame read={runs} what="项目列表">
        {(r) => !r.items.length ? (
          <EmptyState title="还没有项目" body="确认第一个目标时会新建项目。在终端里用 CLI 建的项目不会出现在这里，可以用它的 ID 直接打开。" action={<Link className="btn primary" href="/new">提出目标</Link>} />
        ) : (
          <ul className="plain-list">
            {r.items.map((p) => <li key={p.id}><Link href={to('projects', p.id)}>{p.label}</Link> <span className="stamp">{timeText(p.updated_at)}</span></li>)}
          </ul>
        )}
      </ReadFrame>
    </Page>
  );
}

const PLAN_STATUS: Record<string, [Parameters<typeof Badge>[0]['tone'], string]> = {
  running: ['live', '生成中'], succeeded: ['ok', '已生成'], failed: ['fail', '失败'],
};

function PlanRow({ plan }: { plan: Project['plans'][number] }) {
  const [tone, word] = PLAN_STATUS[plan.status] ?? ['muted', plan.status];
  return (
    <li className="plan-row">
      <Badge tone={plan.finished_at === null ? 'live' : tone}>{plan.finished_at === null ? '进行中' : word}</Badge>
      <Link href={to('plans', plan.plan_id)}>计划 <code>{shortId(plan.plan_id)}</code></Link>
      {plan.origin === 'pm' ? <span className="stamp">你手改的版本，源自 <code>{shortId(plan.source_plan_id)}</code></span> : null}
      <span className="stamp">{timeText(plan.finished_at ?? plan.started_at)}</span>
    </li>
  );
}

function GoalBlock({ goal, plans }: { goal: Goal; plans: Project['plans'] }) {
  const ws = useWorkspace();
  const [planner, setPlanner] = useState<PickerChoice | null>(null);
  const onPlanner = useCallback((v: PickerChoice | null) => setPlanner(v), []);
  const req = requestFrom(ws.data, 'planner', planner);
  const mine = plans.filter((p) => p.goal_id === goal.goal_id);
  const { active } = useJobs();
  const planning = active && (active.action === 'project.plan') && (active.args as { goal_id?: string } | null)?.goal_id === goal.goal_id;
  return (
    <Section title="目标" note={goal.consultation ? (goal.consultation.adoption === 'accepted' ? '原样采纳了顾问的草稿' : '改过顾问的草稿后确认') : '直接确认'}>
      <pre className="goal-text">{goal.confirmed_goal}</pre>
      <Facts rows={[
        ['确认时间', timeText(goal.confirmed_at)],
        ['基线', <code>{shortId(goal.baseline_commit, 10)}</code>],
        ['需求咨询', goal.consultation ? <Link href={to('consultations', goal.consultation.consultation_id)}>查看</Link> : '没有'],
      ]} />
      <h3 className="sub-head">计划</h3>
      {mine.length ? <ul className="plain-list">{mine.map((p) => <PlanRow key={p.plan_id} plan={p} />)}</ul> : <p className="muted">还没有计划。</p>}
      {planning ? <p className="stale-note">计划生成中，最多 300 秒；上面的列表会出现进行中的计划。</p> : null}
      <EnvironmentHint ws={ws.data} />
      <div className="form-actions">
        <ModelPicker ws={ws.data} role="planner" value={planner} onChange={onPlanner} label="规划者" />
        <ActionButton action="project.plan" primary label={mine.length ? '再生成一版计划' : '生成计划'} disabled={!req} reason="先选好规划者。"
          confirm={mine.length ? '每次提交都会新建一份计划，确定再生成一版？' : undefined}
          args={() => ({ goal_id: goal.goal_id, planner: req })}
          onDone={(job) => { const id = job.resources?.find((r) => r.kind === 'project-plan')?.id; if (id) navigate(to('plans', id)); }} />
      </div>
    </Section>
  );
}

export function ProjectPage({ id }: { id: string }) {
  const read = useResource<Project>('project', id);
  return (
    <Page kicker={`项目 · ${shortId(id)}`} title="项目">
      <ReadFrame read={{ ...read, data: read.data?.data ?? null }} what="项目">
        {(p) => p.goals.length ? (
          <>{p.goals.slice().reverse().map((g) => <GoalBlock key={g.goal_id} goal={g} plans={p.plans} />)}</>
        ) : <EmptyState title="这个项目还没有目标" action={<Link className="btn primary" href="/new">提出目标</Link>} />}
      </ReadFrame>
    </Page>
  );
}

export function GoalPage({ id }: { id: string }) {
  const read = useResource<Goal>('goal', id);
  return (
    <Page kicker={`目标 · ${shortId(id)}`} title="目标">
      <ReadFrame read={{ ...read, data: read.data?.data ?? null }} what="目标">
        {(g) => (
          <Section title="确认的目标">
            <pre className="goal-text">{g.confirmed_goal}</pre>
            <p><Link className="btn" href={to('projects', g.project_id)}>打开项目</Link></p>
          </Section>
        )}
      </ReadFrame>
    </Page>
  );
}
