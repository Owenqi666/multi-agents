import type { ReactNode } from 'react';
import type { ReadState } from '../api/reads';
import { errorText } from '../api/transport';
import { readTime } from './LogBlock';
import { EmptyState } from './EmptyState';

/** Wraps a read: while the repository lock is held it keeps the last content with the time
 *  it was read, or says the record can be read once the job ends. */
export function ReadFrame<T>({ read, children, what }: { read: ReadState<T>; children: (data: T) => ReactNode; what: string }) {
  if (read.data === null) {
    if (read.stale) return <EmptyState compact title="作业运行中，结束后可读" body={`${what}和作业抢同一把仓库锁，作业结束后自动读取。`} />;
    if (read.error) return <EmptyState compact title={`读不到${what}`} body={errorText(read.error)} />;
    return <p className="loading">正在读取{what}…</p>;
  }
  return (
    <>
      {read.stale && read.readAt ? <p className="stale-note">作业运行中，下面是 {readTime(read.readAt)} 读到的{what}。</p> : null}
      {read.error && !read.stale ? <p className="stale-note problem">刷新{what}失败：{errorText(read.error)}</p> : null}
      {children(read.data)}
    </>
  );
}
