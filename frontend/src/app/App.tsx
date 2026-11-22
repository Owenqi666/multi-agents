import { useEffect, type ReactNode } from 'react';
import { jobs, useJobs } from '../api/jobs';
import { useReadsInFlight } from '../api/reads';
import { useArchiveMode, useWorkspaceWritesAllowed, WorkspaceProvider } from '../api/workspace';
import { ThemeSwitch } from '../components/ThemeSwitch';
import { LanguageSwitch } from '../components/LanguageSwitch';
import { useLang } from '../i18n';
import { EmptyState } from '../components/EmptyState';
import { JobBar } from './JobBar';
import { Link, match, navigate, useLocation } from './router';
import { ArchivePage } from '../pages/ArchivePage';
import { HomePage } from '../pages/HomePage';
import { NewGoalPage, ConsultationPage } from '../pages/GoalPages';
import { ProjectsPage, ProjectPage, GoalPage } from '../pages/ProjectPages';
import { PlanPage } from '../pages/PlanPage';
import { StagePage } from '../pages/StagePage';
import { TaskPage } from '../pages/TaskPage';
import { NewTaskPlanPage } from '../pages/NewTaskPlanPage';
import { OpenTaskPlanPage, TaskPlanPage } from '../pages/TaskPlanPage';
import { IntegrationPage } from '../pages/IntegrationPage';
import { ReviewPage } from '../pages/ReviewPage';
import { NewReviewPage, ReviewHomePage, ReviewSnapshotPage } from '../pages/StandaloneReviewPages';
import { DiffPage, FrozenFilePage, LogPage } from '../pages/ViewerPages';
import { JobsPage, JobPage } from '../pages/JobPages';
import { tx } from '../i18n';

type Route = [string, (p: Record<string, string>, q: URLSearchParams) => ReactNode];

const ROUTES: Route[] = [
  ['/', () => <HomePage />],
  ['/archive', () => <ArchivePage />],
  ['/new', () => <NewGoalPage />],
  ['/projects', () => <ProjectsPage />],
  ['/projects/:id', (p) => <ProjectPage id={p.id!} />],
  ['/goals/:id', (p) => <GoalPage id={p.id!} />],
  ['/consultations/:id', (p) => <ConsultationPage key={p.id!} id={p.id!} />],
  ['/plans/:id', (p) => <PlanPage id={p.id!} />],
  ['/stages/:id', (p) => <StagePage id={p.id!} />],
  ['/task-plans', () => <OpenTaskPlanPage />],
  ['/task-plans/new', (_, q) => <NewTaskPlanPage key={q.toString()} from={q.get('from')} questionIndex={q.get('question')} />],
  ['/task-plans/:id', (p) => <TaskPlanPage key={p.id!} id={p.id!} />],
  ['/tasks/:id', (p) => <TaskPage id={p.id!} />],
  ['/integrations/:id', (p) => <IntegrationPage id={p.id!} />],
  ['/reviews', () => <ReviewHomePage />],
  ['/reviews/new', () => <NewReviewPage />],
  ['/reviews/:id', (p, q) => <ReviewPage key={p.id!} id={p.id!} patchLocation={q} />],
  ['/snapshots/:id', (p) => <ReviewSnapshotPage key={p.id!} id={p.id!} />],
  ['/snapshots/:id/file', (p, q) => <FrozenFilePage id={p.id!} side={(q.get('side') as 'baseline' | 'candidate') ?? 'candidate'} path={q.get('path') ?? ''} line={Number(q.get('line') ?? 0)} end={Number(q.get('end') ?? q.get('line') ?? 0)} />],
  ['/diffs/:kind/:id', (p, q) => <DiffPage kind={p.kind as 'task' | 'integration' | 'snapshot'} id={p.id!} against={q.get('against') === 'parent' ? 'parent' : undefined} />],
  ['/logs/:id', (p, q) => <LogPage key={p.id!} id={p.id!} title={q.get('title') ?? tx('日志')} locking={q.get('locking') === '1'} />],
  ['/jobs', () => <JobsPage />],
  ['/jobs/:id', (p) => <JobPage id={p.id!} />],
];

function Outlet() {
  const { path, query } = useLocation();
  const archive = useArchiveMode();
  if (archive && path === '/') return <ArchivePage />;
  if (archive && path === '/new') {
    return <EmptyState title={tx('工作区为只读')} body={tx('可以打开归档中的现有记录。')} action={<Link className="btn" href="/archive">{tx('归档')}</Link>} />;
  }
  for (const [pattern, render] of ROUTES) {
    const params = match(pattern, path);
    if (params) return <>{render(params, query)}</>;
  }
  return <EmptyState title={tx('没有这个页面')} body={tx('地址可能写错了，从上面的导航进入。')} action={<Link className="btn" href="/">{tx('回到首页')}</Link>} />;
}

function WorkspaceShell() {
  const { path } = useLocation();
  const archive = useArchiveMode();
  const writesAllowed = useWorkspaceWritesAllowed();
  const reading = useReadsInFlight();
  const { active, settling } = useJobs();
  useEffect(() => { void jobs.init(); }, []);
  useEffect(() => {
    if (archive && path === '/') navigate('/archive', true);
  }, [archive, path]);
  // For scripts and screen readers: whether the page is still catching up.
  useEffect(() => {
    document.documentElement.dataset.reading = reading > 0 || settling ? '1' : '0';
    document.documentElement.dataset.job = active ? active.state : 'none';
  }, [reading, active, settling]);
  const nav: [string, string][] = archive ? [['/archive', tx('归档')]] : [['/', tx('舞台')], ['/projects', tx('项目')], ['/task-plans', tx('单卡')], ['/reviews', tx('审核')], ['/jobs', tx('作业')]];
  const current = (href: string) => href === '/' ? path === '/' : path.startsWith(href) || (href === '/reviews' && path.startsWith('/snapshots/'));
  return (
    <div className="app">
      <header className="app-head">
        <Link className="brand" href={archive ? '/archive' : '/'}>
          <svg width="34" height="8" viewBox="0 0 34 8" aria-hidden="true">
            <line x1="1" y1="4" x2="33" y2="4" strokeWidth="1" style={{ stroke: 'var(--ink-3)' }} />
            <circle cx="5" cy="4" r="2.4" style={{ fill: 'var(--tone-astra)' }} />
            <circle cx="17" cy="4" r="2.4" style={{ fill: 'var(--tone-opus)' }} />
            <circle cx="29" cy="4" r="2.4" style={{ fill: 'var(--tone-terra)' }} />
          </svg>
          {tx('Multi-agent 工作台')}
        </Link>
        <nav className="app-nav" aria-label={tx('主导航')}>
          {nav.map(([href, label]) => <Link key={href} href={href} aria-current={current(href) ? 'page' : undefined}>{label}</Link>)}
        </nav>
        <div className="app-tools">
          {archive ? <span className="stamp archive-mode">{tx('只读')}</span> : null}
          {writesAllowed ? <Link className="btn small primary" href="/new">{tx('提出目标')}</Link> : null}
          <ThemeSwitch />
          <LanguageSwitch />
        </div>
      </header>
      <JobBar />
      <main className="app-main"><Outlet /></main>
    </div>
  );
}

export function App() {
  // Switching the language redraws every page, including drawings measured with the fonts.
  const lang = useLang();
  return <WorkspaceProvider><WorkspaceShell key={lang} /></WorkspaceProvider>;
}
