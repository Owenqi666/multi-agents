import { api } from '../api/client';
import { jobs, useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import { errorText } from '../api/transport';
import { isUnfinished, type Job } from '../api/types';
import { actionLabel, timeText } from '../app/labels';
import { Link, to } from '../app/router';
import { JobBadge } from '../components/Badge';
import { EmptyState } from '../components/EmptyState';
import { LogBlock } from '../components/LogBlock';
import { Facts, Page, RecordLink, Raw, Section } from './common';
import { useState } from 'react';

function argsSummary(args: Job['args']): string {
  if (!args) return '';
  return Object.entries(args)
    .map(([k, v]) => {
      if (v && typeof v === 'object' && 'length' in (v as object) && !Array.isArray(v)) return `${k}：${(v as { length: number }).length} 字`;
      if (Array.isArray(v)) return `${k}：${v.length} 项`;
      if (v && typeof v === 'object') return `${k}：${Object.values(v as object).join(' · ')}`;
      return `${k}：${String(v)}`;
    })
    .join('，');
}

export function JobsPage() {
  const { recent, ready } = useJobs();
  return (
    <Page kicker="作业" title="最近的作业" aside={<button type="button" className="btn small" onClick={() => void jobs.refreshRecent()}>刷新列表</button>}>
      {!ready ? <p className="loading">正在读取作业…</p> : !recent.length ? (
        <EmptyState title="还没有作业" body="提出目标、生成计划或领取阶段时，页面会提交作业，它们会列在这里。" action={<Link className="btn primary" href="/new">提出目标</Link>} />
      ) : (
        <ul className="job-list">
          {recent.map((j) => (
            <li key={j.job_id}>
              <JobBadge job={j} />
              <Link href={to('jobs', j.job_id)} className="job-list-action">{actionLabel(j.action)}</Link>
              <span className="stamp">{timeText(j.created_at ?? j.started_at)}</span>
              {j.exit_code !== null && j.exit_code !== undefined ? <span className="stamp">退出码 {j.exit_code}</span> : null}
              <span className="job-list-args">{argsSummary(j.args)}</span>
            </li>
          ))}
        </ul>
      )}
    </Page>
  );
}

/** One job: its state, exit code, records it touched, stderr when it was refused. Exit 2
 *  without a result is a refusal whose reason is only in stderr, shown as written. */
export function JobPage({ id }: { id: string }) {
  const { generation, active } = useJobs();
  const read = useRead<Job>(`job/${id}/${generation}`, () => api.job(id), { locking: false });
  const [problem, setProblem] = useState<string | null>(null);
  // The running job is polled every second by the job center; use its copy while it runs.
  const job = active?.job_id === id ? active : read.data;
  if (!job) return <Page kicker="作业" title="作业">{read.error ? <EmptyState title="找不到这个作业" body={errorText(read.error)} /> : <p className="loading">正在读取作业…</p>}</Page>;
  const refused = job.state === 'finished' && job.exit_code === 2 && job.result === null;
  const logs = job.artifacts ?? [];
  return (
    <Page kicker="作业" title={actionLabel(job.action)} aside={
      <span className="row">
        <JobBadge job={job} />
        {job.state === 'running' ? <button type="button" className="btn small" onClick={() => jobs.cancel(job.job_id).catch((e) => setProblem(errorText(e)))}>取消作业</button> : null}
      </span>
    }>
      {problem ? <p className="stale-note problem">{problem}</p> : null}
      {job.state === 'cancelling' ? <p className="stale-note">已请求取消，等 CLI 退出后按退出码和领域状态显示结果。</p> : null}
      {job.state === 'interrupted' ? (
        <p className="stale-note">服务重启时这个作业还没结束，已失去控制。它启动的 CLI 可能还在改记录，先打开相关记录看当前状态，再用页面上的恢复或重试操作继续。</p>
      ) : null}
      <Section title="概况">
        <Facts rows={[
          ['动作', <code>{job.action}</code>],
          ['退出码', job.exit_code === null || job.exit_code === undefined ? (isUnfinished(job) ? '还没结束' : '无（环境检查在服务内执行）') : String(job.exit_code)],
          ['受理', timeText(job.created_at)],
          ['开始', timeText(job.started_at)],
          ['请求取消', timeText(job.cancel_requested_at)],
          ['结束', timeText(job.finished_at)],
          ['参数', argsSummary(job.args) || '—'],
          ['错误码', job.error ? <code>{job.error.code}</code> : '无（不代表成功，看退出码和记录）'],
        ]} />
      </Section>
      {job.resources?.length ? (
        <Section title="相关记录">
          <ul className="plain-list">{job.resources.map((r) => <li key={`${r.kind}/${r.id}`}><RecordLink kind={r.kind} id={r.id} /></li>)}</ul>
        </Section>
      ) : null}
      {refused || job.stderr_tail ? (
        <Section title={refused ? '被拒绝的原因' : 'stderr 末尾'} note={refused ? '退出码 2，没有结果。原因只在 stderr 里，按原文显示。' : undefined}>
          <LogBlock title="stderr" stream="stderr" page={{ offset: 0, next_offset: null, byte_count: null, text: job.stderr_tail ?? '' }} />
        </Section>
      ) : null}
      {logs.length ? (
        <Section title="作业日志">
          <ul className="plain-list">
            {logs.map((a) => <li key={a.artifact_id}><Link href={`${to('logs', a.artifact_id)}?title=${encodeURIComponent(`${actionLabel(job.action)} ${a.stream}`)}`}>{a.stream}</Link> <span className="stamp">{a.byte_count.toLocaleString('en-US')} 字节</span></li>)}
          </ul>
        </Section>
      ) : null}
      {job.result ? <Section title="结果"><Raw value={job.result} label="CLI 输出的 JSON" /></Section> : null}
    </Page>
  );
}
