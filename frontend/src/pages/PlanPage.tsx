// 项目计划: read the plan, revise it (each save makes a new version), walk older versions,
// and claim stages in direct or delegate mode.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { jobs } from '../api/jobs';
import { lockBusy, useRead } from '../api/reads';
import type { PlanStage, Project, ProjectPlan, Stage, StageRun, Workspace } from '../api/types';
import { shortId, timeText } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { EmptyState } from '../components/EmptyState';
import { ModelPicker, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { EnvironmentHint, useWorkspace } from './GoalPages';
import { ArtifactLinks, Evidence, Facts, Page, Raw, Section, useResource } from './common';

type StageRuns = { plan_id: string; stages: StageRun[] };
type PlanVersion = { plan: ProjectPlan; stages: StageRun[] };

async function prerequisiteRun(
  version: PlanVersion, stageId: string, loadVersion: (id: string) => Promise<PlanVersion | null>,
): Promise<StageRun | undefined> {
  const { plan, stages } = version;
  const current = stages.find((stage) => stage.stage_id === stageId);
  if (current || !plan.source_plan_id || !plan.document) return current;

  const document = plan.document;
  const needed = new Set([stageId]);
  for (const stage of [...document.stages].reverse()) {
    if (needed.has(stage.stage_id)) for (const id of stage.depends_on) needed.add(id);
  }
  const definitions = document.stages.filter((stage) => needed.has(stage.stage_id));

  // An unchanged prerequisite can come from an earlier plan revision. As in the CLI,
  // a newly claimed ancestor or a changed handoff breaks that inheritance.
  let newer = version;
  while (newer.plan.source_plan_id) {
    if (newer.stages.some((stage) => needed.has(stage.stage_id) && stage.stage_id !== stageId)) break;
    const source = await loadVersion(newer.plan.source_plan_id);
    const previous = source?.plan.document;
    if (!source || !previous || source.plan.goal.goal_id !== plan.goal.goal_id
      || previous.architecture !== document.architecture
      || JSON.stringify(previous.shared_interfaces) !== JSON.stringify(document.shared_interfaces)) break;
    // Plan documents read from the bridge have canonical key order.
    const earlier = new Map(previous.stages.map((stage) => [stage.stage_id, JSON.stringify(stage)]));
    if (definitions.some((stage) => earlier.get(stage.stage_id) !== JSON.stringify(stage))) break;
    const inherited = source.stages.find((stage) => stage.stage_id === stageId);
    if (inherited) return inherited;
    newer = source;
  }
  return undefined;
}

function usePrerequisiteDelivery(plan: ProjectPlan | null, runs: StageRuns | null) {
  const needed = [...new Set((plan?.document?.stages ?? [])
    .filter((stage) => !runs?.stages.some((run) => run.stage_id === stage.stage_id))
    .flatMap((stage) => stage.depends_on))];
  const key = plan && runs && needed.length
    ? `stage-delivery/${plan.plan_id}/${runs.stages.map((stage) => stage.stage_run_id).join(',')}` : null;

  const read = useRead(key, async () => {
    const version = { plan: plan!, stages: runs!.stages };
    const cache = new Map<string, PlanVersion | null>();
    const loadVersion = async (id: string): Promise<PlanVersion | null> => {
      if (!cache.has(id)) {
        const [planRead, stagesRead] = await Promise.all([
          api.resource<ProjectPlan>('project-plan', id), api.resource<StageRuns>('project-stages', id),
        ]);
        cache.set(id, planRead.data && stagesRead.data ? { plan: planRead.data, stages: stagesRead.data.stages } : null);
      }
      return cache.get(id)!;
    };

    const delivered = new Set<string>();
    for (const id of needed) {
      const run = await prerequisiteRun(version, id, loadVersion);
      if (!run) continue;
      if (!jobs.idle) return { lock_busy: true };
      const detail = await api.resource<Stage>('stage', run.stage_run_id);
      if (lockBusy(detail)) return { lock_busy: true };
      if (detail.data?.review_satisfied) delivered.add(id);
    }
    return { key, delivered };
  }, { locking: true });
  return { ...read, data: read.data?.key === key ? read.data?.delivered : null };
}

function ClaimForm({ plan, stage, ws, blocker }: { plan: ProjectPlan; stage: PlanStage; ws: Workspace; blocker: string | null }) {
  const d = ws.defaults;
  const [mode, setMode] = useState<'direct' | 'delegate'>(d?.stage_mode ?? 'direct');
  const [baseline, setBaseline] = useState('');
  const [sol, setSol] = useState<PickerChoice | null>(d ? { model: d.sol.model, effort: d.sol.effort } : null);
  const [review, setReview] = useState<PickerChoice | null>(d ? { model: d.card_review.model, effort: d.card_review.effort } : null);
  const onSol = useCallback((v: PickerChoice | null) => setSol(v), []);
  const onReview = useCallback((v: PickerChoice | null) => setReview(v), []);
  const reviewer = ws.presets.roles.find((r) => r.id === 'review')?.candidates.find((c) => c.model === review?.model);
  const same = sol && review && sol.model === review.model;
  return (
    <div className="claim">
      <div className="seg small" role="radiogroup" aria-label="阶段模式">
        <button type="button" role="radio" aria-checked={mode === 'direct'} className={mode === 'direct' ? 'on' : ''} onClick={() => setMode('direct')}>直接：Sol 拟卡并实施</button>
        <button type="button" role="radio" aria-checked={mode === 'delegate'} className={mode === 'delegate' ? 'on' : ''} onClick={() => setMode('delegate')}>分派：拆成子卡交给 Terra、Sonnet</button>
      </div>
      <ModelPicker ws={ws} role="stage_lead" value={sol} onChange={onSol} label="阶段负责人" />
      <ModelPicker ws={ws} role="review" value={review} onChange={onReview} label="卡片复核" exclude={sol?.model ?? null} />
      {stage.depends_on.length ? (
        <>
          <div className="form-row">
            <label>执行基线（可选）
              <input className="field mono" value={baseline} onChange={(e) => setBaseline(e.target.value)} placeholder="提交 SHA 或 Git 引用" />
            </label>
          </div>
          <span className="action-why">当前置候选分属不同提交时，可以填写包含所有前置交付的合并提交。</span>
        </>
      ) : null}
      <ActionButton action="project.stage.start" primary label="领取这个阶段" disabled={!!blocker || !sol || !reviewer || !!same}
        reason={blocker ?? (same ? '卡片复核不能和阶段负责人是同一个模型。' : '先选好阶段负责人和卡片复核。')}
        args={() => sol && review && reviewer ? {
          plan_id: plan.plan_id, stage_id: stage.stage_id, mode, sol: { model: sol.model, effort: sol.effort },
          card_review: { provider: reviewer.provider, model: reviewer.model, effort: review.effort },
          ...(baseline.trim() ? { baseline: baseline.trim() } : {}),
        } : null}
        onDone={(job) => { const id = job.resources?.find((r) => r.kind === 'stage')?.id; if (id) navigate(to('stages', id)); }} />
      {mode === 'direct' ? <p className="stamp">直接模式的一次作业包含拟卡、复核和实施，可能要好几分钟。</p> : null}
    </div>
  );
}

function Revise({ plan }: { plan: ProjectPlan }) {
  const initial = useMemo(() => JSON.stringify(plan.document, null, 2), [plan.document]);
  const [text, setText] = useState(initial);
  const [open, setOpen] = useState(false);
  useEffect(() => setText(initial), [initial]);
  let parsed: unknown = null;
  let problem: string | null = null;
  try { parsed = JSON.parse(text); } catch (e) { problem = `不是合法的 JSON：${String(e)}`; }
  const changed = text !== initial;
  if (!open) return <button type="button" className="btn" onClick={() => setOpen(true)}>修订计划</button>;
  return (
    <div className="revise">
      <p className="section-note">直接改计划文档的 JSON。保存会生成一个新版本，持有者记为你；服务会先校验文档，不合法时不保存。</p>
      <textarea className="field mono big" rows={18} value={text} onChange={(e) => setText(e.target.value)} aria-label="计划文档" spellCheck={false} />
      {problem ? <p className="action-why problem">{problem}</p> : null}
      <div className="form-actions">
        <ActionButton action="project.revise" primary label="保存为新版本" disabled={!changed || !!problem} reason={problem ? '先改成合法的 JSON。' : '还没有改动。'}
          args={() => ({ plan_id: plan.plan_id, document: parsed })}
          onDone={(job) => { const id = job.resources?.find((r) => r.kind === 'project-plan')?.id; if (id) navigate(to('plans', id)); }} />
        <button type="button" className="btn quiet" onClick={() => { setText(initial); setOpen(false); }}>放弃</button>
      </div>
    </div>
  );
}

export function PlanPage({ id }: { id: string }) {
  const read = useResource<ProjectPlan>('project-plan', id);
  const plan = read.data?.data?.plan_id === id ? read.data.data : null;
  const stages = useResource<StageRuns>('project-stages', plan?.status === 'succeeded' ? id : null);
  const runs = stages.data?.data?.plan_id === id ? stages.data.data : null;
  const deliveries = usePrerequisiteDelivery(plan, runs);
  const project = useResource<Project>('project', plan?.goal.project_id);
  const ws = useWorkspace();
  const versions = (project.data?.data?.plans ?? []).filter((p) => p.goal_id === plan?.goal.goal_id);
  const newer = versions.filter((p) => p.source_plan_id === id);
  return (
    <Page kicker={`项目计划 · ${shortId(id)}`} title={plan?.document?.title ?? '项目计划'}>
      <ReadFrame read={{ ...read, data: plan }} what="项目计划">
        {(p) => (
          <>
            <Section title="版本">
              <Facts rows={[
                ['状态', p.status === 'running' ? <Badge tone="live">生成中</Badge> : p.status === 'succeeded' ? <Badge tone="ok">已生成</Badge> : <Badge tone="fail">失败</Badge>],
                ['持有者', p.origin === 'pm' ? '你（手改的版本）' : <Evidence reportedModel={p.evidence?.reported_model} reportedEffort={p.evidence?.reported_effort} request={p.planner_request} />],
                ['生成时间', timeText(p.finished_at ?? p.started_at)],
                ['上一版', p.source_plan_id ? <Link href={to('plans', p.source_plan_id)}>计划 <code>{shortId(p.source_plan_id)}</code></Link> : '没有，这是第一版'],
                ['之后的版本', newer.length ? newer.map((n) => <Link key={n.plan_id} href={to('plans', n.plan_id)}>计划 <code>{shortId(n.plan_id)}</code> </Link>) : '没有'],
                ['项目', <Link href={to('projects', p.goal.project_id)}>打开项目</Link>],
              ]} />
            </Section>
            {p.status === 'failed' ? <EmptyState compact title="这一版计划没有生成" body="可以回到项目再生成一次；失败的记录保留在这里。" action={<Link className="btn" href={to('projects', p.goal.project_id)}>回到项目</Link>} /> : null}
            {read.data?.artifacts.length ? (
              <Section title="规划日志">
                <ArtifactLinks artifacts={read.data.artifacts} label={(artifact) => `规划日志 ${artifact.coordinates?.log_index ?? artifact.key ?? ''}`.trim()} />
              </Section>
            ) : null}
            {p.document ? (
              <>
                {p.document.architecture ? <Section title="整体架构"><pre className="goal-text">{p.document.architecture}</pre></Section> : null}
                <Section title="阶段" note="阶段按计划顺序列出。前置阶段的候选通过测试和审核后，后面的阶段才能领取。">
                  <ol className="stage-list">
                    {p.document.stages.map((s) => {
                      const claimed = runs?.stages.find((stage) => stage.stage_id === s.stage_id);
                      const delivered = deliveries.data;
                      const missing = s.depends_on.filter((dep) => !delivered?.has(dep));
                      let blocker: string | null = null;
                      if (!runs) blocker = stages.error ? '读不到阶段领取记录。' : '正在读取阶段领取记录…';
                      else if (s.depends_on.length) {
                        if (deliveries.error) blocker = '读不到前置阶段的验证结果。';
                        else if (!delivered || deliveries.loading || deliveries.stale) blocker = '正在核对前置阶段的交付…';
                        else if (missing.length) blocker = `等待前置阶段通过测试和审核：${missing.join('、')}。`;
                      }
                      return (
                        <li key={s.stage_id}>
                          <div className="stage-row-head"><strong>{s.title}</strong> <code className="stamp">{s.stage_id}</code>
                            {s.depends_on.length ? <span className="stamp">依赖 {s.depends_on.join('、')}</span> : null}</div>
                          {s.goal ? <p className="muted">{s.goal}</p> : null}
                          {claimed ? (
                            <p className="row"><Badge tone="ok">已领取 · {claimed.mode === 'direct' ? '直接' : '分派'}</Badge>
                              <Link className="btn small" href={to('stages', claimed.stage_run_id)}>打开阶段</Link>
                              {claimed.dispatch_error ? <span className="action-why problem">{claimed.dispatch_error}</span> : null}</p>
                          ) : p.status === 'succeeded' && ws.data ? (
                            <ClaimForm key={p.plan_id} plan={p} stage={s} ws={ws.data} blocker={blocker} />
                          ) : null}
                        </li>
                      );
                    })}
                  </ol>
                  <EnvironmentHint ws={ws.data} />
                </Section>
                <Section title="修订"><Revise plan={p} /></Section>
                <Section title="完整文档"><Raw value={p.document} label="计划文档（JSON）" />{p.markdown ? <details className="raw"><summary>计划书正文</summary><pre>{p.markdown}</pre></details> : null}</Section>
              </>
            ) : null}
          </>
        )}
      </ReadFrame>
    </Page>
  );
}
