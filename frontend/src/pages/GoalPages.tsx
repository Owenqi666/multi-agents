// 提出目标 and 需求咨询: write the goal, optionally ask Astra and/or Fable to refine it in
// turn, then confirm it as given or after editing (accepted / edited).

import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import type { Advice, Consultation, Job, RunItem, Workspace } from '../api/types';
import { shortId, timeText } from '../app/labels';
import { navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { EmptyState } from '../components/EmptyState';
import { candidatesFor, ModelPicker, requestFrom, type PickerChoice } from '../components/ModelPicker';
import { ReadFrame } from '../components/ReadNote';
import { ArtifactLinks, Evidence, Facts, Page, Section, useResource } from './common';

export function useWorkspace() {
  return useRead<Workspace>('workspace', () => api.workspace(), { locking: false });
}

/** Codex candidates appear only after a successful environment check; offer it, never run it
 *  on page load. */
export function EnvironmentHint({ ws }: { ws: Workspace | null }) {
  if (!ws) return null;
  const missing = (['codex', 'claude'] as const).filter((p) => !ws.environments[p]);
  if (!missing.length) return null;
  return (
    <div className="hint">
      <p>{missing.includes('codex') ? 'Astra、Sol、Terra 要等第一次 Codex 环境检查成功后才出现在选择器里。' : 'Claude 还没有检查过环境，候选用的是工作台的建议值。'}</p>
      <span className="row">
        {missing.map((p) => <ActionButton key={p} action="environment.refresh" args={() => ({ provider: p })} label={`检查 ${p === 'codex' ? 'Codex' : 'Claude'} 环境`} />)}
      </span>
    </div>
  );
}

function jobId(job: Job, kind: string): string | null {
  return job.resources?.find((r) => r.kind === kind)?.id ?? null;
}

export function NewGoalPage() {
  const ws = useWorkspace();
  const w = ws.data;
  const projects = useRead<{ items: RunItem[] }>('runs/project', () => api.runs('project'), { locking: false });
  const { active } = useJobs();
  const [goal, setGoal] = useState('');
  const [baseline, setBaseline] = useState('');
  const [projectId, setProjectId] = useState('');
  const [advisors, setAdvisors] = useState<0 | 1 | 2>(1);
  const [first, setFirst] = useState<PickerChoice | null>(null);
  const [second, setSecond] = useState<PickerChoice | null>(null);
  useEffect(() => { if (w?.head && !baseline) setBaseline(w.head); }, [w, baseline]);
  const setA = useCallback((v: PickerChoice | null) => setFirst(v), []);
  const setB = useCallback((v: PickerChoice | null) => setSecond(v), []);

  const firstReq = requestFrom(w, 'advisor', first);
  const secondReq = requestFrom(w, 'advisor', second);
  const sameProvider = advisors === 2 && firstReq && secondReq && firstReq.provider === secondReq.provider;
  const ready = goal.trim().length > 0 && baseline.length > 0;
  const consulting = active?.action === 'project.consult';
  const common = () => ({ baseline, original_goal: goal, ...(projectId ? { project_id: projectId } : {}) });
  const secondFamily = firstReq ? candidatesFor(w, 'advisor').filter((c) => c.provider !== firstReq.provider).map((c) => c.family) : undefined;

  return (
    <Page kicker="目标" title="提出目标">
      <EnvironmentHint ws={w} />
      <Section title="写下目标" note="按你自己的话写，顾问和规划都只读这段原文和仓库。">
        <textarea className="field big" rows={7} value={goal} onChange={(e) => setGoal(e.target.value)} aria-label="原始目标" placeholder="例如：做一个本机网页，能从目标一路操作到接受，看得到每一步是哪个模型在做。" />
        <div className="form-row">
          <label>基线提交<input className="field mono" value={baseline} onChange={(e) => setBaseline(e.target.value.trim())} /></label>
          {w?.branches?.length ? (
            <label>或选分支<select className="field" value="" onChange={(e) => e.target.value && setBaseline(e.target.value)}>
              <option value="">—</option>
              {w.branches.map((b) => <option key={b.name} value={b.head}>{b.name}（{shortId(b.head, 7)}）</option>)}
            </select></label>
          ) : null}
          {projects.data?.items.length ? (
            <label>归入项目<select className="field" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">新建项目</option>
              {projects.data.items.filter((p) => p.kind === 'project').map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select></label>
          ) : null}
        </div>
      </Section>
      <Section title="请顾问完善" note="一到两位，依次进行，第二位能读到第一位的意见（不是盲审）。两位时提供方必须不同。">
        <div className="seg small" role="radiogroup" aria-label="顾问人数">
          {([0, 1, 2] as const).map((n) => <button key={n} type="button" role="radio" aria-checked={advisors === n} className={advisors === n ? 'on' : ''} onClick={() => setAdvisors(n)}>{n === 0 ? '不请顾问' : n === 1 ? '一位' : '两位'}</button>)}
        </div>
        {advisors >= 1 ? <ModelPicker ws={w} role="advisor" value={first} onChange={setA} label="第一位顾问" /> : null}
        {advisors === 2 ? <ModelPicker ws={w} role="advisor" value={second} onChange={setB} label="第二位顾问" only={secondFamily} /> : null}
        {sameProvider ? <p className="action-why problem">两位顾问的提供方必须不同。</p> : null}
      </Section>
      <div className="form-actions">
        {advisors > 0 ? (
          <ActionButton action="project.consult" primary label="请顾问完善" disabled={!ready || !firstReq || (advisors === 2 && (!secondReq || !!sameProvider))}
            reason={!ready ? '先写下目标和基线。' : '先选好顾问的模型。'}
            args={() => ({ ...common(), advisors: advisors === 2 ? [firstReq, secondReq] : [firstReq] })}
            onDone={(job) => { const id = jobId(job, 'consultation'); navigate(id ? to('consultations', id) : to('jobs', job.job_id)); }} />
        ) : null}
        <ActionButton action="project.confirm" label="直接确认为目标" disabled={!ready} reason="先写下目标和基线。"
          confirm="不经顾问，直接把这段原文确认为项目目标？"
          args={() => ({ ...common(), confirmed_goal: goal })}
          onDone={(job) => { const id = jobId(job, 'project'); navigate(id ? to('projects', id) : to('jobs', job.job_id)); }} />
      </div>
      {consulting ? <p className="stale-note">需求咨询进行中，每位顾问最多 300 秒。咨询的 ID 在作业结束后才知道，完成后自动打开。</p> : null}
    </Page>
  );
}

function Items({ title, items }: { title: string; items: unknown[] }) {
  if (!items.length) return null;
  return (
    <div className="advice-items">
      <h3>{title}</h3>
      <ul>{items.map((it, i) => <li key={i}>{typeof it === 'string' ? it : Object.entries(it as object).map(([k, v]) => `${k}：${typeof v === 'string' ? v : JSON.stringify(v)}`).join('；')}</li>)}</ul>
    </div>
  );
}

function AdviceView({ advice }: { advice: Advice }) {
  return (
    <div className="advice">
      <pre className="goal-text">{advice.draft_goal}</pre>
      <Items title="依据" items={advice.background} />
      <Items title="假设" items={advice.assumptions} />
      <Items title="需要你决定" items={advice.questions} />
      <Items title="两位顾问没谈拢的地方" items={advice.remaining_disagreements} />
    </div>
  );
}

const SEAT_STATUS: Record<string, [Parameters<typeof Badge>[0]['tone'], string]> = {
  pending: ['muted', '等待'], running: ['live', '进行中'], succeeded: ['ok', '完成'], failed: ['fail', '失败'],
};

export function ConsultationPage({ id }: { id: string }) {
  const read = useResource<Consultation>('consultation', id);
  const [text, setText] = useState<string | null>(null);
  return (
    <Page kicker={`需求咨询 · ${shortId(id)}`} title="需求咨询">
      <ReadFrame read={{ ...read, data: read.data?.data ?? null }} what="需求咨询">
        {(c) => {
          const draft = c.draft;
          const value = text ?? draft?.draft_goal ?? '';
          const edited = draft ? value !== draft.draft_goal : false;
          return (
            <>
              <Section title="原始目标"><pre className="goal-text">{c.original_goal}</pre>
                <Facts rows={[['基线', <code>{shortId(c.baseline_commit, 10)}</code>], ['开始', timeText(c.started_at)], ['结束', timeText(c.finished_at)]]} />
              </Section>
              {c.seats.map((s) => (
                <Section key={s.number} title={`顾问 ${s.number}`} note={s.number === 2 ? '读到了第一位的意见' : undefined}>
                  <p className="row"><Badge tone={SEAT_STATUS[s.status]?.[0] ?? 'muted'}>{SEAT_STATUS[s.status]?.[1] ?? s.status}</Badge>
                    <Evidence reportedModel={s.evidence?.reported_model} reportedEffort={s.evidence?.reported_effort} request={s.request} /></p>
                  {s.advice ? <AdviceView advice={s.advice} /> : s.status === 'pending' ? <p className="loading">还没开始。</p> : null}
                  <ArtifactLinks artifacts={(read.data?.artifacts ?? []).filter((a) => a.coordinates?.seat === s.number)}
                    label={(a) => `顾问 ${s.number} · ${a.stream ?? '日志'} ${a.coordinates?.log_index ?? ''}`.trim()} />
                </Section>
              ))}
              {draft ? (
                <Section title="确认目标" note="原样确认记为「原样采纳」，改过再确认记为「改过」。">
                  <textarea className="field big" rows={6} value={value} onChange={(e) => setText(e.target.value)} aria-label="确认的目标" />
                  <div className="form-actions">
                    <span className="stamp">{edited ? '已改过草稿' : '与草稿相同'}</span>
                    <ActionButton action="project.confirm" primary label={edited ? '按改过的确认' : '原样确认'} disabled={!value.trim()} reason="目标不能为空。"
                      args={() => ({ baseline: c.baseline_commit, original_goal: c.original_goal, confirmed_goal: value, consultation_id: c.consultation_id, adoption: edited ? 'edited' : 'accepted', ...(c.project_id ? { project_id: c.project_id } : {}) })}
                      onDone={(job) => { const pid = jobId(job, 'project'); navigate(pid ? to('projects', pid) : to('jobs', job.job_id)); }} />
                  </div>
                </Section>
              ) : c.status === 'failed' ? (
                <EmptyState compact title="咨询没有完成" body="有一位顾问失败，没有可以确认的草稿。可以重新提出目标，或者直接确认原文。" />
              ) : null}
            </>
          );
        }}
      </ReadFrame>
    </Page>
  );
}
