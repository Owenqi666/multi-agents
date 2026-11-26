// 需要我处理: the inbox of the whole repository, read from GET /api/attention. Every row
// links to the page that owns the action; the link is the one the service returned.
//
// The service answers `complete: false` whenever it cannot check the records on the spot:
// while a CLI job holds the worktree, between a job's registration and its end, or while
// the service has its read gate closed. The answer then holds only what the stored records
// confirm. Rows known from the last full reading stay on screen, marked as waiting for a
// refresh (they say nothing about what can be done now), and the list is read again when a
// job ends and, while it stays partial, every so often.

import { useEffect, useMemo, useRef } from 'react';
import { api } from '../api/client';
import { jobs, useJobs } from '../api/jobs';
import { HELD, readWithLock, recall, remember, useRead } from '../api/reads';
import { ApiError, errorText } from '../api/transport';
import type { Attention, AttentionItem } from '../api/types';
import { KIND_LABEL } from '../app/labels';
import { Link } from '../app/router';
import { Badge } from '../components/Badge';
import { readTime } from '../components/LogBlock';
import { translated, tx } from '../i18n';

const FULL = 'attention/complete';
/** How often a partial list is asked again while no job of this page explains it. */
export const PARTIAL_POLL_MS = 10_000;

/** What each reason asks of the user. An unknown reason shows the service's own sentence. */
export const REASON_LABEL: Record<string, string> = translated({
  confirm_goal: '确认顾问整理的目标',
  consultation_failed: '需求咨询失败，重新咨询或直接确认原文',
  plan_goal: '为已确认的目标生成计划',
  retry_project_plan: '计划没有生成，重新生成',
  choose_stage: '可以领取这个阶段',
  dispatch_failed: '阶段分派被拒绝，修订计划',
  planning_attention: '规划没有完成，查看并恢复',
  planning_question: '回答规划提出的阻塞问题',
  implementation_attention: '实施停下了，查看记录和下一步',
  task_test_failed: '测试没有通过，查看证据',
  test_task: '候选已核验，运行任务测试',
  review_task: '测试已通过，选审核席位开始任务审核',
  integrate_stage: '阶段审核已通过，创建集成',
  integration_conflict: '合并有冲突，查看冲突文件',
  rebuild_integration: '目标分支移动过，重建集成',
  inspect_candidate: '候选没有核验通过，查看记录',
  test_integration: '运行集成测试',
  integration_test_failed: '集成测试没有通过，查看后重试',
  inspect_tests: '测试证据没有核验通过，查看记录',
  review_integration: '选审核席位，开始集成审核',
  review_incomplete: '审核没有完成，补跑缺失的席位',
  review_decision: '查看审核意见，决定交叉确认或返工',
  accept_integration: '测试和审核都通过，可以接受',
});

/** Reasons whose `detail` carries something of the record itself (the stage's goal, the
 *  question asked, the refusal, the phase reached). The others come with a fixed sentence
 *  that only repeats the reason. */
const TELLING = new Set(['choose_stage', 'planning_question', 'dispatch_failed', 'planning_attention', 'implementation_attention', 'task_test_failed']);

/** One plan can offer several stages under the same id, so the link is part of the key. */
export const attentionKey = (item: AttentionItem): string => `${item.kind}/${item.id}/${item.reason}/${item.href}`;

export interface AttentionRow extends AttentionItem {
  /** Known from an earlier full reading; the running job keeps the service from confirming it. */
  pending: boolean;
}

export interface AttentionState {
  rows: AttentionRow[];
  /** Nothing has been read yet. */
  loading: boolean;
  /** The last answer was a partial one: the service could not check everything just then. */
  partial: boolean;
  /** When the last full list was read, if rows are kept from it. */
  fullAt: string | null;
  error: unknown;
  /** The service has no inbox (an older service, or the recorded demo). */
  unavailable: boolean;
  reload: () => void;
}

async function fetchAttention(): Promise<Attention> {
  // With no job running this read takes the repository lock on the service, so it waits its
  // turn with the other locking reads. With a job running the service answers at once.
  const held = jobs.idle ? await readWithLock(() => api.attention()) : HELD;
  const answer = held === HELD ? await api.attention() : held;
  // Missing keys read as empty; only an explicit `complete: false` makes the list a partial one.
  const value: Attention = { items: Array.isArray(answer.items) ? answer.items : [], complete: answer.complete !== false };
  if (value.complete) remember(FULL, value.items);
  return value;
}

export function useAttention(partialPollMs: number = PARTIAL_POLL_MS): AttentionState {
  const read = useRead<Attention>('attention', fetchAttention, { locking: false, pollWhileJob: true });
  const data = read.data;
  const { active, submitting } = useJobs();
  // A partial answer with no job of this page behind it (the service closed its read gate, or
  // a job this page has not seen yet): nothing would prompt another reading, so ask again
  // after a while. A hidden page waits until it is looked at.
  const waiting = data !== null && !data.complete && active === null && submitting === null;
  const reload = useRef(read.reload);
  reload.current = read.reload;
  // A reading on its way is left alone: asking again would throw its answer away, and a
  // full reading that takes longer than the interval would then never arrive.
  const loading = useRef(read.loading);
  loading.current = read.loading;
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => { if (document.visibilityState === 'visible' && !loading.current) reload.current(); }, partialPollMs);
    return () => clearInterval(timer);
  }, [waiting, partialPollMs]);
  return useMemo(() => {
    const full = recall<AttentionItem[]>(FULL);
    const unavailable = read.error instanceof ApiError && read.error.status === 404;
    const rows: AttentionRow[] = (data?.items ?? []).map((item) => ({ ...item, pending: false }));
    let fullAt: string | null = null;
    if (!data?.complete) {
      // A partial answer (or none yet): rows of the last full reading that it does not repeat stay, marked.
      const seen = new Set(rows.map(attentionKey));
      const kept = (full?.data ?? []).filter((item) => !seen.has(attentionKey(item))).map((item) => ({ ...item, pending: true }));
      if (kept.length) fullAt = full!.readAt;
      rows.push(...kept);
    }
    return {
      rows, fullAt, unavailable,
      loading: data === null && !read.error,
      partial: data !== null && !data.complete,
      error: unavailable ? null : read.error,
      reload: read.reload,
    };
  }, [data, read.error, read.reload, read.readAt]);
}

export function AttentionPanel({ attention }: { attention: AttentionState }) {
  const { rows, loading, partial, fullAt, error, unavailable, reload } = attention;
  if (unavailable) return null;
  return (
    <section className="home-card attention" aria-label={tx('需要我处理')}>
      <div className="attention-head">
        <h2>{tx('需要我处理')}</h2>
        {rows.length ? <span className="stamp">{tx('共 {0} 项', rows.length)}</span> : null}
        <button type="button" className="btn small quiet" onClick={reload}>{tx('刷新待办')}</button>
      </div>
      {partial ? (
        <p className="stale-note">
          {tx('这次只读到一部分待办：服务当时没法核对现场记录，只给出了已有记录能确认的。稍后会重新读取。')}
          {fullAt ? tx('标着「待刷新」的是 {0} 读到的，现在还要不要处理、能不能操作，要等重新读取。', readTime(fullAt)) : null}
        </p>
      ) : null}
      {error ? <p className="stale-note problem">{tx('读不到待办：{0}', errorText(error))}</p> : null}
      {rows.length ? (
        <ul className="attention-list">
          {rows.map((row) => (
            <li key={attentionKey(row)} className={row.pending ? 'attention-row pending' : 'attention-row'}>
              <div className="attention-what">
                <span className="attention-reason">{REASON_LABEL[row.reason] ?? row.detail}</span>
                <span className="stamp">{KIND_LABEL[row.kind] ?? row.kind}</span>
                {row.pending ? <Badge tone="warn">{tx('待刷新')}</Badge> : null}
              </div>
              <Link className="attention-label" href={row.href}>{row.label}</Link>
              {row.detail && row.detail !== row.label && TELLING.has(row.reason) ? <span className="next-detail">{row.detail}</span> : null}
            </li>
          ))}
        </ul>
      ) : loading ? (
        <p className="loading">{tx('正在读取待办…')}</p>
      ) : error ? null : partial ? (
        <p>{tx('这次没有读到待办，但读取不完整，不代表都处理完了。稍后会重新读取。')}</p>
      ) : (
        <p>{tx('现在没有待办。')}</p>
      )}
    </section>
  );
}
