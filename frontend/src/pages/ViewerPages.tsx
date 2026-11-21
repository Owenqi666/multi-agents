import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { HELD, lockBusy, readWithLock, useRead } from '../api/reads';
import { errorText } from '../api/transport';
import type { ArtifactPage, Diff, FrozenFile } from '../api/types';
import { shortId } from '../app/labels';
import { Link, to } from '../app/router';
import { DiffView } from '../components/DiffView';
import { EmptyState } from '../components/EmptyState';
import { LogBlock } from '../components/LogBlock';
import { ReadFrame } from '../components/ReadNote';
import { Page } from './common';

const UNAVAILABLE: Record<string, string> = {
  lock_busy: '作业运行中，结束后可读。',
  not_verified: '候选还没有核验通过，暂时没有 diff。',
  no_parent: '这是第一次实施，没有返工前的候选可比。',
};

/** Task and integration diffs take the repository lock; snapshot diffs do not. */
export function DiffPage({ kind, id, against }: { kind: 'task' | 'integration' | 'snapshot'; id: string; against?: 'parent' }) {
  const read = useRead<Diff>(`diff/${kind}/${id}/${against ?? ''}`, () => api.diff(kind, id, against), { locking: kind !== 'snapshot' });
  const title = against ? '返工增量' : kind === 'snapshot' ? '冻结的审核输入' : '累计改动';
  return (
    <Page kicker={`diff · ${kind} ${shortId(id)}`} title={title} aside={kind !== 'snapshot' ? (
      <div className="seg small" role="radiogroup" aria-label="diff 范围">
        <Link role="radio" aria-checked={!against} className={!against ? 'on' : ''} href={to('diffs', kind, id)}>累计</Link>
        <Link role="radio" aria-checked={against === 'parent'} className={against === 'parent' ? 'on' : ''} href={`${to('diffs', kind, id)}?against=parent`}>返工增量</Link>
      </div>
    ) : null}>
      <ReadFrame read={read} what="diff">
        {(d) => (
          <>
            <p className="stamp">{d.baseline ? `基线 ${shortId(d.baseline, 7)}` : ''}{d.parent_candidate ? ` · 父候选 ${shortId(d.parent_candidate, 7)}` : ''}{d.candidate ? ` → 候选 ${shortId(d.candidate, 7)}` : ''}</p>
            <DiffView diff={d.diff} emptyNote={<EmptyState compact title="没有可看的 diff" body={(d.unavailable_code && UNAVAILABLE[d.unavailable_code]) ?? d.unavailable_reason ?? '这次读取没有返回 diff。'} />} />
          </>
        )}
      </ReadFrame>
    </Page>
  );
}

/** A frozen file from a review snapshot, with the finding's lines marked. */
export function FrozenFilePage({ id, side, path, line, end }: { id: string; side: 'baseline' | 'candidate'; path: string; line: number; end: number }) {
  const read = useRead<FrozenFile>(`file/${id}/${side}/${path}`, () => api.frozenFile(id, side, path), { locking: false });
  useEffect(() => {
    if (!read.data || !line) return;
    document.getElementById(`F${line}`)?.scrollIntoView({ block: 'center' });
  }, [read.data, line]);
  return (
    <Page kicker={`冻结文件 · ${side === 'candidate' ? '候选' : '基线'} · 快照 ${shortId(id)}`} title={<code className="title-path">{path}</code>}>
      <ReadFrame read={read} what="冻结文件">
        {(f) => f.text === null ? <EmptyState compact title="二进制文件" body="冻结的是二进制内容，不显示。" /> : (
          <div className="diff-file">
            <div className="diff-body">
              <table className="diff-table">
                <tbody>
                  {f.text.replace(/\n$/, '').split('\n').map((t, i) => (
                    <tr key={i} id={`F${i + 1}`} className={`dl dl-ctx${line && i + 1 >= line && i + 1 <= Math.max(line, end) ? ' hot' : ''}`}>
                      <td className="no">{i + 1}</td><td className="tx">{t}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </ReadFrame>
    </Page>
  );
}

/** A log by its registered handle, page by page. Logs of locking records are not read while
 *  a job runs; job logs always are. */
export function LogPage({ id, title, locking }: { id: string; title: string; locking: boolean }) {
  const { active, submitting, ready } = useJobs();
  const [pages, setPages] = useState<ArtifactPage[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const limit = 8192;
  const first = useRead<ArtifactPage>(`log/${id}/0`, () => api.artifact(id, 0, limit), { locking });
  const all = first.data && !lockBusy(first.data) ? [first.data, ...pages] : pages;
  const last = all[all.length - 1];
  const held = locking && (!ready || active !== null || submitting !== null);
  const more = async (): Promise<void> => {
    if (!last || last.next_offset === null || held || loadingMore) return;
    const offset = last.next_offset;
    const fetchPage = () => api.artifact(id, offset, limit);
    setProblem(null);
    setLoadingMore(true);
    try {
      const p = await (locking ? readWithLock(fetchPage) : fetchPage());
      if (p === HELD || lockBusy(p)) return;
      setPages((xs) => [...xs, p]);
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setLoadingMore(false);
    }
  };
  const joined: ArtifactPage | null = all.length
    ? { offset: all[0]!.offset, next_offset: last!.next_offset, byte_count: last!.byte_count, sha256: last!.sha256, text: all.map((p) => p.text ?? '').join(''), stream: all[0]!.stream }
    : null;
  return (
    <Page kicker={`日志 · ${shortId(id, 12)}`} title={title}>
      {problem ? <p className="stale-note">{problem}</p> : null}
      <ReadFrame read={first} what="日志">
        {() => (
          <LogBlock title={title} stream={joined?.stream ?? 'stdout'} page={joined}
            readAt={first.readAt} onMore={() => void more()} moreDisabled={held || loadingMore} />
        )}
      </ReadFrame>
    </Page>
  );
}
