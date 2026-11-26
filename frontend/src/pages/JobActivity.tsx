// What a job's executors were last seen doing, which review seats have a report, and what
// can be continued after an interruption. Read from GET /api/jobs/{id}/activity on the
// rhythm of the existing job poll; events are merged by `sequence` within one job, so a
// repeated or late answer never shows an event twice and a reconnect picks up where the
// list stopped.
//
// What can be continued may change without any job of this page ending: the controller of
// an interrupted job lets go of its journal, or another CLI moves a seat on. So an
// interrupted job whose controller is still there is read again every so often, any job is
// read again when the page comes back to the front, and the user can ask for a reading.

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { jobs, useJobs } from '../api/jobs';
import { ApiError, errorText } from '../api/transport';
import { isUnfinished, type ActivityActor, type ActivityEvent, type Job, type JobActivity, type RecoveryAction } from '../api/types';
import { actionLabel, timeText } from '../app/labels';
import { Link, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge, endingText, jobEnding, type BadgeTone } from '../components/Badge';
import { joinList, translated, tx } from '../i18n';
import { RecordLink, Section } from './common';

const KEPT_EVENTS = 500;
const SHOWN_EVENTS = 100;
/** How often an interrupted job whose controller still holds the journal is read again. */
export const CONTROLLER_POLL_MS = 15_000;
/** How long after a reading that failed the next one is tried. */
export const RETRY_MS = 5_000;

export interface ActivityRead {
  data: JobActivity | null;
  /** Every event seen for this job, oldest first, one per sequence number. */
  events: ActivityEvent[];
  error: unknown;
  readAt: string | null;
  /** False while the snapshot is older than the last job that ended: what it offers to
   *  continue may already have been done. */
  current: boolean;
  /** A reading is on its way. */
  reading: boolean;
  reload: () => void;
}

export function useJobActivity(jobId: string, controllerPollMs: number = CONTROLLER_POLL_MS, retryMs: number = RETRY_MS): ActivityRead {
  const { generation } = useJobs();
  const [state, setState] = useState<Pick<ActivityRead, 'data' | 'error' | 'readAt'> & { generation: number }>({ data: null, error: null, readAt: null, generation: -1 });
  const [reading, setReading] = useState(false);
  // Events belong to one job: the sequence numbers of another job mean nothing here.
  const seen = useRef(new Map<number, ActivityEvent>());
  const owner = useRef(jobId);
  if (owner.current !== jobId) { owner.current = jobId; seen.current = new Map(); }
  const flying = useRef(false);
  const again = useRef(false);
  const alive = useRef(true);
  // A reading that failed is tried again by itself: otherwise an ended job, which nothing
  // polls, would keep its error (and its recovery buttons held back) until the user acted.
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const forget = (): void => { if (retry.current) { clearTimeout(retry.current); retry.current = null; } };
  useEffect(() => { alive.current = true; return () => { alive.current = false; forget(); }; }, []);

  const load = useCallback(async (): Promise<void> => {
    // One read at a time. A request made while one is on its way is not dropped: the answer
    // on its way may predate what prompted it (the job ending), so it is asked once more after.
    if (flying.current) { again.current = true; return; }
    flying.current = true;
    forget();
    setReading(true);
    const asked = jobs.getState().generation;
    try {
      const answer = await api.jobActivity(jobId);
      if (!alive.current || owner.current !== jobId) return;
      // Missing keys read as empty. An answer that is not this job's activity counts as none.
      const data: JobActivity | null = answer?.job_id === jobId ? {
        ...answer,
        events: answer.events ?? [], actors: answer.actors ?? [], reviews: answer.reviews ?? [],
        resources: answer.resources ?? [], recovery: answer.recovery ?? [],
        controller_holds_journal: answer.controller_holds_journal ?? null, history_truncated: answer.history_truncated === true,
      } : null;
      if (!data) { setState({ data: null, error: null, readAt: null, generation: asked }); return; }
      for (const event of data.events) seen.current.set(event.sequence, event);
      if (seen.current.size > KEPT_EVENTS) {
        const keep = [...seen.current.keys()].sort((a, b) => a - b).slice(-KEPT_EVENTS);
        seen.current = new Map(keep.map((k) => [k, seen.current.get(k)!]));
      }
      setState({ data, error: null, readAt: new Date().toISOString(), generation: asked });
    } catch (error) {
      // Keep what was read: after a dropped connection the next answer continues the list.
      if (alive.current && owner.current === jobId) {
        setState((s) => ({ ...s, error }));
        // A service that does not have this read at all (404) will not have it next time either.
        if (!(error instanceof ApiError && error.status === 404)) retry.current = setTimeout(() => { retry.current = null; void load(); }, retryMs);
      }
    } finally {
      flying.current = false;
      if (alive.current) {
        if (again.current) { again.current = false; void load(); } else setReading(false);
      }
    }
  }, [jobId, retryMs]);

  // On arrival, and once more whenever any job ends.
  useEffect(() => { void load(); }, [load, generation]);
  // While this job runs, follow the job poll instead of starting another timer.
  useEffect(() => jobs.onPoll((job) => { if (job.job_id === jobId) void load(); }), [jobId, load]);
  // Back at the front: whatever happened meanwhile, read it.
  useEffect(() => {
    const visible = (): void => { if (document.visibilityState === 'visible') void load(); };
    document.addEventListener('visibilitychange', visible);
    return () => document.removeEventListener('visibilitychange', visible);
  }, [load]);
  // Interrupted, and its controller still holds the journal: what can be continued changes
  // when that process lets go, which no job of this page announces. A hidden page waits.
  const held = state.data?.state === 'interrupted' && state.data.controller_holds_journal === true;
  useEffect(() => {
    if (!held) return;
    const timer = setInterval(() => { if (document.visibilityState === 'visible') void load(); }, controllerPollMs);
    return () => clearInterval(timer);
  }, [held, load, controllerPollMs]);

  const events = [...seen.current.values()].sort((a, b) => a.sequence - b.sequence);
  // What was read for another job (this hook reused for a new id) is not this job's activity.
  const data = state.data?.job_id === jobId ? state.data : null;
  return { data, error: state.error, readAt: state.readAt, events, current: state.generation === generation, reading, reload: () => void load() };
}

// Words for the states and events seen so far. Three different fields share these tables
// (an actor's state, a review seat's state, an event's state), each with its own values on
// the service; the tables are a superset and anything not in them is shown as it came.
const STATE_LABEL: Record<string, string> = translated({
  pending: '未开始', ready: '就绪', running: '进行中', succeeded: '完成', passed: '通过', failed: '失败', cancelled: '已取消',
  cancelling: '正在取消', awaiting_evidence: '等待证据', unconfirmed: '未确认', interrupted: '已中断', queued: '排队中', finished: '已结束',
});
const STATE_TONE: Record<string, BadgeTone> = {
  pending: 'muted', running: 'live', succeeded: 'ok', passed: 'ok', failed: 'fail', cancelled: 'muted', cancelling: 'warn', unconfirmed: 'warn',
};
const EVENT_LABEL: Record<string, string> = translated({
  attempt_started: '开始一次尝试', attempt_settled: '这次尝试已结束', session_started: '会话已建立', message: '模型有输出',
  usage: '用量更新', completed: '运行完成', error: '运行报错', process_exited: '进程已退出', cancel_requested: '已请求取消',
  test_command_started: '开始一条测试命令', suite_settled: '测试已结束', provider_wait: '在等本机的提供方配额', provider_admitted: '取得本机的提供方配额',
});

const stateText = (state: string | null | undefined): string => (state ? STATE_LABEL[state] ?? state : '—');
const eventText = (event: string | null | undefined): string => (event ? EVENT_LABEL[event] ?? event : '—');

/** The executor in words. Review seats keep their key as written (reviewer-1, seat-2). */
export function seatText(seat: string | null | undefined): string {
  let m: RegExpExecArray | null;
  if (!seat) return '—';
  if (seat === 'implementation') return tx('实施');
  if (seat === 'tests') return tx('集成测试');
  if (seat === 'planner') return tx('规划者');
  if (seat === 'draft') return tx('草拟任务卡');
  if (seat === 'review') return tx('卡片复核');
  if ((m = /^test-(\d+)$/.exec(seat))) return tx('测试（实施 {0}）', m[1]);
  if ((m = /^advisor-(\d+)$/.exec(seat))) return tx('顾问 {0}', m[1]);
  if (seat.startsWith('rework-')) return tx('集成返工');
  if (seat.startsWith('followup:')) return tx('追问 {0}', seat.slice('followup:'.length));
  return seat;
}

function ActorRow({ actor, job }: { actor: ActivityActor; job: Job }) {
  const ended = !isUnfinished(job);
  const unconfirmed = actor.state === 'unconfirmed';
  const waiting = !ended && actor.last_event === 'provider_wait';
  return (
    <li className="job-actor">
      <div className="job-actor-head">
        <strong>{seatText(actor.seat)}</strong>
        {actor.provider ? <span className="stamp">{actor.provider}</span> : null}
        <span className="stamp">{tx('第 {0} 次', actor.attempt)}</span>
        <Badge tone={waiting ? 'muted' : STATE_TONE[actor.state] ?? 'plain'}>{waiting ? tx('等待配额') : stateText(actor.state)}</Badge>
        <RecordLink kind={actor.resource.kind} id={actor.resource.id} />
      </div>
      <p className="job-actor-seen">
        {unconfirmed
          ? (job.state === 'interrupted'
            ? tx('中断前最后观测于 {0}，当时{1}（{2}）。之后的结果没有确认，打开相关记录看当前状态。', timeText(actor.observed_at), stateText(actor.last_observed_state), eventText(actor.last_event))
            : tx('作业结束前最后观测于 {0}，当时{1}（{2}）。之后的结果没有确认，打开相关记录看当前状态。', timeText(actor.observed_at), stateText(actor.last_observed_state), eventText(actor.last_event)))
          : actor.observed_at ? tx('最近观测于 {0}：{1}', timeText(actor.observed_at), eventText(actor.last_event)) : tx('没有观测记录。')}
      </p>
    </li>
  );
}

function recoveryLabel(action: RecoveryAction): string {
  const args = action.args ?? {};
  const seats = Array.isArray(args.seat_keys) ? joinList(args.seat_keys) : null;
  if (seats && args.recover === true) return tx('接管并补跑中断的席位（{0}）', seats);
  if (seats) return tx('只补跑缺失的席位（{0}）', seats);
  return actionLabel(action.action);
}

export function JobActivityView({ job, activity }: { job: Job; activity: ActivityRead }) {
  const { active } = useJobs();
  const [outcome, setOutcome] = useState<Job | null>(null);
  const { data, events, error, current, reading, reload } = activity;
  if (!data) {
    // An older service, or the recorded demo, has no activity for this job: the receipt stands alone.
    if (!error || (error instanceof ApiError && error.status === 404)) return null;
    return (
      <Section title={tx('作业活动')}>
        <p className="stale-note problem">{tx('读不到作业活动：{0}', errorText(error))}</p>
        <p className="row">
          <button type="button" className="btn small quiet" disabled={reading} onClick={reload}>{tx('重新读取')}</button>
          <span className="stamp" role="status">{reading ? tx('正在读取…') : tx('过一会儿会自动再读。')}</span>
        </p>
      </Section>
    );
  }
  const ended = !isUnfinished(job);
  const shown = events.slice(-SHOWN_EVENTS).reverse();
  const needsAttention = ['interrupted', 'refused', 'unmet', 'failed', 'errored'].includes(jobEnding(job));
  return (
    <>
      {error ? <p className="stale-note problem">{tx('刷新作业活动失败，下面是 {0} 读到的：{1}', timeText(activity.readAt), errorText(error))}</p> : null}
      {data.actors.length ? (
        <Section title={tx('执行者')} note={tx('每位执行者最近一次被观测到的状态。服务不给出剩余时间。')}>
          <ul className="job-actors">
            {data.actors.map((actor) => <ActorRow key={`${actor.resource.kind}/${actor.resource.id}/${actor.seat}/${actor.attempt}`} actor={actor} job={job} />)}
          </ul>
        </Section>
      ) : null}
      {data.reviews.length ? (
        <Section title={tx('审核席位')} note={tx('已有报告的席位保留原样；补跑沿用同一份冻结快照。')}>
          {data.reviews.map((review) => (
            <div className="round" key={review.round_id}>
              <div className="round-head">
                <RecordLink kind="review" id={review.round_id} />
                <RecordLink kind="snapshot" id={review.snapshot_id} />
              </div>
              <ul className="seat-list">
                {(review.seats ?? []).map((seat) => {
                  // Still recorded as running although no job runs any more: the attempt was cut off.
                  const cutOff = seat.state === 'running' && ended && !active;
                  return (
                    <li key={seat.seat}>
                      <code>{seat.seat}</code>
                      <span className="stamp">{seat.provider}</span>
                      <span className="stamp">{seat.attempt === null ? tx('还没有尝试') : tx('第 {0} 次', seat.attempt)}</span>
                      <Badge tone={cutOff ? 'warn' : STATE_TONE[seat.state] ?? 'plain'}>
                        {seat.state === 'succeeded' ? tx('报告有效') : cutOff ? tx('中断时还在运行，没有报告') : stateText(seat.state)}
                      </Badge>
                      {seat.retryable ? <span className="stamp">{tx('可以补跑')}</span> : null}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </Section>
      ) : null}
      {ended ? (
        <Section title={tx('可以继续的动作')} note={tx('点击后按现有的作业流程提交，参数原样用服务给出的。')}>
          {data.recovery.length ? (
            <ul className="recovery-list">
              {data.recovery.map((action) => (
                <li key={`${action.action}/${action.resource.kind}/${action.resource.id}`}>
                  <RecordLink kind={action.resource.kind} id={action.resource.id} />
                  <ActionButton action={action.action} label={recoveryLabel(action)} args={() => action.args ?? {}}
                    disabled={!current} reason={tx('正在重新读取可以继续的动作…')}
                    onSubmitted={() => setOutcome(null)} onDone={(done) => { setOutcome(done); reload(); }} onRejected={reload} />
                </li>
              ))}
            </ul>
          ) : active && active.job_id !== job.job_id ? (
            <p className="muted">{tx('有作业在运行，结束后这里会重新读取可以继续的动作。')}</p>
          ) : job.state === 'interrupted' && data.controller_holds_journal === true ? (
            <p className="muted">{tx('这个作业启动的 CLI 还在运行，等它退出后这里会给出可以继续的动作。这一页每隔十几秒重新读取一次。')}</p>
          ) : needsAttention ? (
            <p className="muted">{tx('这个作业没有可以直接继续的动作。打开相关记录，用那一页现有的恢复或重试操作继续。')}</p>
          ) : (
            <p className="muted">{tx('没有需要继续的动作。')}</p>
          )}
          {outcome ? (
            <p className={jobEnding(outcome) === 'done' ? 'stale-note' : 'stale-note problem'} role="status">
              {endingText(actionLabel(outcome.action), outcome)}
              {/* Turned down at execution: what this page offered was read before something else moved on. */}
              {jobEnding(outcome) === 'refused' ? ` ${tx('这里读到的可以继续的动作已经过期，上面是重新读取的。')}` : ''}
              {' '}<Link href={to('jobs', outcome.job_id)}>{tx('查看那个作业')}</Link>
            </p>
          ) : null}
          <p className="row">
            <button type="button" className="btn small quiet" disabled={reading} onClick={reload}>{tx('重新读取')}</button>
            <span className="stamp" role="status">{reading ? tx('正在读取…') : activity.readAt ? tx('读于 {0}', timeText(activity.readAt)) : ''}</span>
          </p>
        </Section>
      ) : null}
      <Section title={tx('最近的事件')} note={data.history_truncated ? tx('一次读取最多返回最近 50 条，更早的记录还在服务那边；这里另外留着本页已经读到的。') : undefined}>
        {shown.length ? (
          <ol className="event-list" aria-label={tx('最近的事件')} tabIndex={0}>
            {shown.map((event) => (
              <li key={event.sequence}>
                <span className="stamp">{timeText(event.at)}</span>
                <span className="event-seat">{seatText(event.seat)}{event.provider ? ` · ${event.provider}` : ''}</span>
                <span>{eventText(event.event)}</span>
                {event.state ? <span className="stamp">{stateText(event.state)}</span> : null}
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">{data.controller_holds_journal === null ? tx('这个作业没有留下活动记录（早期的作业不记录）。上面的席位和相关记录来自已保存的结果。') : tx('还没有观测到事件。')}</p>
        )}
      </Section>
    </>
  );
}
