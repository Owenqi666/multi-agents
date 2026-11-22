import type { ReactNode } from 'react';
import type { Job } from '../api/types';
import { tx } from '../i18n';

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
  return <span className="unconfirmed-mark" title={what ? tx('{0}没有运行时报告值', what) : tx('没有运行时报告值')}>{tx('未确认')}</span>;
}

/** The requested value, labelled so it is never read as evidence. */
export function Requested({ children }: { children: ReactNode }) {
  return <span className="tag-request"><b>{tx('请求')}</b>{children}</span>;
}

export interface JobBadgeInfo {
  tone: BadgeTone;
  label: string;
}

/** Some actions end a cancellation with exit code 1 and say so in their receipt: a cancelled
 *  project.plan reports result.status "failed" with error.failure_category "cancelled". */
export function cancelledInResult(result: Record<string, unknown> | null): boolean {
  if (!result) return false;
  const error = result.error as { failure_category?: string } | null | undefined;
  return error?.failure_category === 'cancelled' || result.failure_category === 'cancelled' || result.status === 'cancelled';
}

/** Job state in words. A finished job is judged by its exit code together with the domain
 *  state read back afterwards; error being null does not mean success. */
export function jobBadge(job: Pick<Job, 'state' | 'exit_code' | 'result' | 'error'>): JobBadgeInfo {
  switch (job.state) {
    case 'queued':
      return { tone: 'muted', label: tx('排队中') };
    case 'running':
      return { tone: 'live', label: tx('运行中') };
    case 'cancelling':
      return { tone: 'warn', label: tx('已请求取消') };
    case 'interrupted':
      return { tone: 'warn', label: tx('已中断') };
    case 'finished':
      if (job.exit_code === 130 || job.error?.code === 'cli_cancelled' || cancelledInResult(job.result)) return { tone: 'muted', label: tx('已取消') };
      if (job.exit_code === 2 && job.result === null) return { tone: 'fail', label: tx('被拒绝') };
      if (job.exit_code === 0 || (job.exit_code === null && job.error === null)) return { tone: 'ok', label: tx('已结束') };
      if (job.exit_code === 1 && job.result !== null) return { tone: 'warn', label: tx('结果不满足') };
      return { tone: 'fail', label: tx('失败') };
  }
}

export function JobBadge({ job }: { job: Pick<Job, 'state' | 'exit_code' | 'result' | 'error'> }) {
  const b = jobBadge(job);
  return <Badge tone={b.tone}>{b.label}</Badge>;
}
