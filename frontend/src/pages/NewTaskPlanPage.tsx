import { useState } from 'react';
import { useJobs } from '../api/jobs';
import type { PlanningDecision, PlanningDependency, PlanningInputView, PlanningQuestion, PlanningView, RoleRequest, Workspace } from '../api/types';
import { useWorkspace, useWorkspaceWritesAllowed } from '../api/workspace';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { EmptyState } from '../components/EmptyState';
import { candidatesFor, effortsFor, ModelPicker, requestFrom, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { EnvironmentHint } from './GoalPages';
import { Page, Section, useResource } from './common';

type Dependency = Omit<PlanningDependency, 'source'>;
type Decision = Omit<PlanningDecision, 'source'>;
const SHA = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/i;
const DRAFT_FAMILIES = ['Fable', 'Opus', 'Sonnet'];
const REVIEW_FAMILIES = ['Astra', 'Sol', 'Terra'];

function selectableRequest(ws: Workspace, source: RoleRequest | undefined, families: string[]): RoleRequest | null {
  if (!source) return null;
  const candidate = candidatesFor(ws, 'review', families).find((item) => item.provider === source.provider && item.model === source.model);
  return effortsFor(candidate).includes(source.effort) ? source : null;
}

function TaskPlanForm({ ws, source, question }: { ws: Workspace; source?: PlanningInputView; question?: PlanningQuestion }) {
  const { active, settling, submitting } = useJobs();
  const [baseline, setBaseline] = useState(source?.frozen_rules?.baseline_commit ?? ws.head ?? '');
  const [requirements, setRequirements] = useState(source?.requirements ?? '');
  const [draftOverride, setDraftOverride] = useState<boolean | null>(null);
  const [reviewOverride, setReviewOverride] = useState<boolean | null>(null);
  const [draft, setDraft] = useState<PickerChoice | null>(null);
  const [review, setReview] = useState<PickerChoice | null>(null);
  const [dependencies, setDependencies] = useState<Dependency[]>(source?.dependencies ?? []);
  const [decisions, setDecisions] = useState<Decision[]>(() => {
    const saved = source?.decisions ?? [];
    return question && !saved.some((value) => value.question === question.question)
      ? [...saved, { question: question.question, resolution: '', detail: '' }]
      : saved;
  });

  const previousDraft = selectableRequest(ws, source?.draft_request, DRAFT_FAMILIES);
  const previousReview = selectableRequest(ws, source?.review_request, REVIEW_FAMILIES);
  const draftExplicit = draftOverride ?? previousDraft !== null;
  const reviewExplicit = reviewOverride ?? previousReview !== null;
  const draftChoice = draft ?? previousDraft;
  const reviewChoice = review ?? previousReview;
  const draftRequest = draftExplicit ? requestFrom(ws, 'review', draftChoice) : null;
  const reviewRequest = reviewExplicit ? requestFrom(ws, 'review', reviewChoice) : null;
  const invalidDeclaration = dependencies.some((value) => !value.task_id.trim() || !SHA.test(value.baseline_commit.trim()) || !value.detail.trim())
    || decisions.some((value) => !value.question.trim() || !value.resolution.trim() || !value.detail.trim());
  const reason = !baseline.trim() || !requirements.trim() ? '先填写基线和需求。'
    : (draftExplicit && !draftRequest) || (reviewExplicit && !reviewRequest) ? '先选好指定的模型和档位。'
    : invalidDeclaration ? '请填完整声明。每条依赖需要完整的基线 SHA。' : null;

  function updateDependency(index: number, update: Partial<Dependency>) {
    setDependencies((values) => values.map((value, row) => row === index ? { ...value, ...update } : value));
  }
  function updateDecision(index: number, update: Partial<Decision>) {
    setDecisions((values) => values.map((value, row) => row === index ? { ...value, ...update } : value));
  }

  return (
    <>
      <EnvironmentHint ws={ws} />
      <fieldset className="plan-fields" aria-label="单卡规划输入" disabled={active !== null || settling || submitting !== null}>
        <Section title="需求">
          <div className="form-row">
            <label>基线提交<input className="field mono" value={baseline} onChange={(event) => setBaseline(event.target.value)} /></label>
          </div>
          <textarea className="field big" rows={7} value={requirements} onChange={(event) => setRequirements(event.target.value)} aria-label="单卡需求" />
        </Section>
        <Section title="规划席位" note="Claude 草拟任务卡，Codex 复核。可以指定模型和档位。">
          <div className="plan-models">
            <label className="check"><input type="checkbox" checked={draftExplicit} onChange={(event) => setDraftOverride(event.target.checked)} /> 指定草拟模型</label>
            {source?.draft_request && !previousDraft ? <p className="muted">原草拟请求 <code>{source.draft_request.model}</code> · {source.draft_request.effort} 当前不可选。可指定新的草拟模型或使用默认设置。</p> : null}
            {draftExplicit ? <ModelPicker ws={ws} role="review" only={DRAFT_FAMILIES} value={draftChoice} onChange={setDraft} label="草拟模型" /> : null}
            <label className="check"><input type="checkbox" checked={reviewExplicit} onChange={(event) => setReviewOverride(event.target.checked)} /> 指定复核模型</label>
            {source?.review_request && !previousReview ? <p className="muted">原复核请求 <code>{source.review_request.model}</code> · {source.review_request.effort} 当前不可选。可指定新的复核模型或使用默认设置。</p> : null}
            {reviewExplicit ? <ModelPicker ws={ws} role="review" only={REVIEW_FAMILIES} value={reviewChoice} onChange={setReview} label="规划复核" /> : null}
          </div>
        </Section>
        <Section title="声明">
          <details className="raw" open={question ? true : undefined}>
            <summary>冻结声明（可选）</summary>
            <div className="plan-declarations">
              <p className="section-note">依赖与用户决定会随本次规划冻结。依赖基线 SHA 须与所选提交一致。</p>
              {dependencies.map((value, index) => (
                <fieldset className="plan-declaration" key={index}>
                  <legend>依赖声明 {index + 1}</legend>
                  <div className="form-row">
                    <label>依赖任务 ID {index + 1}<input className="field mono" value={value.task_id} onChange={(event) => updateDependency(index, { task_id: event.target.value })} /></label>
                    <label>依赖基线 SHA {index + 1}<input className="field mono" value={value.baseline_commit} onChange={(event) => updateDependency(index, { baseline_commit: event.target.value })} /></label>
                  </div>
                  <label className="plan-label">依赖说明 {index + 1}<textarea className="field big" rows={2} value={value.detail} onChange={(event) => updateDependency(index, { detail: event.target.value })} /></label>
                  <div><button type="button" className="btn small quiet" onClick={() => setDependencies((values) => values.filter((_, row) => row !== index))}>删除依赖声明 {index + 1}</button></div>
                </fieldset>
              ))}
              {decisions.map((value, index) => (
                <fieldset className="plan-declaration" key={index}>
                  <legend>用户决定 {index + 1}</legend>
                  <label className="plan-label">问题 {index + 1}<input className="field" value={value.question} onChange={(event) => updateDecision(index, { question: event.target.value })} /></label>
                  <label className="plan-label">回答 {index + 1}<textarea className="field big" rows={2} value={value.resolution} onChange={(event) => updateDecision(index, { resolution: event.target.value })} /></label>
                  <label className="plan-label">决定说明 {index + 1}<input className="field" value={value.detail} onChange={(event) => updateDecision(index, { detail: event.target.value })} /></label>
                  <div><button type="button" className="btn small quiet" onClick={() => setDecisions((values) => values.filter((_, row) => row !== index))}>删除用户决定 {index + 1}</button></div>
                </fieldset>
              ))}
              <div className="row">
                <button type="button" className="btn" onClick={() => setDependencies((values) => [...values, { task_id: '', baseline_commit: SHA.test(baseline.trim()) ? baseline.trim() : '', detail: '' }])}>添加依赖声明</button>
                <button type="button" className="btn" onClick={() => setDecisions((values) => [...values, { question: '', resolution: '', detail: '' }])}>添加用户决定</button>
              </div>
            </div>
          </details>
        </Section>
        <Section title="发起规划">
          <ActionButton action="plan.create" primary label="开始单卡规划" disabled={!!reason} reason={reason}
            args={() => ({
              baseline: baseline.trim(), requirements,
              ...(draftRequest ? { draft: { model: draftRequest.model, effort: draftRequest.effort } } : {}),
              ...(reviewRequest ? { plan_review: { model: reviewRequest.model, effort: reviewRequest.effort } } : {}),
              ...(dependencies.length || decisions.length ? { declarations: {
                dependencies: dependencies.map((value) => ({ ...value, task_id: value.task_id.trim(), baseline_commit: value.baseline_commit.trim().toLowerCase(), source: 'user_declared' })),
                decisions: decisions.map((value) => ({ ...value, source: 'user_declared' })),
              } } : {}),
            })}
            onDone={(job) => {
              if (window.location.pathname !== '/task-plans/new') return;
              const plan = job.resources?.find((resource) => resource.kind === 'plan');
              navigate(plan ? to('task-plans', plan.id) : to('jobs', job.job_id));
            }} />
        </Section>
      </fieldset>
    </>
  );
}

export function NewTaskPlanPage({ from, questionIndex }: { from: string | null; questionIndex: string | null }) {
  const ws = useWorkspace();
  const writesAllowed = useWorkspaceWritesAllowed();
  const read = useResource<PlanningView>('plan', from, { implementation: 1 }, { enabled: writesAllowed });
  const plan = read.data?.id === from ? read.data.data : null;
  const question = questionIndex !== null && /^\d+$/.test(questionIndex) ? plan?.questions?.[Number(questionIndex)] : undefined;
  return (
    <Page title="新建单卡规划" aside={<Link className="btn small" href="/task-plans">打开规划</Link>}>
      <ReadFrame read={ws} what="工作区">
        {(workspace) => !writesAllowed || workspace.read_only ? (
          <EmptyState title="工作区为只读" action={<Link className="btn" href="/archive">归档</Link>} />
        ) : from ? (
          <ReadFrame read={{ ...read, data: plan }} what="原单卡规划">
            {(record) => record.standalone && record.details?.input ? <TaskPlanForm ws={workspace} source={record.details.input} question={question} />
              : <EmptyState title="原记录无法带入单卡规划" action={<Link className="btn" href="/task-plans/new">从空白新建</Link>} />}
          </ReadFrame>
        ) : <TaskPlanForm ws={workspace} />}
      </ReadFrame>
    </Page>
  );
}
