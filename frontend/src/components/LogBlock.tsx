import type { ReactNode } from 'react';
import type { ArtifactPage } from '../api/types';
import { EmptyState } from './EmptyState';

const bytes = (n: number | null | undefined) => (n === null || n === undefined ? '—' : n.toLocaleString('en-US'));

export function readTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

/** One page of a log, shown as plain text. A lock_busy page keeps the last text read and
 *  says when it was read; with nothing read yet it says the log is readable after the job. */
export function LogBlock({ title, coords, stream, page, previous, readAt, onMore, footer }: {
  title: string;
  coords?: string;
  stream: 'stdout' | 'stderr';
  page: (ArtifactPage & { lock_busy?: boolean }) | null;
  previous?: ArtifactPage | null;
  readAt?: string | null;
  onMore?: () => void;
  footer?: ReactNode;
}) {
  const busy = page?.lock_busy === true || page?.text === null;
  const shown = busy ? previous ?? null : page;
  return (
    <figure className={`log log-${stream}`}>
      <figcaption className="log-head">
        <span className="log-title">{title}</span>
        <span className="log-stream">{stream}</span>
        {coords ? <span className="log-coords">{coords}</span> : null}
        {shown ? (
          <span className="log-range">
            字节 {bytes(shown.offset)}–{bytes(shown.offset + new TextEncoder().encode(shown.text ?? '').length)} / 共 {bytes(shown.byte_count)}
          </span>
        ) : null}
      </figcaption>
      {busy && shown && readAt ? <p className="log-stale">作业运行中，下面是 {readTime(readAt)} 读到的内容</p> : null}
      {shown ? (
        <pre className="log-body">{shown.text}</pre>
      ) : (
        <EmptyState compact title="作业运行中，结束后可读" body="这份日志和作业抢同一把仓库锁，作业结束后自动读取。" />
      )}
      {shown && shown.next_offset !== null && !busy ? (
        <div className="log-more">
          <button type="button" className="btn small" onClick={onMore}>读下一页</button>
          {shown.sha256 ? <span className="log-hash">sha256 {shown.sha256.slice(0, 12)}</span> : null}
        </div>
      ) : null}
      {footer}
    </figure>
  );
}
