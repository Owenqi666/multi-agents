import type { ArtifactRef, ReviewRound, TaskView } from '../api/types';
import { ArtifactLinks, Section } from './common';

function SessionGroup({ title, logs, locking }: { title: string; logs: ArtifactRef[]; locking?: boolean }) {
  return (
    <li className="session-log-group">
      <h3 className="sub-head">{title}</h3>
      {logs.length ? (
        <ArtifactLinks artifacts={logs} locking={locking} label={(log) => `${title} · ${log.stream ?? '会话输出'}`}
          linkText={(log) => !log.stream || log.stream === 'session' ? '打开会话日志' : `打开 ${log.stream}`} />
      ) : <p className="muted">没有可验证的会话日志。</p>}
    </li>
  );
}

export function TaskSessionLogs({ attempts, artifacts }: { attempts: TaskView['attempts']; artifacts: readonly ArtifactRef[] }) {
  const logs = artifacts.filter((log) => log.key?.startsWith('session-task-'));
  return (
    <Section title="模型会话日志">
      {attempts.length ? <ul className="session-log-groups">
        {attempts.map((attempt, index) => {
          const number = attempt.runtime?.number ?? index + 1;
          return <SessionGroup key={number} title={`实施尝试 ${number}`} locking
            logs={logs.filter((log) => log.coordinates?.implementation_attempt === number)} />;
        })}
      </ul> : <p className="muted">还没有实施尝试。</p>}
    </Section>
  );
}

export function ReviewSessionLogs({ round, artifacts }: { round: ReviewRound; artifacts: readonly ArtifactRef[] }) {
  const logs = artifacts.filter((log) => log.key?.startsWith('session-review-'));
  const attempts = round.seats.flatMap((seat, index) => seat.attempts.map((attempt) => ({
    title: `席位 ${seat.key} · 尝试 ${attempt.number}`,
    logs: logs.filter((log) => log.coordinates?.seat === index + 1 && log.coordinates.review_attempt === attempt.number),
  })));
  return (
    <Section title="模型会话日志">
      {attempts.length ? <ul className="session-log-groups">
        {attempts.map(({ title, logs: streams }) => <SessionGroup key={title} title={title} logs={streams} />)}
      </ul> : <p className="muted">还没有审核席位开始运行。</p>}
    </Section>
  );
}
