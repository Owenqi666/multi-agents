import { api } from '../api/client';
import { ENVIRONMENT_ACTION, jobs, useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import { ApiError, errorText } from '../api/transport';
import { isUnfinished, type Job } from '../api/types';
import { useWorkspaceWritesAllowed } from '../api/workspace';
import { actionLabel, timeText } from '../app/labels';
import { Link, to } from '../app/router';
import { JobBadge, jobEnding } from '../components/Badge';
import { EmptyState } from '../components/EmptyState';
import { LogBlock } from '../components/LogBlock';
import { Facts, Page, RecordLink, Raw, Section } from './common';
import { JobActivityView, useJobActivity } from './JobActivity';
import { useState } from 'react';
import { tx, joinList, pair } from '../i18n';

function argsSummary(args: Job['args']): string {
  if (!args) return '';
  return joinList(Object.entries(args)
    .map(([k, v]) => {
      if (v && typeof v === 'object' && 'length' in (v as object) && !Array.isArray(v)) return tx('{0}：{1} 字', k, (v as { length: number }).length);
      if (Array.isArray(v)) return tx('{0}：{1} 项', k, v.length);
      if (v && typeof v === 'object') return pair(k, Object.values(v as object).join(' · '));
      return pair(k, v);
    }), '，');
}

export function JobsPage() {
  const { recent, ready } = useJobs();
  const writesAllowed = useWorkspaceWritesAllowed();
  return (
    <Page kicker={tx('作业')} title={tx('最近的作业')} aside={<button type="button" className="btn small" onClick={() => void jobs.refreshRecent()}>{tx('刷新列表')}</button>}>
      {!ready ? <p className="loading">{tx('正在读取作业…')}</p> : !recent.length ? (
        <EmptyState title={tx('还没有作业')} body={writesAllowed ? tx('提出目标、生成计划或领取阶段时，页面会提交作业，它们会列在这里。') : undefined}
          action={writesAllowed ? <Link className="btn primary" href="/new">{tx('提出目标')}</Link> : undefined} />
      ) : (
        <ul className="job-list">
          {recent.map((j) => (
            <li key={j.job_id}>
              <JobBadge job={j} />
              <Link href={to('jobs', j.job_id)} className="job-list-action">{actionLabel(j.action)}</Link>
              <span className="stamp">{timeText(j.created_at ?? j.started_at)}</span>
              {j.exit_code !== null && j.exit_code !== undefined ? <span className="stamp">{tx('退出码')} {j.exit_code}</span> : null}
              <span className="job-list-args">{argsSummary(j.args)}</span>
            </li>
          ))}
        </ul>
      )}
    </Page>
  );
}

/** Show the job receipt and preserve stderr from both old and structured CLI output. Below
 *  it: who was last seen doing what, which review seats have a report, and what can continue. */
/** Actions whose receipt is a review's conclusion. */
const REVIEW_ACTIONS = new Set([
  'task.review', 'task.retry_review', 'task.confirm', 'integration.review', 'integration.retry_review', 'integration.confirm',
  'review.run', 'review.retry', 'review.confirm',
]);

export function JobPage({ id }: { id: string }) {
  const { generation, active, recent } = useJobs();
  const writesAllowed = useWorkspaceWritesAllowed();
  const read = useRead<Job>(`job/${id}/${generation}`, () => api.job(id), { locking: false });
  const activity = useJobActivity(id);
  const [problem, setProblem] = useState<string | null>(null);
  // The running job is polled every second by the job center; use its copy while it runs.
  // Right after it ends this page's own reading is still on its way (or may fail): the job
  // center's last copy says how it ended, and is used until a reading of an ended job is here.
  const last = recent.find((j) => j.job_id === id) ?? null;
  const job = active?.job_id === id ? active : read.data && !isUnfinished(read.data) ? read.data : last && !isUnfinished(last) ? last : read.data ?? last;
  if (!job) {
    const missing = read.error instanceof ApiError && read.error.status === 404;
    return (
      <Page kicker={tx('作业')} title={tx('作业')}>
        {read.error ? (
          <EmptyState title={missing ? tx('找不到这个作业') : tx('读不到这个作业')} body={errorText(read.error)}
            action={missing ? undefined : <button type="button" className="btn" onClick={read.reload}>{tx('重新读取')}</button>} />
        ) : <p className="loading">{tx('正在读取作业…')}</p>}
      </Page>
    );
  }
  const ending = jobEnding(job);
  const refused = ending === 'refused';
  // Exit code 1 with a receipt. For a review or a test the receipt is a conclusion to read;
  // for other actions the page only says the result is not a success.
  const kindOfWork = REVIEW_ACTIONS.has(job.action) ? 'review' : job.action === 'task.test' || job.action === 'integration.test' ? 'test' : 'other';
  const logs = job.artifacts ?? [];
  // The activity also knows the records named in the request, before any receipt exists.
  const named = activity.data?.resources ?? [];
  const records = named.length ? named : job.resources ?? [];
  return (
    <Page kicker={tx('作业')} title={actionLabel(job.action)} aside={
      <span className="row">
        <JobBadge job={job} />
        {writesAllowed && job.state === 'running' ? <button type="button" className="btn small" onClick={() => jobs.cancel(job.job_id).catch((e) => setProblem(errorText(e)))}>{tx('取消作业')}</button> : null}
      </span>
    }>
      {problem ? <p className="stale-note problem">{problem}</p> : null}
      {job.state === 'cancelling' ? <p className="stale-note">{tx('已请求取消，等 CLI 退出后按退出码和领域状态显示结果。')}</p> : null}
      {ending === 'unmet' ? (
        <p className="stale-note" role="status">
          {kindOfWork === 'review' ? tx('这个作业跑到了结束。退出码 {0} 表示审核的结论不是通过：打开下面的审核记录看意见，再决定返工还是交叉确认。', job.exit_code ?? '—')
            : kindOfWork === 'test' ? tx('这个作业跑到了结束。退出码 {0} 表示测试没有通过：打开下面的相关记录看失败的命令和日志。', job.exit_code ?? '—')
              : tx('这个作业跑到了结束并留下了结果。退出码 {0} 表示结果不是成功：可能是测试没过、审核要求返工，也可能是规划或返工没有做成。是哪一种，看下面的相关记录。', job.exit_code ?? '—')}
        </p>
      ) : null}
      {refused ? (
        <p className="stale-note problem" role="status">{tx('服务受理了这次提交，执行时拒绝了它。通常是提交时页面上的状态已经过期，记录没有被改动；少数情况下拒绝发生在执行中途。以相关记录现在的内容为准，回去重新读取，再决定下一步。')}</p>
      ) : null}
      {job.state === 'interrupted' ? (
        <div className="stale-note">
          {/* Interrupted by the service going away, or (the service says so in the kind of the
              error) lost for another reason: then the page does not talk of a restart. */}
          <p className="row">{job.error_kind === undefined || job.error_kind === null || job.error_kind === 'interrupted'
            ? tx('服务重启时这个作业还没结束，已失去控制。它启动的 CLI 可能还在改记录，先打开相关记录看当前状态，再用页面上的恢复或重试操作继续。')
            : tx('这个作业没有正常收尾，服务没能记下它的结果，原因见下面的作业错误。先打开相关记录看当前状态，再用页面上的恢复或重试操作继续。')}</p>
          {job.recovery_consultation_id ? <Link href={to('consultations', job.recovery_consultation_id)}>{tx('打开中断咨询')}</Link> : null}
        </div>
      ) : null}
      <Section title={tx('概况')}>
        <Facts rows={[
          [tx('动作'), <code>{job.action}</code>],
          [tx('退出码'), job.exit_code === null || job.exit_code === undefined ? (isUnfinished(job) ? tx('还没结束') : job.state === 'interrupted' ? tx('无（作业中断，没有观测到退出码）') : (job.action === ENVIRONMENT_ACTION ? tx('无（环境检查在服务内执行）') : tx('无（没有观测到退出码）'))) : String(job.exit_code)],
          [tx('受理'), timeText(job.created_at)],
          [tx('开始'), timeText(job.started_at)],
          [tx('请求取消'), timeText(job.cancel_requested_at)],
          [tx('结束'), timeText(job.finished_at)],
          [tx('参数'), argsSummary(job.args) || '—'],
          [tx('错误码'), job.error ? <code>{job.error.code}</code> : tx('无（不代表成功，看退出码和记录）')],
        ]} />
      </Section>
      {records.length ? (
        <Section title={tx('相关记录')}>
          <ul className="plain-list">{records.map((r) => <li key={`${r.kind}/${r.id}`}><RecordLink kind={r.kind} id={r.id} /></li>)}</ul>
        </Section>
      ) : null}
      {job.error ? (
        <Section title={refused ? tx('被拒绝的原因') : tx('作业错误')}>
          <p className="goal-text">{job.error.message}</p>
        </Section>
      ) : null}
      <JobActivityView job={job} activity={activity} />
      {job.stderr_tail ? (
        <Section title={tx('stderr 末尾')}>
          <LogBlock title="stderr" stream="stderr" page={{ offset: 0, next_offset: null, byte_count: null, text: job.stderr_tail ?? '' }} />
        </Section>
      ) : null}
      {logs.length ? (
        <Section title={tx('作业日志')}>
          <ul className="plain-list">
            {logs.map((a) => <li key={a.artifact_id}><Link href={`${to('logs', a.artifact_id)}?title=${encodeURIComponent(`${actionLabel(job.action)} ${a.stream}`)}`}>{a.stream}</Link> <span className="stamp">{a.byte_count.toLocaleString('en-US')} {tx('字节')}</span></li>)}
          </ul>
        </Section>
      ) : null}
      {job.result ? <Section title={tx('结果')}><Raw value={job.result} label={tx('CLI 输出的 JSON')} /></Section> : null}
    </Page>
  );
}
