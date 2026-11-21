// 首页：the weave stage, read from the run state of the most recent stage (or, before any
// stage, the most recent project). Read-only: nothing here submits a job.

import { useMemo } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import type { Consultation, Goal, ProjectPlan, RunItem, Stage as StageData } from '../api/types';
import { actionLabel, KIND_LABEL, timeText } from '../app/labels';
import { Link, to } from '../app/router';
import { EmptyState } from '../components/EmptyState';
import { readTime } from '../components/LogBlock';
import { deriveStory, withRunningJob } from '../stage/derive';
import { useIntegrationHistory } from '../stage/integrationLineage';
import { belongsToProjectTimeline, belongsToStage } from '../stage/jobScope';
import { Stage } from '../stage/Stage';
import { NextNote, RecordLink, useResource } from './common';

const HOME_KINDS = ['stage', 'project-plan', 'goal', 'consultation'];

function latestSubject(items: RunItem[]): RunItem | null {
  return items.filter((i) => HOME_KINDS.includes(i.kind)).sort((a, b) =>
    b.updated_at.localeCompare(a.updated_at) || HOME_KINDS.indexOf(a.kind) - HOME_KINDS.indexOf(b.kind)
  )[0] ?? null;
}

export function HomePage() {
  const { active, ready } = useJobs();
  const runs = useRead<{ items: RunItem[] }>('runs', () => api.runs(), { locking: false, pollWhileJob: true });
  const items = runs.data?.items ?? [];
  const subject = latestSubject(items);
  const stageId = subject?.kind === 'stage' ? subject.id : null;

  const stage = useResource<StageData>('stage', stageId);
  const stageData = stage.data?.id === stageId ? stage.data.data : null;
  const integrationHistory = useIntegrationHistory(stageData);
  const planId = stageData?.plan_id ?? (stageId ? subject?.parent_id : null) ?? (subject?.kind === 'project-plan' ? subject.id : null);
  const plan = useResource<ProjectPlan>('project-plan', planId);
  const planData = plan.data?.id === planId ? plan.data.data : null;
  const planRun = items.find((item) => item.kind === 'project-plan' && item.id === planId);
  const goalId = planData?.goal.goal_id ?? stageData?.goal_id ?? planRun?.parent_id ?? (subject?.kind === 'goal' ? subject.id : null);
  const confirmed = useResource<Goal>('goal', goalId, undefined, { enabled: !planId });
  const goal = planData?.goal ?? (confirmed.data?.id === goalId ? confirmed.data.data : null);
  const consultationId = goal?.consultation?.consultation_id ?? (subject?.kind === 'consultation' ? subject.id : null);
  const consultation = useResource<Consultation>('consultation', consultationId);
  const consultationData = consultation.data?.id === consultationId ? consultation.data.data : null;
  const goalRun = items.find((item) => item.kind === 'goal' && item.id === goalId);
  const projectId = goal?.project_id ?? goalRun?.parent_id ?? consultationData?.project_id
    ?? (subject?.kind === 'consultation' ? subject.parent_id : null);
  const stageAction = active && /^(project\.stage\.|task\.|integration\.)/.test(active.action);
  const firstStage = planId && !stageId && active?.action === 'project.stage.start' && active.args?.plan_id === planId;
  const storyJob = active && (
    stageAction
      ? firstStage || (stageId && belongsToStage(active, stageId, stageData, integrationHistory, items))
      : belongsToProjectTimeline(active, { projectId, goalId, planId, consultationId, empty: runs.data !== null && subject === null })
  ) ? active : null;

  const story = useMemo(() => withRunningJob(deriveStory({
    consultation: consultationData,
    goal,
    plan: planData,
    stage: stageData,
    integrationHistory,
  }), storyJob), [consultationData, goal, planData, stageData, integrationHistory, storyJob]);

  if (runs.data && !items.length && !active) {
    return (
      <div className="home">
        <EmptyState title="还没有项目" body="写下想做成的事。顾问会先读它，帮你写成可以确认的目标；之后规划、实施和审核都会画在这里。" action={<Link className="btn primary" href="/new">提出目标</Link>} />
      </div>
    );
  }

  const next = stageData && (stageData.mode === 'direct' ? stageData.implementation?.next_action ?? stageData.planning.next_action : stageData.planning.next_action);
  const loading = (!runs.data && !runs.error) || (!ready && !!stageId)
    || stage.loading || plan.loading || confirmed.loading || consultation.loading;
  return (
    <div className="home">
      {story.beats.length ? (
        <Stage key={subject?.id} story={story} kind="weave" handle="stage" autoplay={false}
          foot={stage.stale && stage.readAt ? `作业运行中，阶段停在 ${readTime(stage.readAt)} 读到的状态` : '泳道和颜色跟着席位；模型名只用运行时报告值'} />
      ) : loading ? (
        <p className="loading">正在读取运行状态…</p>
      ) : null}
      <div className="home-below">
        <section className="home-card">
          <h2>接下来</h2>
          {active ? (
            <p>作业「{actionLabel(active.action)}」正在进行。<Link href={to('jobs', active.job_id)}>查看作业</Link></p>
          ) : stageId ? (
            <>
              <p><NextNote next={next ?? null} fallback="打开阶段查看当前状态。" /></p>
              <Link className="btn" href={to('stages', stageId)}>打开阶段</Link>
            </>
          ) : planId ? (
            <Link className="btn" href={to('plans', planId)}>打开项目计划</Link>
          ) : goal ? (
            <>
              <pre className="goal-text">{goal.confirmed_goal}</pre>
              <Link className="btn" href={to('projects', goal.project_id)}>前往项目生成计划</Link>
            </>
          ) : consultationId ? (
            <Link className="btn" href={to('consultations', consultationId)}>打开需求咨询</Link>
          ) : null}
        </section>
        <section className="home-card">
          <h2>最近的记录</h2>
          <ul className="plain-list">
            {items.slice().sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 8).map((i) => (
              <li key={`${i.kind}/${i.id}`}><RecordLink kind={i.kind} id={i.id} label={<>{KIND_LABEL[i.kind] ?? i.kind}：{i.label}</>} /> <span className="stamp">{timeText(i.updated_at)}</span></li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
