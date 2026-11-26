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

/** How a job ended. There is no general "the action succeeded" field, so the pieces are read
 *  one by one: the state, the cancellation, the error and its kind, the exit code and the
 *  receipt.
 *  - refused, failed: the service's own classification of the error (`error_kind`). Both
 *    can come with exit code 2: a request that was out of date is refused, a session that
 *    gave no answer failed;
 *  - errored: there is an error and the service did not say which kind (an older service),
 *    or a non-zero exit code with neither an error nor a receipt. No judgement is made;
 *  - done: no error, and exit code 0 or none (an environment check);
 *  - unmet: no error, exit code 1 with a receipt. It ran to its end and the result is not a
 *    success: a review that asks for rework, failed tests, but also a planning or a rework
 *    that did not work out. Which of those is in the records. */
export type JobEnding = 'unfinished' | 'interrupted' | 'cancelled' | 'refused' | 'failed' | 'errored' | 'done' | 'unmet';

type Ended = Pick<Job, 'state' | 'exit_code' | 'result' | 'error'> & Pick<Partial<Job>, 'error_kind'>;

export function jobEnding(job: Ended): JobEnding {
  if (job.state === 'interrupted') return 'interrupted';
  if (job.state !== 'finished') return 'unfinished';
  // A cancellation shows as the kind of the error, as exit code 130, or in the receipt of an
  // action that ends its cancellation with exit code 1.
  if (job.error_kind === 'cancelled' || job.exit_code === 130 || cancelledInResult(job.result)) return 'cancelled';
  if (job.error) {
    if (job.error_kind === 'refused') return 'refused';
    if (job.error_kind === 'failed') return 'failed';
    if (job.error_kind === 'interrupted') return 'interrupted';
    return 'errored';
  }
  if (job.exit_code === 0 || job.exit_code === null || job.exit_code === undefined) return 'done';
  if (job.exit_code === 1 && job.result !== null) return 'unmet';
  return 'errored';
}

/** Job state in words. A finished job is judged by its exit code together with the domain
 *  state read back afterwards; error being null does not mean success. */
export function jobBadge(job: Ended): JobBadgeInfo {
  switch (job.state) {
    case 'queued':
      return { tone: 'muted', label: tx('排队中') };
    case 'running':
      return { tone: 'live', label: tx('运行中') };
    case 'cancelling':
      return { tone: 'warn', label: tx('已请求取消') };
    default:
      switch (jobEnding(job)) {
        case 'interrupted': return { tone: 'warn', label: tx('已中断') };
        case 'cancelled': return { tone: 'muted', label: tx('已取消') };
        case 'refused': return { tone: 'fail', label: tx('被拒绝') };
        case 'done': return { tone: 'ok', label: tx('已结束') };
        case 'unmet': return { tone: 'warn', label: tx('结果不满足') };
        case 'failed': return { tone: 'fail', label: tx('失败') };
        default: return { tone: 'fail', label: tx('有错误') };
      }
  }
}

/** One sentence on how a submitted job ended, for the place it was submitted from. `what`
 *  names the action; the caller adds the link to the job or to the record that holds the result. */
export function endingText(what: string, job: Ended): string {
  switch (jobEnding(job)) {
    case 'done': return tx('「{0}」已结束。', what);
    case 'refused': return tx('「{0}」被服务拒绝：{1}', what, job.error?.message ?? tx('退出码 {0}', job.exit_code ?? '—'));
    case 'unmet': return tx('「{0}」跑完了，但结果不是成功（退出码 {1}）。原因在对应的记录里。', what, job.exit_code ?? '—');
    case 'cancelled': return tx('「{0}」已取消。', what);
    case 'interrupted': return tx('「{0}」被中断，没有观测到结果。', what);
    case 'unfinished': return tx('「{0}」还没结束。', what);
    case 'failed': return job.error ? tx('「{0}」失败：{1}', what, job.error.message) : tx('「{0}」失败，退出码 {1}。', what, job.exit_code ?? '—');
    // Not classified by the service: said as it is, without calling it a refusal or a failure.
    default: return job.error ? tx('「{0}」结束时报告了错误：{1}', what, job.error.message) : tx('「{0}」没有成功结束，退出码 {1}。', what, job.exit_code ?? '—');
  }
}

export function JobBadge({ job }: { job: Ended }) {
  const b = jobBadge(job);
  return <Badge tone={b.tone}>{b.label}</Badge>;
}
