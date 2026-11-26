import { Component, Fragment, type ReactNode } from 'react';
import { forgetReads } from '../api/reads';
import { EmptyState } from '../components/EmptyState';
import { tx } from '../i18n';
import { Link } from './router';

/** Catches an exception thrown while a page renders, so the header, the navigation and the
 *  job bar stay in place and only this page says that it could not be shown. `location` is
 *  the current address: going anywhere else clears the notice, while a page that works is
 *  left mounted when only its query changes. Retrying forgets what was read, because the
 *  page would otherwise be drawn again from the very record that broke it. */
export class PageBoundary extends Component<{ location: string; children: ReactNode }, { error: unknown; attempt: number }> {
  state = { error: null as unknown, attempt: 0 };

  static getDerivedStateFromError(error: unknown): { error: unknown } {
    return { error: error ?? new Error('unknown error') };
  }

  componentDidUpdate(previous: { location: string }): void {
    if (this.state.error !== null && previous.location !== this.props.location) this.setState({ error: null });
  }

  private retry = (): void => {
    forgetReads();
    this.setState((s) => ({ error: null, attempt: s.attempt + 1 }));
  };

  render(): ReactNode {
    const { error, attempt } = this.state;
    if (error === null) return <Fragment key={attempt}>{this.props.children}</Fragment>;
    const reason = error instanceof Error ? error.message : String(error);
    return (
      <div role="alert">
        <EmptyState title={tx('这一页显示不出来')}
          body={<>{tx('显示这一页时出了错，其他页面不受影响。重试会重新读取这一页的记录；还是不行的话，把下面的原因记下来。')}<br /><code className="task-data">{reason}</code></>}
          action={
            <span className="row">
              <button type="button" className="btn primary" onClick={this.retry}>{tx('重试这一页')}</button>
              {window.location.pathname === '/' ? null : <Link className="btn" href="/">{tx('回到首页')}</Link>}
            </span>
          } />
      </div>
    );
  }
}
