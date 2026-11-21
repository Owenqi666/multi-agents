import type { PlanningAttempt, RoleRequest } from '../api/types';
import { timeText } from '../app/labels';
import { Evidence } from './common';

export function PlanningAttempts({ label, attempts, request }: { label: string; attempts: PlanningAttempt[]; request?: RoleRequest }) {
  return (
    <div className="planning-attempts">
      <h3 className="sub-head">{label}</h3>
      {attempts.length ? (
        <ol className="plan-attempt-list" aria-label={label}>
          {attempts.map(({ request: started, runtime }, index) => {
            const result = runtime?.result as { decision?: string; reason?: string; blocking_issues?: string[] } | null | undefined;
            return (
              <li key={runtime?.number ?? index + 1}>
                <div className="row">
                  <Evidence reportedModel={runtime?.reported_model} reportedEffort={runtime?.reported_effort} request={started.request} />
                  {started.started_at ? <span className="stamp">{timeText(started.started_at)}</span> : null}
                  {runtime?.status ? <span className="stamp">{runtime.status}</span> : null}
                  {runtime?.failure_category ? <code className="stamp">{runtime.failure_category}</code> : null}
                </div>
                {result?.decision === 'ready' ? <p className="muted">复核通过</p> : null}
                {result?.decision === 'needs_revision' ? <p className="muted">复核要求修改</p> : null}
                {result?.reason || runtime?.detail ? <p className="goal-text">{result?.reason ?? runtime?.detail}</p> : null}
                {result?.blocking_issues?.length ? <ul>{result.blocking_issues.map((issue, position) => <li key={position}>{issue}</li>)}</ul> : null}
              </li>
            );
          })}
        </ol>
      ) : request ? <p className="row"><Evidence reportedModel={null} reportedEffort={null} request={request} /> <span className="stamp">还没开始</span></p>
        : <p className="muted">还没开始。</p>}
    </div>
  );
}
