// A small path router. The bridge serves index.html for every non-asset path, so pages can
// use real paths and survive a refresh.

import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent, type ReactNode } from 'react';

const listeners = new Set<() => void>();
const notify = (): void => listeners.forEach((l) => l());
window.addEventListener('popstate', notify);

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

const snapshot = (): string => window.location.pathname + window.location.search;

export function useLocation(): { path: string; query: URLSearchParams } {
  const full = useSyncExternalStore(subscribe, snapshot);
  const [path, search = ''] = full.split('?');
  return { path: path || '/', query: new URLSearchParams(search) };
}

export function navigate(to: string, replace = false): void {
  if (to === snapshot()) return;
  if (replace) window.history.replaceState(null, '', to);
  else window.history.pushState(null, '', to);
  window.scrollTo({ top: 0 });
  notify();
}

/** Match '/stages/:id' against a path; params come back decoded. */
export function match(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split('/').filter(Boolean);
  const s = path.split('/').filter(Boolean);
  if (p.length !== s.length) return null;
  const out: Record<string, string> = {};
  for (let i = 0; i < p.length; i++) {
    const a = p[i]!, b = s[i]!;
    if (a.startsWith(':')) out[a.slice(1)] = decodeURIComponent(b);
    else if (a !== b) return null;
  }
  return out;
}

/** Build a path from segments, encoding each id. */
export function to(...parts: (string | number)[]): string {
  return '/' + parts.map((p) => encodeURIComponent(String(p))).join('/');
}

export function Link({ href, children, ...rest }: { href: string; children: ReactNode } & AnchorHTMLAttributes<HTMLAnchorElement>) {
  const onClick = (ev: MouseEvent<HTMLAnchorElement>): void => {
    if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault();
    navigate(href);
  };
  return <a href={href} onClick={onClick} {...rest}>{children}</a>;
}
