import { useState } from 'react';
import { useJobs } from '../api/jobs';
import type { FindingMember, Job, PlanningDecision, PlanningDependency, PlanningInputView, PlanningQuestion, PlanningView, ReviewFindings, RoleRequest, Workspace } from '../api/types';
import { useWorkspace, useWorkspaceWritesAllowed } from '../api/workspace';
import { shortId } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge, endingText, jobEnding } from '../components/Badge';
import { EmptyState } from '../components/EmptyState';
import { candidatesFor, effortsFor, ModelPicker, requestFrom, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { EnvironmentHint } from './GoalPages';
import { Facts, Page, Section, useResource } from './common';
import { useFindings } from './ReviewFindings';
import { tx } from '../i18n';

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

/** The finding a correction starts from, and the commit the review looked at. */
export interface Correction {
  member: FindingMember;
  source: ReviewFindings['source'];
  /** Read the findings again (after a submission that did not lead to a plan page). */
  reload?: () => void;
}

/** The finding in the reviewer's own words, as the start of the requirements. */
function correctionRequirements({ seat, finding: f }: FindingMember): string {
  const lines = f.line_end !== f.line_start ? `${f.line_start}-${f.line_end}` : String(f.line_start);
  return [
    tx('修复审核意见（{0} · {1}）：{2}', seat, f.id, f.title),
    tx('位置：{0}:{1}（{2}）', f.path, lines, f.side === 'baseline' ? tx('基线一侧') : tx('候选一侧')),
    tx('影响：{0}', f.impact),
    tx('依据：{0}', f.rationale),
    tx('引用的原文：'),
    f.quote,
    '',
    tx('拟修内容：'),
    '',
  ].join('\n');
}

/** The request the service makes for a planning seat the user left unspecified. A service
 *  that does not report its defaults gets no line: the page does not guess them. */
function DefaultRequest({ request }: { request: RoleRequest | null | undefined }) {
  if (!request || typeof request.model !== 'string' || !request.model) return null;
  return (
    <p className="muted plan-default">
      {tx('不指定时默认使用')} <code>{request.provider}</code> · <code>{request.model}</code> · {request.effort}
    </p>
  );
}

function TaskPlanForm({ ws, source, question, correction }: { ws: Workspace; source?: PlanningInputView; question?: PlanningQuestion; correction?: Correction }) {
  const { active, settling, submitting } = useJobs();
  // A correction starts from the commit that was reviewed. A pasted diff has none, so the
  // baseline is left for the user to choose.
  const reviewed = correction?.source?.correction_baseline ?? null;
  const [baseline, setBaseline] = useState(correction ? reviewed ?? '' : source?.frozen_rules?.baseline_commit ?? ws.head ?? '');
  const [requirements, setRequirements] = useState(correction ? correctionRequirements(correction.member) : source?.requirements ?? '');
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
  // A correction whose job ended without a plan to open stays on this form and says how the
  // job ended. Submitting the same request again is safe: the service keeps one planning per
  // finding and frozen input, and answers a repeat with the planning it already has.
  const [ended, setEnded] = useState<Job | null>(null);
  const reason = correction && !reviewed && !baseline.trim() ? tx('粘贴的补丁没有候选提交，请明确填写这个仓库里的基线。')
    : !baseline.trim() || !requirements.trim() ? tx('先填写基线和需求。')
    : (draftExplicit && !draftRequest) || (reviewExplicit && !reviewRequest) ? tx('先选好指定的模型和档位。')
    : invalidDeclaration ? tx('请填完整声明。每条依赖需要完整的基线 SHA。') : null;

  function updateDependency(index: number, update: Partial<Dependency>) {
    setDependencies((values) => values.map((value, row) => row === index ? { ...value, ...update } : value));
  }
  function updateDecision(index: number, update: Partial<Decision>) {
    setDecisions((values) => values.map((value, row) => row === index ? { ...value, ...update } : value));
  }

  return (
    <>
      <EnvironmentHint ws={ws} />
      {correction ? (
        <Section title={tx('来源意见')} note={tx('这份规划会挂在这条意见下面，审核页能跳到它。审核结论仍由之后的测试和复审更新。')}>
          <Facts rows={[
            [tx('意见'), <span className="row"><Badge tone={correction.member.finding.severity === 'P0' || correction.member.finding.severity === 'P1' ? 'fail' : 'warn'}>{correction.member.finding.severity}</Badge>{correction.member.finding.title}</span>],
            [tx('发现者'), <span className="row"><code>{correction.member.seat}</code><span className="stamp">{tx('第 {0} 次', correction.member.anchor.attempt)}</span><span className="stamp">{tx('意见 ID')} <code>{correction.member.finding.id}</code></span></span>],
            [tx('审核轮'), <Link href={to('reviews', correction.member.anchor.round_id)}>{tx('审核轮')} <code>{shortId(correction.member.anchor.round_id)}</code></Link>],
            [tx('被审核的提交'), reviewed ? <code className="task-data">{reviewed}</code> : tx('没有（粘贴的补丁）')],
          ]} />
        </Section>
      ) : null}
      <fieldset className="plan-fields" aria-label={tx('单卡规划输入')} disabled={active !== null || settling || submitting !== null}>
        <Section title={tx('需求')}>
          <div className="form-row">
            <label>{tx('基线提交')}<input className="field mono" value={baseline} readOnly={!!reviewed} onChange={(event) => setBaseline(event.target.value)} /></label>
            {correction && !reviewed && ws.branches?.length ? (
              <label>{tx('或选分支')}<select className="field" value="" onChange={(event) => event.target.value && setBaseline(event.target.value)}>
                <option value="">—</option>
                {ws.branches.map((b) => <option key={b.name} value={b.head}>{tx('{0}（{1}）', b.name, shortId(b.head, 7))}</option>)}
              </select></label>
            ) : null}
          </div>
          {reviewed ? <p className="action-why">{tx('关联修复从被审核的提交开始，基线不能改。')}</p> : null}
          <textarea className="field big" rows={7} value={requirements} onChange={(event) => setRequirements(event.target.value)} aria-label={tx('单卡需求')} />
        </Section>
        <Section title={tx('规划席位')} note={tx('Claude 草拟任务卡，Codex 复核。可以指定模型和档位。')}>
          <div className="plan-models">
            <label className="check"><input type="checkbox" checked={draftExplicit} onChange={(event) => setDraftOverride(event.target.checked)} /> {tx('指定草拟模型')}</label>
            {/* What the service requests when the form names nothing. Shown, never submitted. */}
            {!draftExplicit ? <DefaultRequest request={ws.defaults?.task_plan?.draft} /> : null}
            {source?.draft_request && !previousDraft ? <p className="muted">{tx('原草拟请求')} <code>{source.draft_request.model}</code> · {source.draft_request.effort} {tx('当前不可选。可指定新的草拟模型或使用默认设置。')}</p> : null}
            {draftExplicit ? <ModelPicker ws={ws} role="review" only={DRAFT_FAMILIES} value={draftChoice} onChange={setDraft} label={tx('草拟模型')} /> : null}
            <label className="check"><input type="checkbox" checked={reviewExplicit} onChange={(event) => setReviewOverride(event.target.checked)} /> {tx('指定复核模型')}</label>
            {!reviewExplicit ? <DefaultRequest request={ws.defaults?.task_plan?.plan_review} /> : null}
            {source?.review_request && !previousReview ? <p className="muted">{tx('原复核请求')} <code>{source.review_request.model}</code> · {source.review_request.effort} {tx('当前不可选。可指定新的复核模型或使用默认设置。')}</p> : null}
            {reviewExplicit ? <ModelPicker ws={ws} role="review" only={REVIEW_FAMILIES} value={reviewChoice} onChange={setReview} label={tx('规划复核')} /> : null}
          </div>
        </Section>
        <Section title={tx('声明')}>
          <details className="raw" open={question ? true : undefined}>
            <summary>{tx('冻结声明（可选）')}</summary>
            <div className="plan-declarations">
              <p className="section-note">{tx('依赖与用户决定会随本次规划冻结。依赖基线 SHA 须与所选提交一致。')}</p>
              {dependencies.map((value, index) => (
                <fieldset className="plan-declaration" key={index}>
                  <legend>{tx('依赖声明')} {index + 1}</legend>
                  <div className="form-row">
                    <label>{tx('依赖任务 ID')} {index + 1}<input className="field mono" value={value.task_id} onChange={(event) => updateDependency(index, { task_id: event.target.value })} /></label>
                    <label>{tx('依赖基线 SHA')} {index + 1}<input className="field mono" value={value.baseline_commit} onChange={(event) => updateDependency(index, { baseline_commit: event.target.value })} /></label>
                  </div>
                  <label className="plan-label">{tx('依赖说明')} {index + 1}<textarea className="field big" rows={2} value={value.detail} onChange={(event) => updateDependency(index, { detail: event.target.value })} /></label>
                  <div><button type="button" className="btn small quiet" onClick={() => setDependencies((values) => values.filter((_, row) => row !== index))}>{tx('删除依赖声明')} {index + 1}</button></div>
                </fieldset>
              ))}
              {decisions.map((value, index) => (
                <fieldset className="plan-declaration" key={index}>
                  <legend>{tx('用户决定')} {index + 1}</legend>
                  <label className="plan-label">{tx('问题')} {index + 1}<input className="field" value={value.question} onChange={(event) => updateDecision(index, { question: event.target.value })} /></label>
                  <label className="plan-label">{tx('回答')} {index + 1}<textarea className="field big" rows={2} value={value.resolution} onChange={(event) => updateDecision(index, { resolution: event.target.value })} /></label>
                  <label className="plan-label">{tx('决定说明')} {index + 1}<input className="field" value={value.detail} onChange={(event) => updateDecision(index, { detail: event.target.value })} /></label>
                  <div><button type="button" className="btn small quiet" onClick={() => setDecisions((values) => values.filter((_, row) => row !== index))}>{tx('删除用户决定')} {index + 1}</button></div>
                </fieldset>
              ))}
              <div className="row">
                <button type="button" className="btn" onClick={() => setDependencies((values) => [...values, { task_id: '', baseline_commit: SHA.test(baseline.trim()) ? baseline.trim() : '', detail: '' }])}>{tx('添加依赖声明')}</button>
                <button type="button" className="btn" onClick={() => setDecisions((values) => [...values, { question: '', resolution: '', detail: '' }])}>{tx('添加用户决定')}</button>
              </div>
            </div>
          </details>
        </Section>
        <Section title={tx('发起规划')}>
          <ActionButton action="plan.create" primary label={tx('开始单卡规划')} disabled={!!reason} reason={reason}
            onSubmitted={() => setEnded(null)}
            onRejected={() => correction?.reload?.()}
            args={() => ({
              baseline: baseline.trim(), requirements,
              ...(correction ? { review_anchor: correction.member.anchor } : {}),
              ...(draftRequest ? { draft: { model: draftRequest.model, effort: draftRequest.effort } } : {}),
              ...(reviewRequest ? { plan_review: { model: reviewRequest.model, effort: reviewRequest.effort } } : {}),
              ...(dependencies.length || decisions.length ? { declarations: {
                dependencies: dependencies.map((value) => ({ ...value, task_id: value.task_id.trim(), baseline_commit: value.baseline_commit.trim().toLowerCase(), source: 'user_declared' })),
                decisions: decisions.map((value) => ({ ...value, source: 'user_declared' })),
              } } : {}),
            })}
            onDone={(job) => {
              if (window.location.pathname !== '/task-plans/new') return;
              // The same finding and the same frozen input were planned before: the service made
              // no second planning and names the one it has. That one may still be running, or
              // wait for an explicit retry; its page is told that it was not made just now.
              const receipt = job.result as { planning_run_id?: unknown; reused?: unknown } | null;
              if (jobEnding(job) === 'done' && receipt?.reused === true && typeof receipt.planning_run_id === 'string') {
                navigate(`${to('task-plans', receipt.planning_run_id)}?reused=1`);
                return;
              }
              const plan = job.resources?.find((resource) => resource.kind === 'plan');
              if (plan) { navigate(to('task-plans', plan.id)); return; }
              // No plan to open. For a correction, say how it ended here and read the finding
              // again: its seat may have moved on since the form was opened.
              if (correction && jobEnding(job) !== 'done') { setEnded(job); correction.reload?.(); return; }
              navigate(to('jobs', job.job_id));
            }} />
        </Section>
      </fieldset>
      {ended ? (
        <p className="stale-note problem" role="alert">
          {endingText(tx('单卡规划'), ended)} <Link href={to('jobs', ended.job_id)}>{tx('查看那个作业')}</Link>
          {/* Lost sight of: the CLI may still create the planning. A repeat of the same request opens it. */}
          {jobEnding(ended) === 'interrupted' ? ` ${tx('如果它已经启动了 CLI，那个 CLI 可能还在建规划。内容相同的重新提交不会建第二份，会打开已经建好的那一份。')}` : ''}
        </p>
      ) : null}
    </>
  );
}

/** Where a correction link points: one finding of one seat's attempt in a review round. */
export interface ReviewLink {
  round: string;
  seat: string;
  attempt: number;
  finding: string;
}

export function reviewLink(query: URLSearchParams): ReviewLink | null {
  const round = query.get('review');
  const seat = query.get('seat');
  const finding = query.get('finding');
  const attempt = Number(query.get('attempt'));
  return round && seat && finding && Number.isSafeInteger(attempt) && attempt > 0 ? { round, seat, attempt, finding } : null;
}

function CorrectionForm({ ws, link }: { ws: Workspace; link: ReviewLink }) {
  const findings = useFindings(link.round);
  const data = findings.data?.round_id === link.round ? findings.data : null;
  // The seat and the attempt are part of the identity: two seats may share a finding id.
  const member = (data?.groups ?? []).flatMap((group) => group.members ?? []).find((m) =>
    m.anchor?.seat === link.seat && m.anchor.attempt === link.attempt && m.anchor.finding_id === link.finding);
  return (
    <ReadFrame read={{ ...findings, data }} what={tx('审核意见')}>
      {(value) => member ? <TaskPlanForm ws={ws} correction={{ member, source: value.source, reload: findings.reload }} /> : (
        <EmptyState title={tx('找不到这条意见')} body={tx('这个席位可能补跑过，意见属于新的一次尝试。回到审核轮重新选择。')}
          action={<Link className="btn" href={to('reviews', link.round)}>{tx('返回审核轮')}</Link>} />
      )}
    </ReadFrame>
  );
}

export function NewTaskPlanPage({ from, questionIndex, review = null }: { from: string | null; questionIndex: string | null; review?: ReviewLink | null }) {
  const ws = useWorkspace();
  const writesAllowed = useWorkspaceWritesAllowed();
  const read = useResource<PlanningView>('plan', from, { implementation: 1 }, { enabled: writesAllowed });
  const plan = read.data?.id === from ? read.data.data : null;
  const question = questionIndex !== null && /^\d+$/.test(questionIndex) ? plan?.questions?.[Number(questionIndex)] : undefined;
  return (
    <Page title={tx('新建单卡规划')} aside={<Link className="btn small" href="/task-plans">{tx('打开规划')}</Link>}>
      <ReadFrame read={ws} what={tx('工作区')}>
        {(workspace) => !writesAllowed || workspace.read_only ? (
          <EmptyState title={tx('工作区为只读')} action={<Link className="btn" href="/archive">{tx('归档')}</Link>} />
        ) : review ? (
          <CorrectionForm ws={workspace} link={review} />
        ) : from ? (
          <ReadFrame read={{ ...read, data: plan }} what={tx('原单卡规划')}>
            {(record) => record.standalone && record.details?.input ? <TaskPlanForm ws={workspace} source={record.details.input} question={question} />
              : <EmptyState title={tx('原记录无法带入单卡规划')} action={<Link className="btn" href="/task-plans/new">{tx('从空白新建')}</Link>} />}
          </ReadFrame>
        ) : <TaskPlanForm ws={workspace} />}
      </ReadFrame>
    </Page>
  );
}
