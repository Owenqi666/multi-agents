import type { ReactNode } from 'react';
import { api, isLockingKind } from '../api/client';
import { useRead } from '../api/reads';
import type { Query } from '../api/transport';
import type { NextAction, Resource, RoleRequest } from '../api/types';
import { KIND_LABEL, nextText, shortId } from '../app/labels';
import { Link, to } from '../app/router';
import { Requested, Unconfirmed } from '../components/Badge';
import { reportedFull } from '../stage/models';

export function Page({ kicker, title, aside, children }: { kicker?: ReactNode; title: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <article className="page-body">
      <header className="page-head">
        <div>
          {kicker ? <p className="kicker">{kicker}</p> : null}
          <h1>{title}</h1>
        </div>
        {aside ? <div className="page-aside">{aside}</div> : null}
      </header>
      {children}
    </article>
  );
}

export function Section({ title, note, children, id }: { title: string; note?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="section" id={id}>
      <div className="section-head">
        <h2>{title}</h2>
        {note ? <p className="section-note">{note}</p> : null}
      </div>
      <div className="section-body">{children}</div>
    </section>
  );
}

export function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="facts-grid">
      {rows.map(([k, v]) => (
        <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
      ))}
    </dl>
  );
}

const ROUTE_OF: Record<string, string> = {
  project: 'projects', consultation: 'consultations', goal: 'goals', 'project-plan': 'plans', stage: 'stages',
  task: 'tasks', integration: 'integrations', review: 'reviews', 'task-review': 'reviews',
};

/** A link to a record by kind and opaque id. */
export function RecordLink({ kind, id, label }: { kind: string; id: string; label?: ReactNode }) {
  const base = ROUTE_OF[kind];
  const text = label ?? <>{KIND_LABEL[kind] ?? kind} <code>{shortId(id)}</code></>;
  return base ? <Link href={to(base, id)}>{text}</Link> : <span>{text}</span>;
}

/** Model evidence: the reported model and effort, or 未确认; the request marked 请求. */
export function Evidence({ reportedModel, reportedEffort, request }: {
  reportedModel: string | null | undefined;
  reportedEffort: string | null | undefined;
  request?: RoleRequest | { model?: string | null; effort?: string | null } | null;
}) {
  const rm = reportedModel ?? null;
  const re = reportedEffort ?? null;
  return (
    <span className="evidence-line">
      {rm === null ? <Unconfirmed what="模型" /> : <span className="ev-model">{reportedFull(rm)}</span>}
      <span className="ev-effort">档位 {re === null ? <Unconfirmed what="档位" /> : re}</span>
      {request?.model ? <Requested>{request.model}{request.effort ? ` · ${request.effort}` : ''}</Requested> : null}
    </span>
  );
}

/** Read one resource with the lock rules applied by kind. */
export function useResource<T>(kind: string, id: string | null | undefined, query?: Query, opts: { pollWhileJob?: boolean; enabled?: boolean } = {}) {
  const key = id ? `${kind}/${id}?${JSON.stringify(query ?? {})}` : null;
  return useRead<Resource<T>>(key, () => api.resource<T>(kind, id!, query), {
    locking: isLockingKind(kind),
    pollWhileJob: opts.pollWhileJob ?? !isLockingKind(kind),
    enabled: opts.enabled,
  });
}

/** Plain-text block for structured values the page does not lay out field by field. */
export function Raw({ value, label = '原始内容' }: { value: unknown; label?: string }) {
  return (
    <details className="raw">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

export const pluralSeat = (n: number): string => (n > 1 ? '双席' : '单席');

/** The next step the readback offers, in words, with the backend's sentence kept small. */
export function NextNote({ next, fallback }: { next: NextAction | null | undefined; fallback?: string }) {
  const text = nextText(next) ?? fallback ?? null;
  if (!text) return null;
  return (
    <span className="next-note">
      {text}
      {next?.detail && next.detail !== text ? <span className="next-detail">{next.detail}</span> : null}
    </span>
  );
}
