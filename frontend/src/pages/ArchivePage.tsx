import { useState } from 'react';
import { useWorkspace } from '../api/workspace';
import { Link, navigate, to } from '../app/router';
import { EmptyState } from '../components/EmptyState';
import { ReadFrame } from '../components/ReadNote';
import { Page, Section } from './common';

const RECORDS = [
  { route: 'reviews', label: '审核轮' },
  { route: 'diffs/snapshot', label: '冻结快照' },
  { route: 'task-plans', label: '单卡规划' },
  { route: 'tasks', label: '实施' },
  { route: 'integrations', label: '集成' },
  { route: 'consultations', label: '需求咨询' },
  { route: 'projects', label: '项目' },
  { route: 'goals', label: '目标' },
  { route: 'plans', label: '项目计划' },
  { route: 'stages', label: '阶段' },
];

export function ArchivePage() {
  const workspace = useWorkspace();
  const [route, setRoute] = useState('reviews');
  const [id, setId] = useState('');
  return (
    <Page title="归档">
      <ReadFrame read={workspace} what="归档工作区">
        {(ws) => ws.read_only ? (
          <Section title="打开记录" note="按记录类型和 ID 查看归档中的历史证据。">
            <p className="row archive-repository"><span className="stamp">仓库</span><code className="task-data">{ws.repository}</code></p>
            <form className="form-actions archive-open" onSubmit={(event) => {
              event.preventDefault();
              if (id.trim()) navigate(to(...route.split('/'), id.trim()));
            }}>
              <label>记录类型
                <select className="field" value={route} onChange={(event) => setRoute(event.target.value)}>
                  {RECORDS.map((record) => <option key={record.route} value={record.route}>{record.label}</option>)}
                </select>
              </label>
              <label>记录 ID
                <input className="field mono" value={id} onChange={(event) => setId(event.target.value)} required />
              </label>
              <button className="btn primary" type="submit" disabled={!id.trim()}>打开记录</button>
            </form>
          </Section>
        ) : <EmptyState title="当前工作区未打开归档" action={<Link className="btn" href="/">返回舞台</Link>} />}
      </ReadFrame>
    </Page>
  );
}
