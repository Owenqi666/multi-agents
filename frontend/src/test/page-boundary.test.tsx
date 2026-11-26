// A page that throws while it renders must not take the whole app down: the header and the
// navigation stay, the page says what happened, and it can be tried again or left.

import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { setTransport } from '../api/client';
import type { Workspace } from '../api/types';
import { App } from '../app/App';
import { directStage } from '../fixtures/specimen';
import { setLangForTest } from '../i18n';
import { fakeTransport } from './fake';

const workspace: Workspace = {
  repository: '/repo', target_branch: 'main', head: 'base', read_only: false,
  environments: { claude: null, codex: null },
  presets: { source: 'test', rules: {}, roles: [] },
};
// A stage record without the planning block every stage has: the stage page cannot draw it.
const broken = { kind: 'stage', id: 's1', artifacts: [], data: { stage_run_id: 's1', plan_id: 'p1', stage_id: 'amount', mode: 'direct' } };
const sound = { kind: 'stage', id: 's1', artifacts: [], data: { ...directStage, stage_run_id: 's1' } };

function open(path: string, stage: () => unknown) {
  window.history.replaceState(null, '', path);
  setTransport(fakeTransport({
    '/workspace': workspace, '/jobs': { items: [] }, '/runs': { items: [] }, '/attention': { items: [], complete: true },
    '/resources/stage/s1': stage,
  }));
  return render(<App />);
}

beforeEach(() => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
  // React reports the caught exception on the console; the tests expect it.
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/');
});

it('keeps the header and navigation when a page throws, and says why', async () => {
  open('/stages/s1', () => broken);
  const notice = await screen.findByRole('alert');
  expect(within(notice).getByText('这一页显示不出来')).toBeTruthy();
  // The reason is the exception's own message, as plain text.
  expect(notice.querySelector('code')?.textContent).toMatch(/undefined|null/);
  expect(within(notice).getByRole('button', { name: '重试这一页' })).toBeTruthy();
  expect(within(notice).getByRole('link', { name: '回到首页' }).getAttribute('href')).toBe('/');
  const nav = screen.getByRole('navigation', { name: '主导航' });
  expect(within(nav).getAllByRole('link').map((link) => link.textContent)).toEqual(['舞台', '项目', '单卡', '审核', '作业']);
  expect(screen.getByRole('link', { name: '提出目标' })).toBeTruthy();
});

it('reads the record again on retry and shows the page once it can be drawn', async () => {
  let answer: unknown = broken;
  open('/stages/s1', () => answer);
  const retry = await screen.findByRole('button', { name: '重试这一页' });
  // Still broken: the notice comes back instead of a blank page.
  fireEvent.click(retry);
  expect(await screen.findByRole('button', { name: '重试这一页' })).toBeTruthy();
  answer = sound;
  fireEvent.click(screen.getByRole('button', { name: '重试这一页' }));
  expect(await screen.findByRole('heading', { name: '门槛' })).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
});

it('clears the notice when another page is opened, and catches the same page again', async () => {
  open('/stages/s1', () => broken);
  await screen.findByRole('alert');
  fireEvent.click(within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', { name: '项目' }));
  expect(await screen.findByRole('heading', { name: '项目', level: 1 })).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
  // Going back draws the stage from the same record and is caught again.
  window.history.back();
  await waitFor(() => expect(window.location.pathname).toBe('/stages/s1'));
  expect(await screen.findByRole('alert')).toBeTruthy();
  // The link home leaves it as well.
  fireEvent.click(screen.getByRole('link', { name: '回到首页' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect(window.location.pathname).toBe('/');
});

it('says the same in English', async () => {
  setLangForTest('en');
  open('/stages/s1', () => broken);
  const notice = await screen.findByRole('alert');
  expect(within(notice).getByText('This page could not be shown')).toBeTruthy();
  expect(within(notice).getByRole('button', { name: 'Try this page again' })).toBeTruthy();
  const text = (notice.textContent ?? '').replace(notice.querySelector('code')?.textContent ?? '', '');
  expect(text).not.toMatch(/[一-鿿：；，、（）。「」]/);
});
