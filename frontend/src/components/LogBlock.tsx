import type { ReactNode } from 'react';
import type { ArtifactPage } from '../api/types';
import { EmptyState } from './EmptyState';
import { getLang, tx } from '../i18n';

const bytes = (n: number | null | undefined) => (n === null || n === undefined ? '—' : n.toLocaleString('en-US'));

export function readTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString(getLang() === 'en' ? 'en-GB' : 'zh-CN', { hour12: false });
}

/** One page of a log, shown as plain text. A lock_busy page keeps the last text read and
 *  says when it was read; with nothing read yet it says the log is readable after the job. */
export function LogBlock({ title, coords, stream, page, previous, readAt, onMore, moreDisabled, fullDigest, footer }: {
  title: string;
  coords?: string;
  stream: string;
  page: (ArtifactPage & { lock_busy?: boolean }) | null;
  previous?: ArtifactPage | null;
  readAt?: string | null;
  onMore?: () => void;
  moreDisabled?: boolean;
  fullDigest?: boolean;
  footer?: ReactNode;
}) {
  const busy = page?.lock_busy === true || page?.text === null;
  const shown = busy ? previous ?? null : page;
  return (
    <figure className={`log${stream === 'stderr' ? ' log-stderr' : ''}`}>
      <figcaption className="log-head">
        <span className="log-title">{title}</span>
        <span className="log-stream">{stream}</span>
        {coords ? <span className="log-coords">{coords}</span> : null}
        {shown ? (
          <span className="log-range">
            {tx('字节 {0}–{1} / 共 {2}', bytes(shown.offset), bytes(shown.next_offset ?? shown.byte_count ?? (shown.offset + new TextEncoder().encode(shown.text ?? '').length)), bytes(shown.byte_count))}
          </span>
        ) : null}
      </figcaption>
      {busy && shown && readAt ? <p className="log-stale">{tx('作业运行中，下面是 {0} 读到的内容', readTime(readAt))}</p> : null}
      {shown ? (
        <pre className="log-body">{shown.text}</pre>
      ) : (
        <EmptyState compact title={tx('作业运行中，结束后可读')} body={tx('这份日志和作业抢同一把仓库锁，作业结束后自动读取。')} />
      )}
      {shown && !busy && (shown.next_offset !== null || (fullDigest && shown.sha256)) ? (
        <div className="log-more">
          {shown.next_offset !== null ? <button type="button" className="btn small" disabled={moreDisabled} onClick={onMore}>{tx('读下一页')}</button> : null}
          {shown.sha256 ? <span className="log-hash task-data">sha256 {fullDigest ? shown.sha256 : shown.sha256.slice(0, 12)}</span> : null}
        </div>
      ) : null}
      {footer}
    </figure>
  );
}
