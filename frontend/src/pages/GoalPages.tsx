// 提出目标 and 需求咨询: write the goal, check what the chosen baseline needs, optionally ask
// Astra and/or Fable to refine the goal in turn, then confirm it as given or after editing
// (accepted / edited). The unsubmitted text is kept in this browser per repository.

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { jobs, useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import { ApiError } from '../api/transport';
import { useArchiveMode, useWorkspace } from '../api/workspace';
import { isUnfinished, type Advice, type Consultation, type Job, type RunItem, type Workspace } from '../api/types';
import { shortId, timeText } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { EmptyState } from '../components/EmptyState';
import { candidatesFor, ModelPicker, requestFrom, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { ArtifactLinks, Evidence, Facts, Page, Section, useResource } from './common';
import { clearSubmitted, draftKey, forgetSent, loadDraft, loadSent, rememberSent, saveDraft } from './goalDraft';
import { OnboardingPanel, unresolved, useOnboarding } from './Onboarding';
import { tx, joinList, pair } from '../i18n';

/** Codex candidates appear only after a successful environment check; offer it, never run it
 *  on page load. */
export function EnvironmentHint({ ws }: { ws: Workspace | null }) {
  // A check that has not ended, started here or found again after a reload, holds its button:
  // the service takes one check per provider at a time.
  const { environment } = useJobs();
  if (!ws || ws.read_only) return null;
  const missing = (['codex', 'claude'] as const).filter((p) => !ws.environments[p]);
  if (!missing.length) return null;
  const name = (p: 'codex' | 'claude'): string => (p === 'codex' ? 'Codex' : 'Claude');
  return (
    <div className="hint">
      <p>{missing.includes('codex') ? tx('Astra、Sol、Terra 要等第一次 Codex 环境检查成功后才出现在选择器里。') : tx('Claude 还没有检查过环境，候选用的是工作台的建议值。')}</p>
      <span className="row">
        {missing.map((p) => (
          <ActionButton key={p} action="environment.refresh" args={() => ({ provider: p })} label={tx('检查 {0} 环境', name(p))}
            disabled={!!environment[p]} reason={tx('正在检查 {0} 环境…', name(p))} />
        ))}
      </span>
    </div>
  );
}

function jobId(job: Job, kind: string): string | null {
  return job.resources?.find((r) => r.kind === kind)?.id ?? null;
}

/** The form. `origin` is a failed consultation the inbox pointed at: its original text,
 *  baseline and project are carried in, and this form's edits are kept apart from the
 *  repository's own draft. */
function GoalForm({ w, origin }: { w: Workspace; origin: Consultation | null }) {
  const projects = useRead<{ items: RunItem[] }>('runs/project', () => api.runs('project'), { locking: false });
  const { active } = useJobs();
  const key = draftKey(w.repository, origin?.consultation_id);
  const [restored] = useState(() => loadDraft(key));
  const [goal, setGoalText] = useState(restored?.goal ?? origin?.original_goal ?? '');
  const [baseline, setBaseline] = useState(origin?.baseline_commit ?? w.head ?? '');
  const [projectId, setProjectId] = useState(origin?.project_id ?? '');
  const [advisors, setAdvisors] = useState<0 | 1 | 2>(1);
  const [first, setFirst] = useState<PickerChoice | null>(null);
  const [second, setSecond] = useState<PickerChoice | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const submitted = useRef('');
  // A goal sent from this form before the page was reloaded: its job is followed to the end.
  const [sent, setSent] = useState<{ job_id: string; taken: boolean } | null>(() => {
    const mark = loadSent(key);
    return mark && mark.goal === restored?.goal ? { job_id: mark.job_id, taken: false } : null;
  });
  const setA = useCallback((v: PickerChoice | null) => setFirst(v), []);
  const setB = useCallback((v: PickerChoice | null) => setSecond(v), []);
  const onboarding = useOnboarding(baseline, w.head);

  // Every edit is stored at once; an untouched text carried in from a consultation is not a draft.
  const setGoal = (text: string): void => {
    setGoalText(text);
    saveDraft(key, origin && text === origin.original_goal ? '' : text);
  };
  // Called once the service holds the goal or the consultation: drop the draft that was sent.
  const taken = (text: string): void => {
    clearSubmitted(key, text);
    if (origin) clearSubmitted(draftKey(w.repository), text);
  };
  const succeeded = (job: Job): boolean => job.exit_code === 0 && (jobId(job, 'consultation') ?? jobId(job, 'project')) !== null;
  // The usual end of a submission, while this page is still the one that sent it.
  const ended = (job: Job): void => {
    forgetSent(key);
    if (succeeded(job)) taken(submitted.current);
  };
  // After a reload that page is gone: follow the job that was sent and conclude the same way.
  useEffect(() => {
    const mark = loadSent(key);
    if (!mark) return;
    let alive = true;
    void (async () => {
      let job: Job;
      try {
        job = await api.job(mark.job_id);
        if (isUnfinished(job)) {
          await jobs.track(mark.job_id);
          job = await jobs.waitFor(mark.job_id);
        }
      } catch (e) {
        // A job this service does not know says nothing about the text; the draft stays.
        if (e instanceof ApiError && e.status === 404) forgetSent(key);
        if (alive) setSent(null);
        return;
      }
      forgetSent(key);
      const ok = succeeded(job);
      if (ok) taken(mark.goal);
      if (!alive) return;
      if (ok) setGoalText((now) => (now === mark.goal ? '' : now));
      setSent(ok ? { job_id: mark.job_id, taken: true } : null);
    })();
    return () => { alive = false; };
  }, [key]);

  const firstReq = requestFrom(w, 'advisor', first);
  const secondReq = requestFrom(w, 'advisor', second);
  const sameProvider = advisors === 2 && firstReq && secondReq && firstReq.provider === secondReq.provider;
  const ready = goal.trim().length > 0 && baseline.length > 0;
  const noCommit = onboarding.check?.rules.status === 'no_commit';
  const badBaseline = unresolved(onboarding.error);
  const blocked = !goal.trim() ? tx('先写下目标。')
    : noCommit ? tx('仓库还没有提交，先提交一个基线。')
    : badBaseline ? tx('这个基线解析不到提交，改一个再试。')
    : !ready ? tx('先写下目标和基线。') : null;
  const consulting = active?.action === 'project.consult';
  const common = () => {
    submitted.current = goal;
    return { baseline, original_goal: goal, ...(projectId ? { project_id: projectId } : {}) };
  };
  const secondFamily = firstReq ? candidatesFor(w, 'advisor').filter((c) => c.provider !== firstReq.provider).map((c) => c.family) : undefined;
  const canConfirm = onboarding.check?.next_steps.some((step) => step.kind === 'create_goal') ?? false;
  const signedIn = Object.values(onboarding.check?.providers ?? {}).some((provider) => provider.status === 'ready');

  return (
    <>
      <EnvironmentHint ws={w} />
      {origin ? (
        <p className="stale-note">
          {tx('从没有完成的需求咨询带入了当时的原文、基线和项目。可以重新请顾问，或直接确认原文。这个仓库另存的草稿没有动。')}
          {' '}<Link href={to('consultations', origin.consultation_id)}>{tx('打开那次咨询')}</Link>
        </p>
      ) : sent ? (
        <p className="stale-note draft-note">
          {sent.taken ? tx('上次提交的目标已经被服务接收，草稿已清除。') : tx('这段目标已经提交，作业还没结束；结束后这里的草稿会清除。')}
          {' '}<Link href={to('jobs', sent.job_id)}>{tx('查看那个作业')}</Link>
        </p>
      ) : restored && !dismissed ? (
        <p className="stale-note draft-note">
          {tx('已恢复这个仓库上次没提交的目标（{0} 存在本机浏览器里）。', timeText(restored.saved_at))}
          {' '}<button type="button" className="link" onClick={() => { setGoal(''); setDismissed(true); }}>{tx('清空草稿')}</button>
        </p>
      ) : null}
      <Section title={tx('写下目标')} note={tx('按你自己的话写，顾问和规划都只读这段原文和仓库。没提交的内容会留在这台机器的浏览器里。')}>
        <textarea className="field big" rows={7} value={goal} onChange={(e) => setGoal(e.target.value)} aria-label={tx('原始目标')} placeholder={tx('例如：做一个本机网页，能从目标一路操作到接受，看得到每一步是哪个模型在做。')} />
        <div className="form-row">
          <label>{tx('基线提交')}<input className="field mono" value={baseline} onChange={(e) => setBaseline(e.target.value.trim())} /></label>
          {w.branches?.length ? (
            <label>{tx('或选分支')}<select className="field" value="" onChange={(e) => e.target.value && setBaseline(e.target.value)}>
              <option value="">—</option>
              {w.branches.map((b) => <option key={b.name} value={b.head}>{tx('{0}（{1}）', b.name, shortId(b.head, 7))}</option>)}
            </select></label>
          ) : null}
          {projects.data?.items.length ? (
            <label>{tx('归入项目')}<select className="field" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">{tx('新建项目')}</option>
              {projects.data.items.filter((p) => p.kind === 'project').map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select></label>
          ) : null}
        </div>
      </Section>
      <OnboardingPanel read={onboarding} />
      <Section title={tx('请顾问完善')} note={tx('一到两位，依次进行，第二位能读到第一位的意见（不是盲审）。两位时提供方必须不同。')}>
        <div className="seg small" role="radiogroup" aria-label={tx('顾问人数')}>
          {([0, 1, 2] as const).map((n) => <button key={n} type="button" role="radio" aria-checked={advisors === n} className={advisors === n ? 'on' : ''} onClick={() => setAdvisors(n)}>{n === 0 ? tx('不请顾问') : n === 1 ? tx('一位') : tx('两位')}</button>)}
        </div>
        {advisors >= 1 ? <ModelPicker ws={w} role="advisor" value={first} onChange={setA} label={tx('第一位顾问')} /> : null}
        {advisors === 2 ? <ModelPicker ws={w} role="advisor" value={second} onChange={setB} label={tx('第二位顾问')} only={secondFamily} /> : null}
        {sameProvider ? <p className="action-why problem">{tx('两位顾问的提供方必须不同。')}</p> : null}
      </Section>
      <div className="form-actions">
        {advisors > 0 ? (
          <ActionButton action="project.consult" primary label={tx('请顾问完善')} disabled={!!blocked || !firstReq || (advisors === 2 && (!secondReq || !!sameProvider))}
            reason={blocked ?? tx('先选好顾问的模型。')}
            args={() => ({ ...common(), advisors: advisors === 2 ? [firstReq, secondReq] : [firstReq] })}
            onSubmitted={(job) => rememberSent(key, { job_id: job.job_id, goal: submitted.current })}
            onDone={(job) => {
              const id = jobId(job, 'consultation');
              ended(job);
              navigate(id ? to('consultations', id) : to('jobs', job.job_id));
            }} />
        ) : null}
        <ActionButton action="project.confirm" label={tx('直接确认为目标')} disabled={!!blocked} reason={blocked}
          confirm={tx('不经顾问，直接把这段原文确认为项目目标？')}
          args={() => ({ ...common(), confirmed_goal: goal })}
          onSubmitted={(job) => rememberSent(key, { job_id: job.job_id, goal: submitted.current })}
          onDone={(job) => {
            const id = jobId(job, 'project');
            ended(job);
            navigate(id ? to('projects', id) : to('jobs', job.job_id));
          }} />
      </div>
      {canConfirm && !signedIn ? <p className="action-why">{tx('还没有确认登录的模型也没关系：直接确认原文不需要顾问。')}</p> : null}
      {consulting ? <p className="stale-note">{tx('需求咨询进行中，每位顾问最多 300 秒。咨询的 ID 在作业结束后才知道，完成后自动打开。')}</p> : null}
    </>
  );
}

/** `consultation` comes from the inbox (?consultation=): a consultation that did not finish. */
export function NewGoalPage({ consultation = null }: { consultation?: string | null }) {
  const ws = useWorkspace();
  const source = useResource<Consultation>('consultation', consultation, undefined, { pollWhileJob: false });
  const origin = consultation && source.data?.id === consultation ? source.data.data : null;
  return (
    <Page kicker={tx('目标')} title={tx('提出目标')}>
      <ReadFrame read={ws} what={tx('工作区')}>
        {(w) => consultation ? (
          <ReadFrame read={{ ...source, data: origin }} what={tx('需求咨询')}>
            {(c) => <GoalForm key={c.consultation_id} w={w} origin={c} />}
          </ReadFrame>
        ) : <GoalForm key={w.repository} w={w} origin={null} />}
      </ReadFrame>
    </Page>
  );
}

function Items({ title, items }: { title: string; items: unknown[] }) {
  if (!items.length) return null;
  return (
    <div className="advice-items">
      <h3>{title}</h3>
      <ul>{items.map((it, i) => <li key={i}>{typeof it === 'string' ? it : joinList(Object.entries(it as object).map(([k, v]) => pair(k, typeof v === 'string' ? v : JSON.stringify(v))), '；')}</li>)}</ul>
    </div>
  );
}

function AdviceView({ advice }: { advice: Advice }) {
  return (
    <div className="advice">
      <pre className="goal-text">{advice.draft_goal}</pre>
      <Items title={tx('依据')} items={advice.background} />
      <Items title={tx('假设')} items={advice.assumptions} />
      <Items title={tx('需要你决定')} items={advice.questions} />
      <Items title={tx('两位顾问没谈拢的地方')} items={advice.remaining_disagreements} />
    </div>
  );
}

const SEAT_STATUS: Record<string, [Parameters<typeof Badge>[0]['tone'], string]> = {
  pending: ['muted', '等待'], running: ['live', '进行中'], succeeded: ['ok', '完成'], failed: ['fail', '失败'],
};

function consultationError(error: unknown): string | null {
  if (typeof error === 'string') return error;
  if (!error || typeof error !== 'object') return null;
  for (const key of ['detail', 'message'] as const) {
    const value = (error as Partial<Record<typeof key, unknown>>)[key];
    if (typeof value === 'string') return value;
  }
  return null;
}

export function ConsultationPage({ id }: { id: string }) {
  const read = useResource<Consultation>('consultation', id);
  const archive = useArchiveMode();
  const [text, setText] = useState<string | null>(null);
  return (
    <Page kicker={tx('需求咨询 · {0}', shortId(id))} title={tx('需求咨询')}>
      <ReadFrame read={{ ...read, data: read.data?.id === id ? read.data.data : null }} what={tx('需求咨询')}>
        {(c) => {
          const draft = c.draft;
          const value = text ?? draft?.draft_goal ?? '';
          const edited = draft ? value !== draft.draft_goal : false;
          return (
            <>
              <Section title={tx('原始目标')}><pre className="goal-text">{c.original_goal}</pre>
                <Facts rows={[
                  [tx('基线'), <code>{shortId(c.baseline_commit, 10)}</code>],
                  [tx('开始'), timeText(c.started_at)], [tx('结束'), timeText(c.finished_at)],
                  ...(c.interrupted_at ? [[tx('中断对账'), timeText(c.interrupted_at)] as [string, string]] : []),
                ]} />
                {c.status === 'running' && !archive ? (
                  <ActionButton action="project.consult.reconcile" label={tx('检查并对账')}
                    args={() => ({ consultation_id: c.consultation_id })}
                    onDone={(job) => {
                      if (job.exit_code !== 0 && window.location.pathname === to('consultations', id)) navigate(to('jobs', job.job_id));
                    }} />
                ) : null}
              </Section>
              {c.seats.map((s) => (
                <Section key={s.number} title={tx('顾问 {0}', s.number)} note={s.number === 2 && s.started_at ? tx('输入中包含第一位顾问的意见。') : undefined}>
                  <p className="row"><Badge tone={SEAT_STATUS[s.status]?.[0] ?? 'muted'}>{SEAT_STATUS[s.status] ? tx(SEAT_STATUS[s.status]![1]) : s.status}</Badge>
                    <Evidence reportedModel={s.evidence?.reported_model} reportedEffort={s.evidence?.reported_effort} request={s.request} /></p>
                  {s.advice ? <AdviceView advice={s.advice} /> : s.status === 'pending' ? <p className="loading">{tx('还没开始。')}</p> : null}
                  {s.error ? <p className="goal-text">{consultationError(s.error) ?? tx('顾问没有完成。')}</p> : null}
                  <ArtifactLinks artifacts={(read.data?.artifacts ?? []).filter((a) => a.coordinates?.seat === s.number)}
                    label={(a) => tx('顾问 {0} · {1} {2}', s.number, a.stream ?? tx('日志'), a.coordinates?.log_index ?? '').trim()} />
                </Section>
              ))}
              {draft && !archive ? (
                <Section title={tx('确认目标')} note={tx('原样确认记为「原样采纳」，改过再确认记为「改过」。')}>
                  <textarea className="field big" rows={6} value={value} onChange={(e) => setText(e.target.value)} aria-label={tx('确认的目标')} />
                  <div className="form-actions">
                    <span className="stamp">{edited ? tx('已改过草稿') : tx('与草稿相同')}</span>
                    <ActionButton action="project.confirm" primary label={edited ? tx('按改过的确认') : tx('原样确认')} disabled={!value.trim()} reason={tx('目标不能为空。')}
                      args={() => ({ baseline: c.baseline_commit, original_goal: c.original_goal, confirmed_goal: value, consultation_id: c.consultation_id, adoption: edited ? 'edited' : 'accepted', ...(c.project_id ? { project_id: c.project_id } : {}) })}
                      onDone={(job) => { const pid = jobId(job, 'project'); navigate(pid ? to('projects', pid) : to('jobs', job.job_id)); }} />
                  </div>
                </Section>
              ) : c.interrupted_at ? (
                <EmptyState compact title={tx('咨询已中断')} body={tx('已保存的顾问意见和历史记录仍可查看。')} />
              ) : c.status === 'failed' ? (
                <EmptyState compact title={tx('咨询没有完成')} body={archive ? tx('这里保留了当时的顾问结果。') : tx('有一位顾问失败，没有可以确认的草稿。可以重新提出目标，或者直接确认原文。')} />
              ) : null}
            </>
          );
        }}
      </ReadFrame>
    </Page>
  );
}
