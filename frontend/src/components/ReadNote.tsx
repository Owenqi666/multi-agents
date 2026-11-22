import type { ReactNode } from 'react';
import type { ReadState } from '../api/reads';
import { errorText } from '../api/transport';
import { readTime } from './LogBlock';
import { EmptyState } from './EmptyState';
import { getLang, tx } from '../i18n';

/** Wraps a read: while the repository lock is held it keeps the last content with the time
 *  it was read, or says the record can be read once the job ends. */
export function ReadFrame<T>({ read, children, what: label }: { read: ReadState<T>; children: (data: T) => ReactNode; what: string }) {
  // The record's name sits mid-sentence; English wants it in lower case.
  const what = getLang() === 'en' ? label.charAt(0).toLowerCase() + label.slice(1) : label;
  if (read.data === null) {
    if (read.stale) return <EmptyState compact title={tx('作业运行中，结束后可读')} body={tx('{0}和作业抢同一把仓库锁，作业结束后自动读取。', what)} />;
    if (read.error) return <EmptyState compact title={tx('读不到{0}', what)} body={errorText(read.error)} />;
    return <p className="loading">{tx('正在读取{0}…', what)}</p>;
  }
  return (
    <>
      {read.stale && read.readAt ? <p className="stale-note">{tx('作业运行中，下面是 {0} 读到的{1}。', readTime(read.readAt), what)}</p> : null}
      {read.error && !read.stale ? <p className="stale-note problem">{tx('刷新{0}失败：{1}', what, errorText(read.error))}</p> : null}
      {children(read.data)}
    </>
  );
}
