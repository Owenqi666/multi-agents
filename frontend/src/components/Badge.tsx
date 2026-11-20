import type { ReactNode } from 'react';
import type { Job } from '../api/types';

export type BadgeTone = 'ok' | 'fail' | 'warn' | 'live' | 'muted' | 'plain';

/** A small status mark: an indicator light and a word. */
export function Badge({ tone, children, title }: { tone: BadgeTone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      <i aria-hidden="true" />
      {children}
    </span>
  );
}

/** Marks a value the runtime did not report. Never filled in from the request. */
export function Unconfirmed({ what }: { what?: string }) {
  return <span className="unconfirmed-mark" title={what ? `${what}没有运行时报告值` : '没有运行时报告值'}>未确认</span>;
}

/** The requested value, labelled so it is never read as evidence. */
export function Requested({ children }: { children: ReactNode }) {
  return <span className="tag-request"><b>请求</b>{children}</span>;
}

export interface JobBadgeInfo {
  tone: BadgeTone;
  label: string;
}

/** Job state in words. A finished job is judged by its exit code together with the domain
 *  state read back afterwards; error being null does not mean success. */
export function jobBadge(job: Pick<Job, 'state' | 'exit_code' | 'result' | 'error'>): JobBadgeInfo {
  switch (job.state) {
    case 'queued':
      return { tone: 'muted', label: '排队中' };
    case 'running':
      return { tone: 'live', label: '运行中' };
    case 'cancelling':
      return { tone: 'warn', label: '已请求取消' };
    case 'interrupted':
      return { tone: 'warn', label: '已中断' };
    case 'finished':
      if (job.exit_code === 130 || job.error?.code === 'cli_cancelled') return { tone: 'muted', label: '已取消' };
      if (job.exit_code === 2 && job.result === null) return { tone: 'fail', label: '被拒绝' };
      if (job.exit_code === 0 || (job.exit_code === null && job.error === null)) return { tone: 'ok', label: '已结束' };
      if (job.exit_code === 1 && job.result !== null) return { tone: 'warn', label: '结果不满足' };
      return { tone: 'fail', label: '失败' };
  }
}

export function JobBadge({ job }: { job: Pick<Job, 'state' | 'exit_code' | 'result' | 'error'> }) {
  const b = jobBadge(job);
  return <Badge tone={b.tone}>{b.label}</Badge>;
}
