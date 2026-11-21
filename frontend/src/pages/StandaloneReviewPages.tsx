import { useState } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import type { Job, ReviewSnapshot, RunItem, StandaloneSnapshotInput } from '../api/types';
import { useWorkspace, useWorkspaceWritesAllowed } from '../api/workspace';
import { shortId, timeText } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge } from '../components/Badge';
import { DiffView } from '../components/DiffView';
import { EmptyState } from '../components/EmptyState';
import { ReadFrame } from '../components/ReadNote';
import { EnvironmentHint } from './GoalPages';
import { useSeats } from './bits';
import { Facts, Page, Section, useResource } from './common';

const REVIEW_SCOPE = 'independent_review';
const ROUTES = { snapshot: 'snapshots', review: 'reviews' } as const;

function openResult(job: Job, kind: keyof typeof ROUTES) {
  const id = job.resources?.find((record) => record.kind === kind)?.id;
  navigate(id ? to(ROUTES[kind], id) : to('jobs', job.job_id));
}

function History({ kind, snapshot }: { kind: keyof typeof ROUTES; snapshot?: string }) {
  const title = kind === 'snapshot' ? '快照历史' : '审核历史';
  const read = useRead<{ items: RunItem[] }>(`runs/${kind}`, () => api.runs(kind), { locking: false, pollWhileJob: true });
  return (
    <Section title={title}>
      <ReadFrame read={read} what={title}>
        {({ items }) => {
          const records = items.filter((item) => item.kind === kind && item.scope === REVIEW_SCOPE && (!snapshot || item.parent_id === snapshot));
          return records.length ? (
            <ul className="plain-list" aria-label={title}>
              {records.map((item) => (
                <li key={item.id} className="row">
                  <Link href={to(ROUTES[kind], item.id)}>{item.label} <code>{shortId(item.id, 12)}</code></Link>
                  <span className="stamp">{timeText(item.updated_at)}</span>
                </li>
              ))}
            </ul>
          ) : <p className="muted">{kind === 'snapshot' ? '还没有登记的独立审核快照。' : '还没有登记的审核轮。'}</p>;
        }}
      </ReadFrame>
    </Section>
  );
}

export function ReviewHomePage() {
  const writesAllowed = useWorkspaceWritesAllowed();
  const [id, setId] = useState('');
  const [kind, setKind] = useState<keyof typeof ROUTES>('snapshot');
  return (
    <Page title="独立审核" aside={writesAllowed ? <Link className="btn primary" href="/reviews/new">新建独立审核</Link> : null}>
      <Section title="打开已有记录">
        <form className="form-row" onSubmit={(event) => { event.preventDefault(); if (id.trim()) navigate(to(ROUTES[kind], id.trim())); }}>
          <label>记录类型
            <select className="field" value={kind} onChange={(event) => setKind(event.target.value === 'review' ? 'review' : 'snapshot')}>
              <option value="snapshot">冻结快照</option>
              <option value="review">审核轮</option>
            </select>
          </label>
          <label>记录 ID<input className="field mono" value={id} onChange={(event) => setId(event.target.value)} required /></label>
          <button className="btn" type="submit" disabled={!id.trim()}>打开记录</button>
        </form>
      </Section>
      <History kind="snapshot" />
      <History kind="review" />
    </Page>
  );
}

function parseTests(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text);
    if (!value || typeof value !== 'object' || !('required_commands' in value) || !('results' in value)) return null;
    return Array.isArray(value.required_commands) && Array.isArray(value.results) ? value as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

export function NewReviewPage() {
  const { data: ws } = useWorkspace();
  const writesAllowed = useWorkspaceWritesAllowed();
  const { active, settling, submitting } = useJobs();
  const [mode, setMode] = useState<'commits' | 'patch'>('commits');
  const [baseline, setBaseline] = useState('');
  const [candidate, setCandidate] = useState('');
  const [patch, setPatch] = useState('');
  const [requirements, setRequirements] = useState('');
  const [acceptanceText, setAcceptance] = useState('');
  const [testsText, setTests] = useState('');
  const acceptance = acceptanceText.split('\n').map((line) => line.trim()).filter(Boolean);
  const tests = mode === 'commits' && testsText.trim() ? parseTests(testsText) : null;
  const reason = mode === 'patch' && !patch.trim() ? '请粘贴补丁原文。'
    : mode === 'commits' && (!baseline.trim() || !candidate.trim()) ? '请填写基线和候选提交。'
    : !requirements.trim() || !acceptance.length ? '请填写审核需求及至少一条验收标准。'
    : mode === 'commits' && testsText.trim() && !tests ? '测试证据需要包含 required_commands 和 results 数组。' : null;
  return (
    <Page title="新建独立审核" aside={<Link className="btn small" href="/reviews">审核历史</Link>}>
      {!writesAllowed ? <EmptyState title={ws?.read_only ? '工作区为只读' : '正在读取工作区'} action={ws?.read_only ? <Link className="btn" href="/archive">归档</Link> : null} /> : (
        <fieldset className="plan-fields" aria-label="独立审核输入" disabled={active !== null || settling || submitting !== null}>
          <Section title="改动" note={ws?.repository ? <code className="task-data">{ws.repository}</code> : undefined}>
            <div className="seg small" role="radiogroup" aria-label="审核输入类型">
              <button type="button" role="radio" aria-checked={mode === 'commits'} className={mode === 'commits' ? 'on' : ''} onClick={() => setMode('commits')}>提交对</button>
              <button type="button" role="radio" aria-checked={mode === 'patch'} className={mode === 'patch' ? 'on' : ''} onClick={() => setMode('patch')}>粘贴补丁</button>
            </div>
            {mode === 'commits' ? (
              <div className="form-row">
                <label>基线提交<input className="field mono" value={baseline} onChange={(event) => setBaseline(event.target.value)} /></label>
                <label>候选提交<input className="field mono" value={candidate} onChange={(event) => setCandidate(event.target.value)} placeholder={ws?.head ?? undefined} /></label>
              </div>
            ) : (
              <label className="plan-label">补丁原文
                <textarea className="field big mono" rows={8} value={patch} onChange={(event) => setPatch(event.target.value)} />
              </label>
            )}
          </Section>
          <Section title="审核目标">
            <label className="plan-label">审核需求
              <textarea className="field big" rows={4} value={requirements} onChange={(event) => setRequirements(event.target.value)} />
            </label>
            <label className="plan-label">验收标准
              <textarea className="field big" rows={3} value={acceptanceText} onChange={(event) => setAcceptance(event.target.value)} placeholder="每行一条" />
            </label>
          </Section>
          {mode === 'commits' ? (
            <Section title="测试证据">
              <details className="raw">
                <summary>添加测试证据（可选）</summary>
                <label className="plan-label review-test-input">用户提供的测试证据（JSON）
                  <textarea className="field big mono" rows={5} value={testsText} onChange={(event) => setTests(event.target.value)}
                    placeholder={'{\n  "required_commands": [["uv", "run", "pytest"]],\n  "results": []\n}'} />
                </label>
              </details>
            </Section>
          ) : null}
          <Section title="冻结">
            <ActionButton action="review.freeze" primary label="冻结审核输入" disabled={reason !== null} reason={reason}
              args={() => ({
                requirements, acceptance,
                ...(mode === 'commits' ? { baseline: baseline.trim(), candidate: candidate.trim(), ...(tests ? { tests } : {}) } : { diff: patch }),
              })}
              onDone={(job) => { if (window.location.pathname === '/reviews/new') openResult(job, 'snapshot'); }} />
          </Section>
        </fieldset>
      )}
    </Page>
  );
}

export function UserTests({ input }: { input: StandaloneSnapshotInput }) {
  if (!input.required_tests?.length && !input.tests?.length) return null;
  return (
    <Section title="测试证据" note="用户提供">
      {input.required_tests?.length ? (
        <>
          <h3 className="sub-head">要求运行的命令</h3>
          <ul className="plain-list">
            {input.required_tests.map((argv, index) => <li key={index}><code className="task-data">{JSON.stringify(argv)}</code></li>)}
          </ul>
        </>
      ) : null}
      {input.tests?.length ? <ul className="plain-list">
        {input.tests.map((test, index) => <li key={index}>
          <p className="row"><code className="task-data">{JSON.stringify(test.command)}</code><Badge tone="muted">用户提供</Badge></p>
          <p className="row"><code className="task-data">{test.commit}</code><span className="stamp">退出 {test.exit_code}</span></p>
          <details className="raw"><summary>查看测试输出</summary><pre>{test.log}</pre></details>
        </li>)}
      </ul> : null}
    </Section>
  );
}

function RunForm({ id }: { id: string }) {
  const { data: ws } = useWorkspace();
  const seats = useSeats(ws, { independent: true });
  return (
    <Section title="开始审核" note="双席读取同一份冻结输入，独立给出意见。">
      <EnvironmentHint ws={ws} />
      <div className="review-snapshot-seats">{seats.form}</div>
      <ActionButton action="review.run" primary label="开始审核" disabled={!seats.valid} reason="请选好每席的模型和档位。双席的模型须不同。"
        args={() => ({ snapshot_id: id, seats: seats.seats })}
        onDone={(job) => { if (window.location.pathname === to('snapshots', id)) openResult(job, 'review'); }} />
    </Section>
  );
}

export function ReviewSnapshotPage({ id }: { id: string }) {
  const read = useResource<ReviewSnapshot>('snapshot', id);
  const writesAllowed = useWorkspaceWritesAllowed();
  return (
    <Page title="冻结审核输入" aside={<Link className="btn small" href="/reviews">审核历史</Link>}>
      <ReadFrame read={read} what="冻结输入">
        {({ data }) => {
          if (!data) return null;
          const { input } = data;
          if (!('requirements' in input)) return <EmptyState title="这份快照随任务或集成生成" action={<Link className="btn" href={to('diffs', 'snapshot', id)}>查看冻结的改动</Link>} />;
          return <>
            <Section title="审核目标">
              <Facts rows={[
                ['快照 ID', <code className="task-data">{id}</code>],
                ['来源', input.baseline && input.candidate ? '提交对' : '粘贴补丁'],
                ...(input.baseline && input.candidate ? [
                  ['基线提交', <code className="task-data">{input.baseline}</code>],
                  ['候选提交', <code className="task-data">{input.candidate}</code>],
                ] as [string, React.ReactNode][] : []),
              ]} />
              <h3 className="sub-head">审核需求</h3>
              <pre className="goal-text">{input.requirements}</pre>
              <h3 className="sub-head">验收标准</h3>
              <ul className="plain-list" aria-label="验收标准">{input.acceptance.map((item, index) => <li key={index}>{item}</li>)}</ul>
            </Section>
            <UserTests input={input} />
            <Section title="冻结的改动">
              <DiffView diff={input.diff} />
              {input.files.length === 0 ? <details className="raw"><summary>补丁原文</summary><pre>{input.diff}</pre></details> : null}
            </Section>
            {writesAllowed && !read.stale ? <RunForm id={id} /> : null}
            <History kind="review" snapshot={id} />
          </>;
        }}
      </ReadFrame>
    </Page>
  );
}
