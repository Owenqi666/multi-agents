import { useEffect, useRef, useState } from 'react';
import { jobs, useJobs } from '../api/jobs';
import { errorText } from '../api/transport';
import type { Job } from '../api/types';
import { useWorkspaceWritesAllowed } from '../api/workspace';
import { JobBadge, jobEnding } from '../components/Badge';
import { actionLabel } from './labels';
import { Link, to } from './router';
import { tx } from '../i18n';

function elapsed(from: string | null | undefined): string {
  if (!from) return '';
  const s = Math.max(0, Math.round((Date.now() - new Date(from).getTime()) / 1000));
  return s < 60 ? tx('{0} 秒', s) : tx('{0} 分 {1} 秒', Math.floor(s / 60), s % 60);
}

/** The strip under the header: the running job with its state and, only while it is
 *  running, a cancel button; afterwards, how the last job ended. */
export function JobBar() {
  const { active, recent, environment } = useJobs();
  const writesAllowed = useWorkspaceWritesAllowed();
  const [, tick] = useState(0);
  const [problem, setProblem] = useState<string | null>(null);
  const lastActive = useRef<string | null>(null);
  const [ended, setEnded] = useState<Job | null>(null);

  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [active]);

  useEffect(() => {
    if (active) { lastActive.current = active.job_id; setEnded(null); return; }
    if (lastActive.current) {
      const done = recent.find((j) => j.job_id === lastActive.current) ?? null;
      setEnded(done);
      lastActive.current = null;
    }
  }, [active, recent]);

  const env = Object.values(environment);
  if (!active && !ended && !env.length) return null;
  return (
    <div className="job-bar" role="status" aria-live="polite">
      {active ? (
        <>
          <JobBadge job={active} />
          <span className="job-bar-action">{actionLabel(active.action)}</span>
          <span className="stamp">{elapsed(active.started_at ?? active.created_at)}</span>
          {writesAllowed && active.state === 'running' ? (
            <button type="button" className="btn small quiet" onClick={() => { setProblem(null); jobs.cancel(active.job_id).catch((e) => setProblem(errorText(e))); }}>{tx('取消作业')}</button>
          ) : null}
          <Link href={to('jobs', active.job_id)}>{tx('作业详情')}</Link>
          <span className="job-bar-note">{tx('作业结束前，和它抢仓库锁的记录不读新内容，页面保留上一次读到的内容。')}</span>
        </>
      ) : ended ? (
        <>
          <JobBadge job={ended} />
          <span className="job-bar-action">{actionLabel(ended.action)}</span>
          {ended.exit_code !== null && ended.exit_code !== undefined ? <span className="stamp">{tx('退出码')} {ended.exit_code}</span> : null}
          {/* The badge alone does not say whether the job broke or only its conclusion is not a pass. */}
          {jobEnding(ended) === 'unmet' ? <span className="job-bar-note">{tx('作业跑完了，结果不是成功，见它的记录。')}</span> : null}
          {jobEnding(ended) === 'refused' ? <span className="job-bar-note">{tx('服务拒绝了这次提交，原因见作业详情。')}</span> : null}
          <Link href={to('jobs', ended.job_id)}>{tx('作业详情')}</Link>
          <button type="button" className="btn small quiet" onClick={() => setEnded(null)}>{tx('收起')}</button>
        </>
      ) : null}
      {env.map((j) => <span key={j.job_id} className="stamp">{tx('正在检查 {0} 环境…', String((j.args as { provider?: string } | null)?.provider ?? ''))}</span>)}
      {problem ? <span className="action-why problem">{problem}</span> : null}
    </div>
  );
}
