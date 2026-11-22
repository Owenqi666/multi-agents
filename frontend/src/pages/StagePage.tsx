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
import { PlanningAttempts } from './PlanningAttempts';
import { Evidence, Facts, NextNote, Page, Raw, Section, useResource } from './common';
import { tx, joinList } from '../i18n';

function Gate({ ok, label }: { ok: boolean; label: string }) {
  return <Badge tone={ok ? 'ok' : 'muted'}>{label}{ok ? '' : tx('：还没有')}</Badge>;
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
    <Section title={stage.mode === 'delegate' ? tx('拆分') : tx('拟卡和复核')} note={archive ? undefined : <NextNote next={p.next_action} />}>
      <Facts rows={[
        [tx('阶段负责人'), draft ? <Evidence reportedModel={draft.runtime?.reported_model} reportedEffort={draft.runtime?.reported_effort} request={draft.request.request} /> : tx('还没开始')],
        ...(stage.mode === 'direct' ? [[tx('卡片复核'), review ? <Evidence reportedModel={review.runtime?.reported_model} reportedEffort={review.runtime?.reported_effort} request={review.request.request} /> : tx('还没开始')] as [string, React.ReactNode],
          [tx('复核结论'), decision?.decision === 'ready' ? <Badge tone="ok">{tx('可以实施')}</Badge> : decision?.decision === 'needs_revision' ? <Badge tone="warn">{tx('要求修改：')}{decision.reason ?? ''}</Badge> : '—'] as [string, React.ReactNode]] : []),
        [tx('规划状态'), <code>{p.phase}</code>],
      ]} />
      {needsResume ? (
        <ActionButton action="project.stage.resume" label={tx('恢复阶段')} args={() => ({ stage_run_id: stage.stage_run_id, ...(p.attempts.draft.length ? {} : { prepare: true }) })} />
      ) : null}
    </Section>
  );
}

function IntegrateTarget({ stage, ws }: { stage: DirectStage; ws: Workspace | null }) {
  const archive = useArchiveMode();
  const [target, setTarget] = useState(ws?.target_branch ?? '');
  if (archive || !stage.review_satisfied || !stage.task_run_id || stage.implementation?.implementation_attempt == null) return null;
  return (
    <Section title={tx('合进目标分支')} note={tx('直接模式的阶段到审核通过为止；要把候选合进目标分支，另走创建集成、测试、审核和接受。')}>
      <div className="form-actions">
        <label>{tx('目标分支')}<select className="field" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">{tx('选择分支')}</option>
          {(ws?.branches ?? []).map((b) => <option key={b.name} value={b.name}>{b.name}</option>)}
        </select></label>
        <ActionButton action="integration.create" primary label={tx('创建集成')} disabled={!target} reason={tx('先选目标分支。')} confirm={tx('每次都会新建一个集成，确定创建？')}
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
        <Section title={tx('实施')}><p className="muted">{tx('直接模式里，拟卡和复核通过后由阶段负责人在同一个作业里实施。')}</p></Section>
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
      <summary>{tx('原规划')} <code>{shortId(id)}</code></summary>
      {open ? (
        <ReadFrame read={{ ...read, data: read.data?.data ?? null }} what={tx('原规划')}>
          {(plan) => <Raw value={plan} label={tx('完整规划记录')} />}
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
  const [overrideReview, setOverrideReview] = useState(false);
  const [review, setReview] = useState<PickerChoice | null>(null);
  const onExec = useCallback((v: PickerChoice | null) => setExec(v), []);
  const planning = nz(leaf.planning);
  const impl = nz(leaf.implementation);
  const executor = planning?.draft_projection?.executor ?? null;
  const last = impl?.attempts[impl.attempts.length - 1];
  const claimed = planning !== null;
  const waiting = leaf.unmet_dependencies.length > 0;
  const canRefreeze = claimed && !leaf.task_run_id && !leaf.review_ready && !waiting && upstreamChanged(leaf, stage.leaves);
  const baseline = planning?.card?.baseline_commit;
  const req = requestFrom(ws, 'implementation', exec);
  const stageInput = stage.planning.details?.input;
  const frozenReview = planning?.details?.input?.review_request;
  const reviewer = overrideReview ? requestFrom(ws, 'review', review) : null;
  const budget = planning?.budget?.review ?? (frozenReview ? planning?.budget?.[frozenReview.provider] : undefined);
  const canRetryReview = !leaf.task_run_id && !waiting && !leaf.review_ready && !impl?.attempts.length && planning?.next_action?.kind === 'plan_retry'
    && ['awaiting_review', 'failed', 'cancelled'].includes(planning.phase) && (budget?.remaining ?? 1) > 0;
  return (
    <li className="leaf">
      <div className="leaf-head">
        <code>{leaf.card_id}</code>
        {claimed ? <Badge tone={leaf.review_ready ? 'ok' : 'muted'}>{leaf.review_ready ? tx('复核通过') : tx('复核中或未通过')}</Badge> : <Badge tone="muted">{tx('未领取')}</Badge>}
        {leaf.candidate_verified ? <Badge tone="ok">{tx('候选已核验')}</Badge> : null}
        {waiting ? <span className="stamp">{tx('等待')} {joinList(leaf.unmet_dependencies)}</span> : null}
        {typeof baseline === 'string' ? <span className="stamp">{tx('基线')} <code>{shortId(baseline)}</code></span> : null}
        {leaf.task_run_id ? <Link href={to('tasks', leaf.task_run_id)}>{tx('实施详情')}</Link> : null}
      </div>
      {leaf.blocked_reason ? <p className="muted">{leaf.blocked_reason}</p> : null}
      {executor ? <p className="row"><span className="stamp">{tx('执行者')}</span>{last ? <Evidence reportedModel={last.runtime?.reported_model} reportedEffort={last.runtime?.reported_effort} request={executor} /> : <span>{tx('请求 {0} · {1}', executor.model, executor.effort)}</span>}</p> : null}
      {planning ? <PlanningAttempts label={tx('卡片复核')} attempts={planning.attempts.review} request={frozenReview} /> : null}
      {archive ? null : <div className={claimed ? 'form-actions' : 'leaf-claim-fields'}>
        {!claimed ? (
          <>
            <ModelPicker ws={ws} role="implementation" value={exec} onChange={onExec} label={tx('执行者')} only={['Terra', 'Sonnet']} />
            <div className="plan-models">
              <label className="check">
                <input type="checkbox" checked={overrideReview} onChange={(event) => setOverrideReview(event.target.checked)} /> {tx('为此子卡指定复核模型')}
              </label>
              {overrideReview ? <ModelPicker ws={ws} role="review" value={review} onChange={setReview} label={tx('子卡复核')}
                exclude={stageInput?.draft_request?.model ?? stage.planning.attempts.draft.at(-1)?.request.request.model} />
                : stageInput?.review_request ? <span className="stamp">{tx('继承阶段复核')} <code>{stageInput.review_request.model}</code> · {stageInput.review_request.effort}</span> : null}
            </div>
            <label className="check"><input type="checkbox" checked={stop} onChange={(e) => setStop(e.target.checked)} /> {tx('复核后先停下，不马上实施')}</label>
            <ActionButton action="project.stage.leaf" label={tx('领取子卡')} disabled={!req || waiting || (overrideReview && !reviewer)}
              reason={waiting ? tx('依赖的子卡还没完成。') : !req ? tx('先选执行者。') : tx('先选好复核模型和档位。')}
              args={() => ({
                stage_run_id: stage.stage_run_id, card_id: leaf.card_id, implementation: req,
                ...(reviewer ? { card_review: reviewer } : {}), ...(stop ? { review_only: true } : {}),
              })} />
          </>
        ) : canRefreeze ? (
          <ActionButton action="project.stage.leaf" primary label={tx('重新冻结并复核')}
            args={() => ({ stage_run_id: stage.stage_run_id, card_id: leaf.card_id, refreeze: true, review_only: true })} />
        ) : canRetryReview ? (
          <ActionButton action="project.stage.leaf" label={tx('重试卡片复核')}
            args={() => ({ stage_run_id: stage.stage_run_id, card_id: leaf.card_id, review_only: true })} />
        ) : leaf.review_ready && !waiting && planning && !impl?.attempts.length ? (
          <ActionButton action="task.run" primary label={tx('实施')} args={() => ({ planning_run_id: planning.planning_run_id })} />
        ) : leaf.review_ready && !waiting && impl && impl.next_action?.kind === 'run' && impl.attempts.length ? (
          <ActionButton action="task.retry" label={tx('重试实施')} args={() => ({ planning_run_id: impl.planning_run_id })} />
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
      <Section title={tx('子卡')} note={tx('首次领取可以为子卡选复核模型。默认使用阶段设置。互不依赖的子卡可以并行实施。')}>
        {stage.leaves.length ? <ul className="leaf-list">{stage.leaves.map((l) => <LeafRow key={l.card_id} stage={stage} leaf={l} ws={ws} />)}</ul> : <p className="muted">{tx('拆分核验通过后才会列出子卡。')}</p>}
        {ready.length >= 2 ? (
          <ActionButton action="task.run_batch" label={tx('并行实施 {0} 张子卡', ready.length)} args={() => ({ planning_run_ids: ready.map((l) => l.planning_run_id) })} />
        ) : null}
      </Section>
      <Section title={tx('合并')} note={tx('全部子卡的候选核验通过后，工作台把它们合成一个集成候选。每次合并都会新建一个集成。')}>
        {integration ? (
          <p className="row"><Link className="btn" href={to('integrations', integration.integration_id)}>{tx('打开集成')}</Link>
            <Gate ok={integration.tests_passed} label={tx('测试通过')} /><Gate ok={integration.review_satisfied} label={tx('审核通过')} />
            {integration.evidence?.acceptance ? <Badge tone="ok">{tx('已接受')}</Badge> : null}</p>
        ) : (
          <ActionButton action="project.stage.integrate" primary label={tx('合并子卡')} disabled={!stage.can_integrate} reason={tx('还有子卡的候选没有核验通过。')}
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
    <Page kicker={tx('阶段 · {0}', shortId(id))} title={stage ? tx('阶段 {0}', stage.stage_id) : tx('阶段')} aside={stage ? <Link className="btn small" href={to('plans', stage.plan_id)}>{tx('项目计划')}</Link> : null}>
      {story.beats.length ? (
        <div className="stage-weave">
          <Stage key={id} story={story} kind="weave" handle="stage" autoplay={false} keys={false}
            foot={read.stale && read.readAt ? tx('作业运行中，停在 {0} 读到的状态', readTime(read.readAt)) : undefined} />
        </div>
      ) : null}
      <ReadFrame read={{ ...read, data: stage }} what={tx('阶段')}>
        {(s) => (
          <>
            <Section title={tx('门槛')} note={tx('后面的阶段只看审核是否通过；接受不影响解锁。')}>
              <p className="row">
                <Badge tone="plain">{s.mode === 'direct' ? tx('直接模式') : tx('分派模式')}</Badge>
                {s.mode === 'delegate' ? <Gate ok={s.split_verified} label={tx('拆分已核验')} /> : null}
                <Gate ok={s.candidate_verified} label={tx('候选已核验')} />
                <Gate ok={s.tests_passed} label={tx('测试通过')} />
                <Gate ok={s.review_satisfied} label={tx('审核通过')} />
              </p>
              {s.dispatch_error ? <p className="action-why problem">{s.dispatch_error}</p> : null}
            </Section>
            {s.mode === 'direct' ? <DirectBody stage={s} ws={ws.data} /> : <DelegateBody stage={s} ws={ws.data} />}
          </>
        )}
      </ReadFrame>
      {!stage && !read.stale && read.error ? <EmptyState compact title={tx('读不到阶段')} /> : null}
    </Page>
  );
}
