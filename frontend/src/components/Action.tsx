import { useState, type ReactNode } from 'react';
import { jobs, useJobs } from '../api/jobs';
import { useReadsInFlight } from '../api/reads';
import { useWorkspaceWritesAllowed } from '../api/workspace';
import { errorText } from '../api/transport';
import type { Job } from '../api/types';
import { actionLabel } from '../app/labels';
import { Link } from '../app/router';

/** A button that submits one action. From the click until its job ends it cannot be pressed
 *  again, and no other action can be submitted while a CLI job is unfinished. When it is
 *  not available the reason is written next to it. */
export function ActionButton({ action, args, label, primary = false, disabled = false, reason, onSubmitted, onDone, confirm }: {
  action: string;
  args: () => Record<string, unknown> | null;
  label?: string;
  primary?: boolean;
  disabled?: boolean;
  /** Why the button cannot be used now; shown next to it when disabled. */
  reason?: ReactNode;
  onSubmitted?: (job: Job) => void;
  onDone?: (job: Job) => void;
  /** Ask before submitting (actions that create something new). */
  confirm?: string;
}) {
  const { active, submitting, settling, ready } = useJobs();
  const writesAllowed = useWorkspaceWritesAllowed();
  const reading = useReadsInFlight() > 0 || settling;
  const [mine, setMine] = useState<string | null>(null);
  const [problem, setProblem] = useState<ReactNode>(null);
  const running = mine !== null && active?.job_id === mine;
  const busyElsewhere = !running && (active !== null || submitting !== null);
  // While the page is still reading (just after a job, or on arrival) its buttons may come from
  // the state before; wait for the reading so nothing is submitted twice.
  const blocked = !ready || disabled || busyElsewhere || running || submitting === action || reading;
  if (!writesAllowed) return null;

  const click = async (): Promise<void> => {
    if (blocked) return;
    const a = args();
    if (!a) return;
    if (confirm && !window.confirm(confirm)) return;
    setProblem(null);
    try {
      const r = await jobs.submit(action, a);
      if ('busy' in r) {
        setProblem(<>已有作业在运行。{r.busy ? <Link href={`/jobs/${encodeURIComponent(r.busy)}`}>查看那个作业</Link> : null}</>);
        return;
      }
      setMine(r.job.job_id);
      onSubmitted?.(r.job);
      const done = await jobs.waitFor(r.job.job_id);
      setMine(null);
      onDone?.(done);
    } catch (e) {
      setMine(null);
      setProblem(errorText(e));
    }
  };

  return (
    <span className="action">
      <button type="button" className={`btn${primary ? ' primary' : ''}${running || submitting === action ? ' busy' : ''}`} disabled={blocked} onClick={() => void click()}>
        {running || submitting === action ? '已提交，等作业结束' : label ?? actionLabel(action)}
      </button>
      {disabled && reason ? <span className="action-why">{reason}</span> : null}
      {!disabled && busyElsewhere && active ? (
        <span className="action-why">
          作业「{actionLabel(active.action)}」还没结束。<Link href={`/jobs/${encodeURIComponent(active.job_id)}`}>查看</Link>
        </span>
      ) : null}
      {problem ? <span className="action-why problem">{problem}</span> : null}
    </span>
  );
}
