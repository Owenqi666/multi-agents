import { useRef, useState, type ReactNode } from 'react';
import { jobs, useJobs } from '../api/jobs';
import { useReadsInFlight } from '../api/reads';
import { useWorkspaceWritesAllowed } from '../api/workspace';
import { ApiError, errorText } from '../api/transport';
import type { Job } from '../api/types';
import { actionLabel } from '../app/labels';
import { Link } from '../app/router';
import { tx } from '../i18n';

/** A button that submits one action. From the click until its job ends it cannot be pressed
 *  again, and no other action can be submitted while a CLI job is unfinished. When it is
 *  not available the reason is written next to it. */
export function ActionButton({ action, args, label, primary = false, disabled = false, reason, onSubmitted, onDone, onRejected, confirm }: {
  action: string;
  args: () => Record<string, unknown> | null;
  label?: string;
  primary?: boolean;
  disabled?: boolean;
  /** Why the button cannot be used now; shown next to it when disabled. */
  reason?: ReactNode;
  onSubmitted?: (job: Job) => void;
  onDone?: (job: Job) => void;
  /** The submission was not accepted (another job holds the repository, or the service
   *  found the request wrong): no job exists. A good moment to read the page's data again. */
  onRejected?: (error?: unknown) => void;
  /** Ask before submitting (actions that create something new). */
  confirm?: string;
}) {
  const { active, submitting, settling, ready } = useJobs();
  const writesAllowed = useWorkspaceWritesAllowed();
  const reading = useReadsInFlight() > 0 || settling;
  const [mine, setMine] = useState<string | null>(null);
  const [problem, setProblem] = useState<ReactNode>(null);
  // From the click until the service has answered the submission, this button sends nothing
  // more. The job center already holds CLI actions back; an environment check has no such
  // guard, and a second click would only earn a 409.
  const sending = useRef(false);
  const [sent, setSent] = useState(false);
  const running = mine !== null && active?.job_id === mine;
  const busyElsewhere = !running && (active !== null || submitting !== null);
  // While the page is still reading (just after a job, or on arrival) its buttons may come from
  // the state before; wait for the reading so nothing is submitted twice.
  const blocked = !ready || disabled || busyElsewhere || running || submitting === action || reading || sent;
  if (!writesAllowed) return null;

  const click = async (): Promise<void> => {
    if (blocked || sending.current) return;
    const a = args();
    if (!a) return;
    if (confirm && !window.confirm(confirm)) return;
    setProblem(null);
    sending.current = true;
    setSent(true);
    try {
      const r = await jobs.submit(action, a).finally(() => { sending.current = false; setSent(false); });
      if ('busy' in r) {
        setProblem(<>{tx('已有作业在运行，这次没有提交。等它结束后再试。')}{r.busy ? <Link href={`/jobs/${encodeURIComponent(r.busy)}`}>{tx('查看那个作业')}</Link> : null}</>);
        onRejected?.();
        return;
      }
      setMine(r.job.job_id);
      onSubmitted?.(r.job);
      const done = await jobs.waitFor(r.job.job_id);
      setMine(null);
      onDone?.(done);
    } catch (e) {
      setMine(null);
      // A request the service found wrong, or cannot take in the record's present state, says
      // why in its own words (a seat that moved on, a planning budget that is used up); the
      // general sentence alone would not help.
      setProblem(e instanceof ApiError && (e.code === 'invalid_request' || e.code === 'job_unavailable') && e.message ? `${errorText(e)} ${e.message}` : errorText(e));
      onRejected?.(e);
    }
  };

  return (
    <span className="action">
      <button type="button" className={`btn${primary ? ' primary' : ''}${running || submitting === action ? ' busy' : ''}`} disabled={blocked} onClick={() => void click()}>
        {running || submitting === action ? tx('已提交，等作业结束') : label ?? actionLabel(action)}
      </button>
      {disabled && reason ? <span className="action-why">{reason}</span> : null}
      {!disabled && busyElsewhere && active ? (
        <span className="action-why">
          {tx('作业「{0}」还没结束。', actionLabel(active.action))}<Link href={`/jobs/${encodeURIComponent(active.job_id)}`}>{tx('查看')}</Link>
        </span>
      ) : null}
      {problem ? <span className="action-why problem" role="alert">{problem}</span> : null}
    </span>
  );
}
