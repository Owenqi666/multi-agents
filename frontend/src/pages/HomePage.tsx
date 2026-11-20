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
import { Stage } from '../stage/Stage';
import { NextNote, RecordLink, useResource } from './common';

function latest(items: RunItem[], kind: string): RunItem | null {
  return items.filter((i) => i.kind === kind).sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0] ?? null;
}

export function HomePage() {
  const { active } = useJobs();
  const runs = useRead<{ items: RunItem[] }>('runs', () => api.runs(), { locking: false, pollWhileJob: true });
  const items = runs.data?.items ?? [];
  const stageRun = latest(items, 'stage');
  const planRun = latest(items, 'project-plan');
  const consultRun = latest(items, 'consultation');

  const stage = useResource<StageData>('stage', stageRun?.id);
  const planId = stage.data?.data?.plan_id ?? planRun?.id ?? null;
  const plan = useResource<ProjectPlan>('project-plan', planId);
  const goal: Goal | null = plan.data?.data?.goal ?? null;
  const consultationId = goal?.consultation?.consultation_id ?? (goal ? null : consultRun?.id ?? null);
  const consultation = useResource<Consultation>('consultation', consultationId);

  const story = useMemo(() => withRunningJob(deriveStory({
    consultation: consultation.data?.data ?? null,
    goal,
    plan: plan.data?.data ?? null,
    stage: stage.data?.data ?? null,
  }), active), [consultation.data, goal, plan.data, stage.data, active]);

  if (runs.data && !items.length && !active) {
    return (
      <div className="home">
        <EmptyState title="还没有项目" body="写下想做成的事。顾问会先读它，帮你写成可以确认的目标；之后规划、实施和审核都会画在这里。" action={<Link className="btn primary" href="/new">提出目标</Link>} />
      </div>
    );
  }

  const stageData = stage.data?.data ?? null;
  const next = stageData && (stageData.mode === 'direct' ? stageData.implementation?.next_action ?? stageData.planning.next_action : stageData.planning.next_action);
  return (
    <div className="home">
      {story.beats.length ? (
        <Stage key={story.beats.length + (stageRun?.id ?? '')} story={story} kind="weave" handle="stage" autoplay={false}
          foot={stage.stale && stage.readAt ? `作业运行中，阶段停在 ${readTime(stage.readAt)} 读到的状态` : '泳道和颜色跟着席位；模型名只用运行时报告值'} />
      ) : (
        <p className="loading">正在读取运行状态…</p>
      )}
      <div className="home-below">
        <section className="home-card">
          <h2>接下来</h2>
          {active ? (
            <p>作业「{actionLabel(active.action)}」正在进行。<Link href={to('jobs', active.job_id)}>查看作业</Link></p>
          ) : stageRun ? (
            <>
              <p><NextNote next={next ?? null} fallback="打开阶段查看当前状态。" /></p>
              <Link className="btn" href={to('stages', stageRun.id)}>打开阶段</Link>
            </>
          ) : planRun ? (
            <Link className="btn" href={to('plans', planRun.id)}>打开项目计划</Link>
          ) : consultRun ? (
            <Link className="btn" href={to('consultations', consultRun.id)}>打开需求咨询</Link>
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
