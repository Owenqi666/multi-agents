import type { ImplementationAttempt, RoleRequest } from '../api/types';
import { timeText } from '../app/labels';
import { Badge, Unconfirmed, type BadgeTone } from '../components/Badge';
import { Evidence, Facts } from './common';
import { tx } from '../i18n';

const DECISION_REASON: Record<string, string> = {
  explicit_request: '采用指定模型',
  first_compatible_default: '采用冻结规则默认值',
  first_common_configured_model: '采用规则共同允许的模型',
  enabled_ordered_fallback: '按冻结顺序选用备选',
};

const SELECTION_OUTCOME: Record<string, string> = {
  disallowed_by_rules: '冻结规则禁止',
  known_unavailable: '已知不可用',
  selected_request: '已选择',
};

// What the implementer says about its own run. It is a claim: the candidate, tests and review decide.
const OUTCOME: Record<string, { label: string; tone: BadgeTone }> = {
  completed: { label: '报告完成', tone: 'plain' },
  scope_blocked: { label: '报告改动范围不够，没有交付', tone: 'warn' },
};

const USAGE_LABEL = new Map(Object.entries({
  inputTokens: '输入 token',
  cachedInputTokens: '缓存输入 token',
  cacheWriteInputTokens: '写入缓存的输入 token',
  outputTokens: '输出 token',
  reasoningOutputTokens: '推理输出 token',
  totalTokens: '总 token',
  modelContextWindow: '上下文窗口',
  input_tokens: '输入 token',
  output_tokens: '输出 token',
  cache_creation_input_tokens: '写入缓存的输入 token',
  cache_read_input_tokens: '读取缓存的输入 token',
  client_estimated_cost_usd: 'Claude 客户端估算（USD）',
}));

function counterLabel(key: string): string {
  if (key.startsWith('total.')) {
    const name = key.slice(6);
    return tx('累计 · {0}', USAGE_LABEL.has(name) ? tx(USAGE_LABEL.get(name)!) : name);
  }
  if (key.startsWith('last.')) {
    const name = key.slice(5);
    return tx('最近一次 · {0}', USAGE_LABEL.has(name) ? tx(USAGE_LABEL.get(name)!) : name);
  }
  return USAGE_LABEL.has(key) ? tx(USAGE_LABEL.get(key)!) : key;
}

function Choice({ value }: { value: RoleRequest }) {
  return <span className="task-data"><code>{value.model}</code> <span className="stamp">{value.effort}</span></span>;
}

function Usage({ evidence }: { evidence: ImplementationAttempt['usage_evidence'] }) {
  if (!evidence) return <Unconfirmed what={tx('用量')} />;
  const counters = Object.entries(evidence.counts);
  return (
    <div className="task-usage">
      <span className="stamp">{tx('来源')} <code className="task-data">{evidence.source}</code></span>
      {counters.length ? (
        <dl className="task-counters">
          {counters.map(([key, value]) => (
            <div key={key}><dt title={key}>{counterLabel(key)}</dt><dd><code>{value}</code></dd></div>
          ))}
        </dl>
      ) : <Unconfirmed what={tx('用量')} />}
    </div>
  );
}

function Report({ result }: { result: unknown }) {
  const report = result && typeof result === 'object' ? result as { outcome?: unknown; summary?: unknown; required_paths?: unknown } : null;
  const outcome = typeof report?.outcome === 'string' ? report.outcome : null;
  if (!report || !outcome) return <Unconfirmed what={tx('实施者报告')} />;
  const paths = Array.isArray(report.required_paths) ? report.required_paths.filter((path): path is string => typeof path === 'string') : [];
  const known = OUTCOME[outcome];
  return (
    <div className="task-report">
      <span className="row">
        {known ? <Badge tone={known.tone}>{tx(known.label)}</Badge> : <code className="task-data">{outcome}</code>}
        {paths.length ? <span className="stamp task-data">{tx('它说还需要改')} {paths.map((path) => <code key={path}>{path}</code>)}</span> : null}
      </span>
      {typeof report.summary === 'string' && report.summary ? <p className="goal-text">{report.summary}</p> : null}
    </div>
  );
}

function RuntimeReroutes({ reroutes }: { reroutes: ImplementationAttempt['runtime_reroutes'] }) {
  if (!reroutes) return <Unconfirmed what={tx('运行时改派')} />;
  if (!reroutes.length) return <span className="stamp">{tx('未记录改派')}</span>;
  return (
    <ul className="task-reroutes">
      {reroutes.map((reroute, index) => (
        <li key={index}>
          <span className="task-data"><code>{reroute.from_model}</code> → <code>{reroute.to_model}</code></span>
          <span className="stamp task-data">{tx('原因')} <code>{reroute.reason}</code></span>
          <span className="stamp task-data">{tx('来源')} <code>{reroute.source}</code></span>
        </li>
      ))}
    </ul>
  );
}

export function TaskAttemptEvidence({ attempt, number }: { attempt: ImplementationAttempt; number: number }) {
  const { request, runtime } = attempt;
  const { decision } = request;
  const fallbacks = attempt.events?.find((event) => event.kind === 'task_execution_prepared')?.dispatch_policy?.fallbacks;
  const requested = decision.requested === null ? tx('按冻结规则默认派发')
    : decision.requested ? <Choice value={decision.requested} /> : <Unconfirmed what={tx('实施请求')} />;
  return (
    <li className="task-attempt-evidence">
      <header className="row">
        <h3>{request.kind === 'task_rework_started' ? tx('返工') : tx('第 {0} 次实施', number)}</h3>
        <span className="stamp">{timeText(request.started_at)}</span>
        {runtime?.status ? <span className="stamp">{runtime.status}</span> : null}
      </header>
      <Facts rows={[
        [tx('实施者报告'), <Report result={runtime?.result} />],
        [tx('工作树'), request.worktree ? <code className="task-data">{request.worktree}</code> : <Unconfirmed what={tx('工作树')} />],
        [tx('请求'), requested],
        [tx('工作台选择'), <span className="row">
          <Choice value={decision.choice} />
          {decision.reason ? <span className="stamp" title={decision.reason}>{DECISION_REASON[decision.reason] ? tx(DECISION_REASON[decision.reason]!) : decision.reason}</span> : null}
        </span>],
        [tx('运行时报告'), <span className="task-data"><Evidence reportedModel={runtime?.reported_model} reportedEffort={runtime?.reported_effort} /></span>],
        [tx('冻结备选链'), fallbacks ? (
          fallbacks.length ? <ol className="task-choice-list">{fallbacks.map((choice, index) => <li key={index}><Choice value={choice} /></li>)}</ol> : tx('无备选')
        ) : <Unconfirmed what={tx('冻结备选链')} />],
        [tx('选择记录'), decision.selection_history?.length ? (
          <ol className="task-choice-list">{decision.selection_history.map((entry, index) => (
            <li key={index}><Choice value={entry.choice} /> <span className="stamp" title={entry.outcome}>{SELECTION_OUTCOME[entry.outcome] ? tx(SELECTION_OUTCOME[entry.outcome]!) : entry.outcome}</span></li>
          ))}</ol>
        ) : <Unconfirmed what={tx('选择记录')} />],
        [tx('用量'), <Usage evidence={attempt.usage_evidence} />],
        [tx('运行时改派'), <RuntimeReroutes reroutes={attempt.runtime_reroutes} />],
      ]} />
    </li>
  );
}
