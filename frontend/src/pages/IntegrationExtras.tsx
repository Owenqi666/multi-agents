// Two blocks of the integration page that are read on their own: whether the blocking
// findings can be reworked (and by whom), and the delivery summary of an accepted candidate.

import { useRef, useState } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import { ApiError, errorText } from '../api/transport';
import type { Delivery, IntegrationRecord, Job, ReworkEligibility, ReworkOwner } from '../api/types';
import { actionLabel, timeText } from '../app/labels';
import { Link, navigate, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge, endingText, jobEnding } from '../components/Badge';
import { readTime } from '../components/LogBlock';
import { getLang, tx } from '../i18n';
import { Facts } from './common';

function Owner({ owner }: { owner: ReworkOwner }) {
  return <Link href={to('tasks', owner.task_run_id)}>{owner.task_id}</Link>;
}

/** 返工资格: read before offering the rework. The reason is the one the command itself would
 *  give; the button uses the integration and round of this very answer. The service reads
 *  it under the repository lock, so during a job the last answer stays with its time. */
export function ReworkBlock({ it, enabled }: { it: IntegrationRecord; enabled: boolean }) {
  const id = it.integration_id;
  const read = useRead<ReworkEligibility>(enabled ? `rework-eligibility/${id}` : null, () => api.reworkEligibility(id), { locking: true });
  const [refused, setRefused] = useState<Job | null>(null);
  const { active, submitting } = useJobs();
  if (!enabled) return null;
  // The reading is held back while a job runs. It can also come back empty-handed with no
  // job of this page behind it (the service was busy, or the reading failed): then the page
  // says so and offers to read again, instead of pointing at a job that is not there.
  const jobRunning = active !== null || submitting !== null;
  const again = <button type="button" className="btn small quiet" onClick={read.reload}>{tx('重新读取')}</button>;
  const e = read.data?.integration_id === id ? read.data : null;
  if (!e) {
    // A service without this check (an older one, or the recorded demo): nothing to add.
    if (read.error instanceof ApiError && read.error.code === 'not_found') return null;
    if (read.error) return <p className="stale-note problem">{tx('读不到返工资格：{0}', errorText(read.error))} {again}</p>;
    if (read.stale) return jobRunning ? <p className="muted">{tx('作业运行中，返工资格等作业结束后读取。')}</p> : <p className="muted">{tx('服务正忙，这次没有读到返工资格。')} {again}</p>;
    return <p className="loading">{tx('正在读取返工资格…')}</p>;
  }
  const blockers = e.findings ?? [];
  // Nothing blocks and nothing can be reworked: the review has no rework to offer.
  if (!blockers.length && !e.eligible) return null;
  // The receipt names the integration the rework produced; the job's records are a fallback.
  const child = (job: Job): string | undefined => {
    const result = job.result as { rework?: { child_id?: string | null } | null; revision?: { integration_id?: string | null } | null } | null;
    return result?.rework?.child_id ?? result?.revision?.integration_id ?? job.resources?.find((r) => r.kind === 'integration' && r.id !== id)?.id;
  };
  const refusal = refused ? ((refused.result?.rework as { reason?: string } | undefined)?.reason ?? refused.error?.message ?? null) : null;
  // A child integration does not mean the rework went well: the retest or the re-review that
  // follows may not have passed. The child page is told which job made it, and says so.
  const opened = (job: Job, next: string): string => (jobEnding(job) === 'done' ? to('integrations', next) : `${to('integrations', next)}?rework_job=${encodeURIComponent(job.job_id)}`);
  return (
    <div className="round rework-check" role="group" aria-label={tx('返工资格')}>
      <div className="round-head">
        <strong>{tx('返工资格')}</strong>
        {e.eligible ? <Badge tone="ok">{tx('可以返工')}</Badge> : <Badge tone="muted">{tx('现在不能返工')}</Badge>}
        {read.stale && read.readAt ? <span className="stamp">{jobRunning ? tx('作业运行中，这是 {0} 读到的', readTime(read.readAt)) : tx('这是 {0} 读到的，之后没有读到新的', readTime(read.readAt))}</span> : null}
        {read.stale && !jobRunning ? again : null}
      </div>
      {read.stale && !jobRunning && read.error ? <p className="stale-note problem">{tx('读不到返工资格：{0}', errorText(read.error))}</p> : null}
      {e.eligible && e.owner ? <p className="row">{tx('返工会交给这个成员的实施者：')}<Owner owner={e.owner} /></p> : null}
      {!e.eligible && e.reason ? <p className="row"><span className="stamp">{tx('不能执行的原因')}</span><span className="task-data">{e.reason}</span></p> : null}
      {blockers.length ? (
        <ul className="plain-list" aria-label={tx('阻塞意见的归属')}>
          {blockers.map((b) => (
            <li key={`${b.seat}/${b.finding_id}`}>
              <code>{b.seat}</code>
              <span className="stamp">{tx('意见 ID')} <code>{b.finding_id}</code></span>
              <code className="task-data">{b.path}</code>
              {b.reference_status === 'unverified' ? <span className="finding-ref">{tx('引用没有对上冻结文件')}</span> : null}
              <span className="stamp">{tx('归属')}</span>
              {b.owners?.length ? b.owners.map((owner) => <Owner key={owner.task_run_id} owner={owner} />) : <span className="muted">{tx('没有成员改过这个文件')}</span>}
            </li>
          ))}
        </ul>
      ) : null}
      {e.round_id && !it.rework ? (
        <ActionButton action="integration.rework" primary label={tx('按阻塞意见返工（每条谱系一次）')}
          disabled={!e.eligible || read.stale} reason={read.stale ? (jobRunning ? tx('等作业结束后重新读取资格。') : tx('先重新读取资格。')) : tx('现在不能返工，原因见上。')}
          args={() => ({ integration_id: e.integration_id, round_id: e.round_id })}
          onSubmitted={() => setRefused(null)}
          onRejected={read.reload}
          onDone={(job) => {
            const next = child(job);
            // Only someone still looking at this integration is taken to the one that replaced it.
            if (next) { if (window.location.pathname === to('integrations', id)) navigate(opened(job, next)); return; }
            setRefused(job);
            read.reload();
          }} />
      ) : null}
      {refused ? (
        <p className="stale-note problem" role="alert">
          {jobEnding(refused) === 'refused' ? tx('返工被服务拒绝：{0}', refusal ?? tx('退出码 {0}', refused.exit_code ?? '—'))
            // Lost sight of, not ended: the CLI it started may still deliver the new integration.
            : jobEnding(refused) === 'interrupted' ? tx('返工作业被中断，没有观测到结果。如果它已经启动了 CLI，那个 CLI 可能还在运行，之后仍可能产生新的集成；过一会儿重新读取这一页。')
              : jobEnding(refused) === 'cancelled' ? tx('返工已取消，没有产生新的集成。')
                : refusal ? tx('返工没有产生新的集成：{0}', refusal) : tx('返工没有产生新的集成。')} <Link href={to('jobs', refused.job_id)}>{tx('查看那个作业')}</Link>
        </p>
      ) : null}
    </div>
  );
}

/** Shown on an integration that was opened right after the job whose rework produced it (a
 *  rework, or a review, confirmation or retry that reworked by itself), when that job did
 *  not end as a success: the candidate exists, the checks after it are another matter. */
export function ReworkOutcome({ jobId }: { jobId: string }) {
  const read = useRead<Job>(`job/${jobId}`, () => api.job(jobId), { locking: false });
  const job = read.data?.job_id === jobId ? read.data : null;
  if (!job) return null;
  const ending = jobEnding(job);
  if (ending === 'done' || ending === 'unfinished') return null;
  const what = actionLabel(job.action);
  // What the rework itself said about stopping, when it said anything.
  const said = (job.result?.rework as { reason?: string } | null | undefined)?.reason ?? null;
  return (
    <p className="stale-note problem" role="status">
      {ending === 'unmet'
        ? tx('这个集成是作业「{0}」里的返工产生的，但那个作业的结果不是成功（退出码 {1}）：返工之后的重测或复审没有通过，或者没有跑完。看下面的测试和审核。', what, job.exit_code ?? '—')
        : tx('这个集成是作业「{0}」里的返工产生的，但那个作业没有正常结束：{1}', what, endingText(what, job))}
      {said ? ` ${said}` : ''}
      {' '}<Link href={to('jobs', job.job_id)}>{tx('查看那个作业')}</Link>
    </p>
  );
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* no permission: try the selection-based copy below */
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

/** 交付摘要: the Markdown the service builds from the acceptance receipt, to paste into a PR
 *  or a handoff. Reading and copying it never touches Git, and it stays the accepted version
 *  after the target branch moves. */
export function DeliverySummary({ id }: { id: string }) {
  const read = useRead<Delivery>(`delivery/${id}`, () => api.delivery(id), { locking: false });
  const [copied, setCopied] = useState<'ok' | 'failed' | null>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const d = read.data?.receipt?.integration_id === id ? read.data : null;
  if (!d) {
    // An answer that is not this integration's summary: nothing to show.
    if (read.data) return null;
    // Before acceptance the service answers 409 with the code job_unavailable; either is enough.
    if (read.error instanceof ApiError && (read.error.status === 409 || read.error.code === 'job_unavailable')) return <p className="muted">{tx('交付摘要：还没有验收收据，接受后才能导出。')}</p>;
    if (read.error instanceof ApiError && read.error.code === 'not_found') return null;
    // An archive from before acceptance receipts existed: there is nothing to export, and nothing is wrong.
    if (read.error instanceof ApiError && read.error.code === 'archive_unsupported') return <p className="muted">{tx('交付摘要：这份归档早于验收收据功能，没有可导出的摘要。')}</p>;
    if (read.error) return <p className="stale-note problem">{tx('读不到交付摘要：{0}', errorText(read.error))}</p>;
    return <p className="loading">{tx('正在读取交付摘要…')}</p>;
  }
  const r = d.receipt;
  const copy = async (): Promise<void> => {
    const ok = await copyText(d.markdown);
    setCopied(ok ? 'ok' : 'failed');
    if (!ok) { area.current?.focus(); area.current?.select(); }
  };
  return (
    <div className="delivery" role="group" aria-label={tx('交付摘要')}>
      <h3 className="sub-head">{tx('交付摘要')}</h3>
      <p className="section-note wide">{tx('按验收收据导出：之后目标分支移动或重新验收，这里仍是当时接受的版本。摘要里的链接指向这台机器上的工作台。')}</p>
      <Facts rows={[
        [tx('候选提交'), <span className="row"><code className="task-data">{r.candidate?.commit ?? '—'}</code><span className="stamp">{r.candidate?.ref}</span></span>],
        [tx('目标分支'), <code className="task-data">{r.target_ref}</code>],
        [tx('验收基线'), <code className="task-data">{r.target_baseline}</code>],
        [tx('验收收据'), <span className="row"><code className="task-data">{r.acceptance_id}</code><span className="stamp">{timeText(r.accepted_at)}</span></span>],
        [tx('交付内容'), <ul className="plain-list">{(d.tasks ?? []).map((task) => <li key={task.id}><code>{task.id}</code><span>{task.goal}</span></li>)}</ul>],
        [tx('测试'), <ul className="plain-list">
          <li>{tx('第 {0} 次测试通过', r.test_attempt)}</li>
          {(d.tests ?? []).map((test, i) => <li key={i}><code>{test.name}</code><span className="stamp">{tx('退出码 {0}', test.exit_code ?? '—')}</span></li>)}
        </ul>],
        [tx('测试证据'), <span className="row"><span className="stamp">SHA-256</span><code className="task-data">{r.test_evidence_hash}</code></span>],
        [tx('审核检查点'), <span className="row"><span className="stamp">SHA-256</span><code className="task-data">{r.review_checkpoint_hash}</code></span>],
        [tx('依据'), <span className="row">
          {d.links?.integration ? <Link href={d.links.integration}>{tx('交付与测试')}</Link> : null}
          {d.links?.diff ? <Link href={d.links.diff}>{tx('冻结差异')}</Link> : null}
          {d.links?.review ? <Link href={d.links.review}>{tx('审核依据')}</Link> : null}
        </span>],
      ]} />
      <label className="plan-label">{tx('摘要预览（Markdown）')}
        <textarea ref={area} className="field big mono" rows={12} readOnly value={d.markdown} spellCheck={false} lang="zh" />
      </label>
      {/* The service writes the summary in Chinese whatever the interface language is. */}
      {getLang() === 'en' ? <p className="section-note wide">{tx('导出的摘要是中文的，服务目前不提供其他语言；里面的提交、哈希、命令和退出码与上面一致。')}</p> : null}
      <div className="form-actions">
        <button type="button" className="btn primary" onClick={() => void copy()}>{tx('复制交付摘要')}</button>
        <span role="status" aria-live="polite" className={copied === 'failed' ? 'action-why problem' : 'action-why'}>
          {copied === 'ok' ? tx('已复制到剪贴板。') : copied === 'failed' ? tx('复制失败：浏览器没有给剪贴板权限。上面的文本已经选中，请手动复制。') : ''}
        </span>
      </div>
    </div>
  );
}
