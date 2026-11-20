import type { ReactNode } from 'react';

/** An empty place that says what to do next, in the interface's voice. */
export function EmptyState({ title, body, action, compact = false }: {
  title: string;
  body?: ReactNode;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={compact ? 'empty compact' : 'empty'}>
      <svg className="empty-mark" width="44" height="10" viewBox="0 0 44 10" aria-hidden="true">
        <line x1="2" y1="5" x2="42" y2="5" />
        <circle cx="8" cy="5" r="3.2" />
        <circle cx="22" cy="5" r="3.2" />
        <circle cx="36" cy="5" r="3.2" />
      </svg>
      <p className="empty-title">{title}</p>
      {body ? <p className="empty-body">{body}</p> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}
