// 检查接入: what the selected baseline needs before work can start: the commit itself, the
// rules file and its registered commands, and whether each provider's CLI is signed in.
// Read-only: opening the panel or changing the baseline writes nothing and starts no model.
// A provider check is the existing environment.refresh job, started only by a click.

import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useJobs } from '../api/jobs';
import { useRead } from '../api/reads';
import { ApiError, errorText } from '../api/transport';
import type { Onboarding, OnboardingProvider } from '../api/types';
import { shortId, timeText } from '../app/labels';
import { Link, to } from '../app/router';
import { ActionButton } from '../components/Action';
import { Badge, type BadgeTone } from '../components/Badge';
import { tx } from '../i18n';
import { Facts, Section } from './common';

const SETTLE_MS = 400;

/** The service could not resolve the baseline to a commit (a typo, an unknown branch). */
export const unresolved = (error: unknown): error is ApiError => error instanceof ApiError && error.code === 'invalid_request';

interface Checked {
  /** The baseline the check was asked for; null means HEAD. */
  query: string | null;
  value: Onboarding | null;
}

export interface OnboardingRead {
  /** The check for the baseline now in the form; null while it is being read. */
  check: Onboarding | null;
  loading: boolean;
  error: unknown;
  /** The service has no setup check (an older service, or the recorded demo). */
  unavailable: boolean;
  reload: () => void;
}

/** Missing keys read as empty. An answer without the rules check is not a setup check. */
function normalize(value: Partial<Onboarding> | null): Onboarding | null {
  if (!value || typeof value !== 'object' || !value.rules) return null;
  return {
    repository: value.repository ?? '', read_only: value.read_only === true,
    branch: value.branch ?? null, head: value.head ?? null, branches: value.branches ?? [], baseline: value.baseline ?? null,
    rules: {
      path: value.rules.path ?? '', status: value.rules.status ?? '', detail: value.rules.detail ?? null,
      commands: (value.rules.commands ?? []).map((command) => ({ name: command?.name ?? '', argv: command?.argv ?? [] })),
    },
    rules_template: value.rules_template ?? '', providers: value.providers ?? {}, next_steps: value.next_steps ?? [],
  };
}

/** Follows the baseline field: typing settles for a moment before the check is asked again. */
export function useOnboarding(baseline: string, head: string | null): OnboardingRead {
  const wanted = baseline.trim();
  const [settled, setSettled] = useState(wanted);
  useEffect(() => {
    if (wanted === settled) return;
    const timer = setTimeout(() => setSettled(wanted), SETTLE_MS);
    return () => clearTimeout(timer);
  }, [wanted, settled]);
  const query = settled && settled !== head ? settled : null;
  // An error belongs to the baseline it was asked for; it is never shown for the next one.
  const [failure, setFailure] = useState<{ query: string | null; error: unknown } | null>(null);
  const read = useRead<Checked>(`onboarding/${query ?? ''}`, async () => {
    try {
      const value = normalize(await api.onboarding(query));
      setFailure((f) => (f?.query === query ? null : f));
      return { query, value };
    } catch (error) {
      setFailure({ query, error });
      throw error;
    }
  }, { locking: false });
  const current = wanted === settled && read.data?.query === query;
  const failed = wanted === settled && failure?.query === query ? failure.error : null;
  const missing = failed instanceof ApiError && failed.status === 404;
  return {
    check: current ? read.data!.value : null,
    loading: !current || read.loading,
    error: missing ? null : failed,
    unavailable: missing || (current && read.data!.value === null),
    reload: read.reload,
  };
}

const RULES: Record<string, [BadgeTone, string]> = {
  ready: ['ok', '已登记测试命令'],
  no_commands: ['warn', '还没有登记测试命令'],
  missing: ['warn', '这个提交里没有规则文件'],
  invalid: ['fail', '规则文件无效'],
  no_commit: ['muted', '仓库还没有提交'],
};
const PROVIDER: Record<string, [BadgeTone, string]> = {
  ready: ['ok', '已登录'],
  login_required: ['warn', '需要登录'],
  check_failed: ['fail', '检查失败'],
  checking: ['live', '检查中'],
  missing_cli: ['fail', '找不到命令行程序'],
  unknown: ['muted', '还没有确认登录'],
};
const NAME: Record<string, string> = { codex: 'Codex', claude: 'Claude' };
const providerName = (provider: string | undefined): string => (provider ? NAME[provider] ?? provider : '');

function stepText(step: { kind: string; provider?: string }, check: Onboarding): string {
  const name = providerName(step.provider);
  const path = check.rules.path;
  // The service asks for a check both before the first one and after one that failed.
  if (step.kind === 'check_provider' && check.providers[step.provider ?? '']?.status === 'check_failed') {
    return tx('上一次检查 {0} 没有成功。看上面的错误，处理后再检查一次。', name);
  }
  switch (step.kind) {
    case 'commit_baseline': return tx('仓库还没有提交。先在仓库里提交一次，作为基线。');
    case 'add_rules': return tx('这个基线里没有 {0}。复制下面的模板，在你的仓库里审阅并提交，再回来重新检查这个基线。', path);
    case 'fix_rules': return tx('这个基线里的 {0} 无效。按上面的错误修正并提交，再重新检查。', path);
    case 'register_commands': return tx('规则里还没有登记测试命令。登记并提交后，任务才能引用它们。');
    case 'install_cli': return tx('找不到 {0} 的命令行程序。要用它的模型，先安装。', name);
    case 'check_provider': return tx('还没有确认 {0} 的登录。要用它的模型，先检查一次。', name);
    case 'sign_in': return tx('{0} 还没有登录。要用它的模型，先在终端登录，再重新检查。', name);
    case 'create_goal': return tx('现在就可以写下目标并直接确认原文；规则和登录可以之后再补。');
    default: return step.kind;
  }
}

function ProviderRow({ provider, check }: { provider: string; check: OnboardingProvider }) {
  // A check started from this browser shows at once; the service's own answer follows when it ends.
  const { environment } = useJobs();
  const status = environment[provider] ? 'checking' : check.status;
  const [tone, label] = PROVIDER[status] ?? ['plain', status];
  const name = providerName(provider);
  return (
    <span className="row">
      <Badge tone={tone}>{PROVIDER[status] ? tx(label) : label}</Badge>
      {check.cli_version ? <span className="stamp">{check.cli_version}</span> : null}
      {check.observed_at ? <span className="stamp">{tx('观测于 {0}', timeText(check.observed_at))}</span> : null}
      {check.error?.message ? <span className="action-why problem">{check.error.message}</span> : null}
      {check.check_job_id ? <Link href={to('jobs', check.check_job_id)}>{tx('检查记录')}</Link> : null}
      {status !== 'missing_cli' && status !== 'checking' ? (
        <ActionButton action="environment.refresh" args={() => ({ provider })}
          label={status === 'unknown' ? tx('检查 {0} 登录', name) : tx('重新检查 {0}', name)} />
      ) : null}
    </span>
  );
}

function Template({ text, path, open, onRecheck }: { text: string; path: string; open: boolean; onRecheck: () => void }) {
  const [copied, setCopied] = useState<'ok' | 'failed' | null>(null);
  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied('ok');
    } catch {
      setCopied('failed');
    }
  };
  return (
    <details className="raw" open={open ? true : undefined}>
      <summary>{tx('规则文件模板')}</summary>
      <p className="section-note wide">{tx('模板是 Python unittest 的例子，命令和模型换成适合你仓库的。工作台不会替你写文件：复制后在自己的仓库里审阅、保存为 {0} 并提交，再回来重新检查对应的基线。', path)}</p>
      <pre role="region" aria-label={tx('规则文件模板')} tabIndex={0}>{text}</pre>
      <div className="form-actions">
        <button type="button" className="btn small" onClick={() => void copy()}>{tx('复制模板')}</button>
        <button type="button" className="btn small quiet" onClick={onRecheck}>{tx('重新检查这个基线')}</button>
        <span role="status" aria-live="polite" className={copied === 'failed' ? 'action-why problem' : 'action-why'}>
          {copied === 'ok' ? tx('已复制到剪贴板。') : copied === 'failed' ? tx('复制失败，请在上面的文本里手动选中复制。') : ''}
        </span>
      </div>
    </details>
  );
}

export function OnboardingPanel({ read }: { read: OnboardingRead }) {
  const { check, loading, error, reload } = read;
  if (read.unavailable) return null;
  return (
    <Section title={tx('检查接入')} note={tx('按上面选中的基线检查：规则文件和测试命令从那个提交里读。只读，不写文件，也不启动模型。')}>
      {error ? <p className="stale-note problem">{tx('这个基线检查不了：{0}', errorText(error))}{unresolved(error) ? ` ${error.message}` : ''}</p> : null}
      {!check ? (error ? null : <p className="loading">{tx('正在检查接入…')}</p>) : (
        <div className="onboarding" aria-busy={loading}>
          <Facts rows={[
            [tx('仓库'), <code className="task-data">{check.repository}</code>],
            [tx('当前分支'), <span className="row">{check.branch ? <code>{check.branch}</code> : tx('没有检出分支')}{check.head ? <span className="stamp">HEAD <code>{shortId(check.head, 10)}</code></span> : null}</span>],
            [tx('选中的基线'), check.baseline ? (
              <span className="row"><code className="task-data">{check.baseline}</code><span className="stamp">{check.baseline === check.head ? tx('就是当前 HEAD') : tx('不是当前 HEAD')}</span></span>
            ) : <Badge tone="warn">{tx('仓库还没有提交')}</Badge>],
            [tx('规则文件'), <span className="row">
              <code>{check.rules.path}</code>
              <Badge tone={RULES[check.rules.status]?.[0] ?? 'plain'}>{RULES[check.rules.status] ? tx(RULES[check.rules.status]![1]) : check.rules.status}</Badge>
              {check.rules.detail ? <span className="action-why problem">{check.rules.detail}</span> : null}
            </span>],
            [tx('已登记的命令'), check.rules.commands.length ? (
              <div className="onboarding-commands">
                <ul className="plain-list" aria-label={tx('已登记的命令')}>
                  {check.rules.commands.map((command) => <li key={command.name}><code>{command.name}</code><code className="task-data stamp">{command.argv.join(' ')}</code></li>)}
                </ul>
                {/* The check reads the rules; it runs nothing. Whether the test sandbox can run them is still open. */}
                <p className="action-why">{tx('这里只确认命令已经登记。测试运行环境里能不能跑这些命令还没有验证，第一次运行测试时才知道。')}</p>
              </div>
            ) : tx('没有')],
            ...Object.entries(check.providers).map(([provider, value]) => [providerName(provider), <ProviderRow provider={provider} check={value} />] as [string, React.ReactNode]),
          ]} />
          {check.next_steps.length ? (
            <ul className="next-steps" aria-label={tx('下一步')}>
              {check.next_steps.map((step, i) => <li key={i} className={step.kind === 'create_goal' ? 'ready' : undefined}>{stepText(step, check)}</li>)}
            </ul>
          ) : null}
          <Template text={check.rules_template} path={check.rules.path} open={['missing', 'invalid', 'no_commands'].includes(check.rules.status)} onRecheck={reload} />
          {/* Said in words while a newer check is on its way; the last answer stays readable meanwhile. */}
          <p className="stamp onboarding-status" role="status">{loading ? tx('正在重新检查…') : ''}</p>
        </div>
      )}
    </Section>
  );
}
