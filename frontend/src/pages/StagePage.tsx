// 阶段: the stage's own weave, its gates, and the work in it. A direct stage shows Sol's
// card, its review and the implementation; a delegate stage shows the split, the leaves,
// the merge and the integration. The stage record takes the repository lock, so during a
// job it keeps the last reading and the weave marks where the running job is.

import { useCallback, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import type { DelegateStage, DirectStage, Leaf, PlanningView, Stage as StageData, Workspace } from '../api/types';
import { useArchiveMode, useWorkspace } from '../api/workspace';
import { nz } from '../api/types';
import { shortId } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { EmptyState } from '../components/EmptyState';
import { ModelPicker, requestFrom, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { readTime } from '../components/LogBlock';
import { deriveStory, withRunningJob } from '../stage/derive';
import { useIntegrationHistory } from '../stage/integrationLineage';
import { belongsToStage } from '../stage/jobScope';
import { Stage } from '../stage/Stage';
import { TaskPanel } from './TaskPage';
import { Evidence, Facts, NextNote, Page, Raw, Section, useResource } from './common';

function Gate({ ok, label }: { ok: boolean; label: string }) {
  return <Badge tone={ok ? 'ok' : 'muted'}>{label}{ok ? '' : '：还没有'}</Badge>;
}

function PlanningBlock({ stage }: { stage: StageData }) {
  const archive = useArchiveMode();
  const p = stage.planning;
  const draft = p.attempts.draft[p.attempts.draft.length - 1];
  const review = p.attempts.review[p.attempts.review.length - 1];
  const decision = (review?.runtime?.result as { decision?: string; reason?: string } | undefined);
  const kind = p.next_action?.kind;
  const needsResume = !p.attempts.draft.length || kind === 'plan_retry';
  return (
    <Section title={stage.mode === 'delegate' ? '拆分' : '拟卡和复核'} note={archive ? undefined : <NextNote next={p.next_action} />}>
      <Facts rows={[
        ['阶段负责人', draft ? <Evidence reportedModel={draft.runtime?.reported_model} reportedEffort={draft.runtime?.reported_effort} request={draft.request.request} /> : '还没开始'],
        ...(stage.mode === 'direct' ? [['卡片复核', review ? <Evidence reportedModel={review.runtime?.reported_model} reportedEffort={review.runtime?.reported_effort} request={review.request.request} /> : '还没开始'] as [string, React.ReactNode],
          ['复核结论', decision?.decision === 'ready' ? <Badge tone="ok">可以实施</Badge> : decision?.decision === 'needs_revision' ? <Badge tone="warn">要求修改：{decision.reason ?? ''}</Badge> : '—'] as [string, React.ReactNode]] : []),
        ['规划状态', <code>{p.phase}</code>],
      ]} />
      {needsResume ? (
        <ActionButton action="project.stage.resume" label="恢复阶段" args={() => ({ stage_run_id: stage.stage_run_id, ...(p.attempts.draft.length ? {} : { prepare: true }) })} />
      ) : null}
    </Section>
  );
}

function IntegrateTarget({ stage, ws }: { stage: DirectStage; ws: Workspace | null }) {
  const archive = useArchiveMode();
  const [target, setTarget] = useState(ws?.target_branch ?? '');
  if (archive || !stage.review_satisfied || !stage.task_run_id || stage.implementation?.implementation_attempt == null) return null;
  return (
    <Section title="合进目标分支" note="直接模式的阶段到审核通过为止；要把候选合进目标分支，另走创建集成、测试、审核和接受。">
      <div className="form-actions">
        <label>目标分支<select className="field" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">选择分支</option>
          {(ws?.branches ?? []).map((b) => <option key={b.name} value={b.name}>{b.name}</option>)}
        </select></label>
        <ActionButton action="integration.create" primary label="创建集成" disabled={!target} reason="先选目标分支。" confirm="每次都会新建一个集成，确定创建？"
          args={() => ({ task_run_ids: [stage.task_run_id], target })}
          onDone={(job) => { const id = job.resources?.find((r) => r.kind === 'integration')?.id; if (id) navigate(to('integrations', id)); }} />
      </div>
    </Section>
  );
}

function DirectBody({ stage, ws }: { stage: DirectStage; ws: Workspace | null }) {
  return (
    <>
      <PlanningBlock stage={stage} />
      {stage.implementation ? <TaskPanel task={stage.implementation} /> : (
        <Section title="实施"><p className="muted">直接模式里，拟卡和复核通过后由阶段负责人在同一个作业里实施。</p></Section>
      )}
      <IntegrateTarget stage={stage} ws={ws} />
    </>
  );
}

function PriorLeafPlan({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const read = useResource<PlanningView>('plan', id, { history: 1 }, { enabled: open });
  return (
    <details className="raw" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>原规划 <code>{shortId(id)}</code></summary>
      {open ? (
        <ReadFrame read={{ ...read, data: read.data?.data ?? null }} what="原规划">
          {(plan) => <Raw value={plan} label="完整规划记录" />}
        </ReadFrame>
      ) : null}
    </details>
  );
}

function upstreamChanged(leaf: Leaf, leaves: Leaf[]): boolean {
  return (leaf.planning?.draft_projection?.dependencies ?? []).some((frozen) => {
    const current = leaves.find((l) => l.card_id === frozen.card_id);
    return current?.candidate_verified && (
      current.planning_run_id !== frozen.planning_run_id || current.task_run_id !== frozen.task_run_id
      || current.implementation_attempt !== frozen.implementation_attempt || current.candidate !== frozen.candidate
    );
  });
}

function LeafRow({ stage, leaf, ws }: { stage: DelegateStage; leaf: Leaf; ws: Workspace | null }) {
  const archive = useArchiveMode();
  const [exec, setExec] = useState<PickerChoice | null>(null);
  const [stop, setStop] = useState(true);
  const onExec = useCallback((v: PickerChoice | null) => setExec(v), []);
  const planning = nz(leaf.planning);
  const impl = nz(leaf.implementation);
  const executor = planning?.draft_projection?.executor ?? null;
  const last = impl?.attempts[impl.attempts.length - 1];
  const claimed = planning !== null;
  const waiting = leaf.unmet_dependencies.length > 0;
  const canRefreeze = claimed && !leaf.review_ready && !waiting && upstreamChanged(leaf, stage.leaves);
  const baseline = planning?.card?.baseline_commit;
  const req = requestFrom(ws, 'implementation', exec);
  return (
    <li className="leaf">
      <div className="leaf-head">
        <code>{leaf.card_id}</code>
        {claimed ? <Badge tone={leaf.review_ready ? 'ok' : 'muted'}>{leaf.review_ready ? '复核通过' : '复核中或未通过'}</Badge> : <Badge tone="muted">未领取</Badge>}
        {leaf.candidate_verified ? <Badge tone="ok">候选已核验</Badge> : null}
        {waiting ? <span className="stamp">等待 {leaf.unmet_dependencies.join('、')}</span> : null}
        {typeof baseline === 'string' ? <span className="stamp">基线 <code>{shortId(baseline)}</code></span> : null}
        {leaf.task_run_id ? <Link href={to('tasks', leaf.task_run_id)}>实施详情</Link> : null}
      </div>
      {leaf.blocked_reason ? <p className="muted">{leaf.blocked_reason}</p> : null}
      {executor ? <p className="row"><span className="stamp">执行者</span>{last ? <Evidence reportedModel={last.runtime?.reported_model} reportedEffort={last.runtime?.reported_effort} request={executor} /> : <span>请求 {executor.model} · {executor.effort}</span>}</p> : null}
      {archive ? null : <div className="form-actions">
        {!claimed ? (
          <>
            <ModelPicker ws={ws} role="implementation" value={exec} onChange={onExec} label="执行者" only={['Terra', 'Sonnet']} />
            <label className="check"><input type="checkbox" checked={stop} onChange={(e) => setStop(e.target.checked)} /> 复核后先停下，不马上实施</label>
            <ActionButton action="project.stage.leaf" label="领取子卡" disabled={!req || waiting} reason={waiting ? '依赖的子卡还没完成。' : '先选执行者。'}
              args={() => ({ stage_run_id: stage.stage_run_id, card_id: leaf.card_id, implementation: req, ...(stop ? { review_only: true } : {}) })} />
          </>
        ) : canRefreeze ? (
          <ActionButton action="project.stage.leaf" primary label="重新冻结并复核"
            args={() => ({ stage_run_id: stage.stage_run_id, card_id: leaf.card_id, refreeze: true, review_only: true })} />
        ) : leaf.review_ready && !waiting && planning && !impl?.attempts.length ? (
          <ActionButton action="task.run" primary label="实施" args={() => ({ planning_run_id: planning.planning_run_id })} />
        ) : leaf.review_ready && !waiting && impl && impl.next_action?.kind === 'run' && impl.attempts.length ? (
          <ActionButton action="task.retry" label="重试实施" args={() => ({ planning_run_id: impl.planning_run_id })} />
        ) : null}
      </div>}
      {(leaf.prior_planning_run_ids ?? []).map((id) => <PriorLeafPlan key={id} id={id} />)}
    </li>
  );
}

function DelegateBody({ stage, ws }: { stage: DelegateStage; ws: Workspace | null }) {
  const ready = stage.leaves.filter((l) => nz(l.planning) && l.review_ready && !nz(l.implementation)?.attempts.length && !l.unmet_dependencies.length);
  const integration = stage.integration;
  return (
    <>
      <PlanningBlock stage={stage} />
      <Section title="子卡" note="互不依赖的子卡可以并行实施；有依赖的等前面的完成。每张卡的复核者沿用阶段的设置。">
        {stage.leaves.length ? <ul className="leaf-list">{stage.leaves.map((l) => <LeafRow key={l.card_id} stage={stage} leaf={l} ws={ws} />)}</ul> : <p className="muted">拆分核验通过后才会列出子卡。</p>}
        {ready.length >= 2 ? (
          <ActionButton action="task.run_batch" label={`并行实施 ${ready.length} 张子卡`} args={() => ({ planning_run_ids: ready.map((l) => l.planning_run_id) })} />
        ) : null}
      </Section>
      <Section title="合并" note="全部子卡的候选核验通过后，工作台把它们合成一个集成候选。每次合并都会新建一个集成。">
        {integration ? (
          <p className="row"><Link className="btn" href={to('integrations', integration.integration_id)}>打开集成</Link>
            <Gate ok={integration.tests_passed} label="测试通过" /><Gate ok={integration.review_satisfied} label="审核通过" />
            {integration.evidence?.acceptance ? <Badge tone="ok">已接受</Badge> : null}</p>
        ) : (
          <ActionButton action="project.stage.integrate" primary label="合并子卡" disabled={!stage.can_integrate} reason="还有子卡的候选没有核验通过。"
            args={() => ({ stage_run_id: stage.stage_run_id })}
            onDone={(job) => { const id = job.resources?.find((r) => r.kind === 'integration')?.id; if (id) navigate(to('integrations', id)); }} />
        )}
      </Section>
    </>
  );
}

export function StagePage({ id }: { id: string }) {
  const read = useResource<StageData>('stage', id);
  const ws = useWorkspace();
  const { active } = useJobs();
  const stage = read.data?.id === id ? read.data.data : null;
  const integrationHistory = useIntegrationHistory(stage);
  const runs = useRead('runs', () => api.runs(), { locking: false, pollWhileJob: true, enabled: active !== null });
  const ownJob = active && belongsToStage(active, id, stage, integrationHistory, runs.data?.items ?? []) ? active : null;
  const story = useMemo(() => withRunningJob(deriveStory({ stage, integrationHistory }), ownJob), [stage, integrationHistory, ownJob]);
  return (
    <Page kicker={`阶段 · ${shortId(id)}`} title={stage ? `阶段 ${stage.stage_id}` : '阶段'} aside={stage ? <Link className="btn small" href={to('plans', stage.plan_id)}>项目计划</Link> : null}>
      {story.beats.length ? (
        <div className="stage-weave">
          <Stage key={id} story={story} kind="weave" handle="stage" autoplay={false} keys={false}
            foot={read.stale && read.readAt ? `作业运行中，停在 ${readTime(read.readAt)} 读到的状态` : undefined} />
        </div>
      ) : null}
      <ReadFrame read={{ ...read, data: stage }} what="阶段">
        {(s) => (
          <>
            <Section title="门槛" note="后面的阶段只看审核是否通过；接受不影响解锁。">
              <p className="row">
                <Badge tone="plain">{s.mode === 'direct' ? '直接模式' : '分派模式'}</Badge>
                {s.mode === 'delegate' ? <Gate ok={s.split_verified} label="拆分已核验" /> : null}
                <Gate ok={s.candidate_verified} label="候选已核验" />
                <Gate ok={s.tests_passed} label="测试通过" />
                <Gate ok={s.review_satisfied} label="审核通过" />
              </p>
              {s.dispatch_error ? <p className="action-why problem">{s.dispatch_error}</p> : null}
            </Section>
            {s.mode === 'direct' ? <DirectBody stage={s} ws={ws.data} /> : <DelegateBody stage={s} ws={ws.data} />}
          </>
        )}
      </ReadFrame>
      {!stage && !read.stale && read.error ? <EmptyState compact title="读不到阶段" /> : null}
    </Page>
  );
}
