// The five workbench additions in a real browser, against the loopback service with Git,
// SQLite and substitute model CLIs (e2e/workbench_env.py):
//   1 首页待办  2 作业进展和恢复  3 审查意见  4 仓库首次接入（含目标草稿）  5 验收交付摘要
// Each new panel is also looked at in the dark theme and at 1024 and 768 pixels, and the same
// records are opened once more through a read-only archive and through the recorded demo.
// Screenshots of the key pages are written next to the run records.

import { execFileSync, spawn } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test, type Locator, type Page } from '@playwright/test';
import type { Attention, Delivery, IntegrationRecord, Job, JobActivity, ReviewFindings } from '../src/api/types';
import { act, NetworkLog, OfflineEnv, pick, readback, RECORDS, refreshEnvironments, REPO, settled } from './support';
import { claim, directStage, goalToPlan, integrationReview, leavesToIntegration } from './flow';

test.use({ deviceScaleFactor: 2, permissions: ['clipboard-read', 'clipboard-write'] });

const SHOTS = resolve(RECORDS, 'workbench-shots');
const DIST = resolve(REPO, 'frontend/dist');

async function shot(target: Page | Locator, name: string): Promise<void> {
  mkdirSync(SHOTS, { recursive: true });
  const path = resolve(SHOTS, `${name}.png`);
  if ('goto' in target) await target.screenshot({ path, fullPage: true });
  else await target.screenshot({ path });
}

/** Uncaught exceptions and console errors of the page, to be read after a stretch of use. */
function watchErrors(page: Page) {
  const thrown: string[] = [];
  const logged: string[] = [];
  page.on('pageerror', (error) => thrown.push(String(error)));
  page.on('console', (message) => { if (message.type() === 'error') logged.push(message.text()); });
  return {
    thrown, logged,
    /** Console errors other than the browser's own line for a non-2xx answer. */
    unexpected: (): string[] => logged.filter((text) => !text.startsWith('Failed to load resource')),
    clear: (): void => { thrown.length = 0; logged.length = 0; },
  };
}

interface LowContrast {
  text: string;
  where: string;
  color: string;
  background: string;
  ratio: number;
  needed: number;
}

/** Text in a panel whose contrast against what is painted behind it is under WCAG AA
 *  (4.5:1, or 3:1 for large text). Disabled controls are exempt. */
async function lowContrast(panel: Locator): Promise<LowContrast[]> {
  return panel.evaluate((root) => {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
    const rgba = (value: string): number[] => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = '#000';
      ctx.fillStyle = value;
      ctx.fillRect(0, 0, 1, 1);
      const d = ctx.getImageData(0, 0, 1, 1).data;
      return [d[0]!, d[1]!, d[2]!, d[3]! / 255];
    };
    const over = (top: number[], bottom: number[]): number[] => [0, 1, 2].map((i) => top[i]! * top[3]! + bottom[i]! * (1 - top[3]!));
    const luminance = (c: number[]): number => {
      const [r, g, b] = c.slice(0, 3).map((v) => { const x = v / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; });
      return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
    };
    const page = rgba(getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() || '#fff');
    const behind = (el: Element): number[] => {
      const layers: number[][] = [];
      for (let node: Element | null = el; node; node = node.parentElement) {
        const c = rgba(getComputedStyle(node).backgroundColor);
        if (c[3]! > 0) layers.push(c);
        if (c[3] === 1) break;
      }
      return layers.reverse().reduce((base, layer) => over(layer, base), page.slice(0, 3));
    };
    const found: LowContrast[] = [];
    const seen = new Set<Element>();
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = (node.textContent ?? '').trim();
      const el = node.parentElement;
      if (!text || !el || seen.has(el)) continue;
      seen.add(el);
      const style = getComputedStyle(el);
      if (style.visibility === 'hidden' || !el.getClientRects().length || el.closest('button:disabled, fieldset:disabled')) continue;
      // What a closed <details> holds is not painted, and its styles are not kept up to date.
      const folded = el.closest('details:not([open])');
      if (folded && !el.closest('summary')) continue;
      const background = behind(el);
      const color = over(rgba(style.color), background);
      const [a, b] = [luminance(color), luminance(background)].sort((x, y) => y - x);
      const ratio = (a! + 0.05) / (b! + 0.05);
      const size = parseFloat(style.fontSize);
      const needed = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700) ? 3 : 4.5;
      if (ratio < needed) {
        found.push({
          text: text.slice(0, 32), where: `${el.tagName.toLowerCase()}${el.className ? `.${String(el.className).replace(/\s+/g, '.')}` : ''}`,
          color: style.color, background: `rgb(${background.map(Math.round).join(', ')})`, ratio: Math.round(ratio * 100) / 100, needed,
        });
      }
    }
    return found;
  });
}

/** What sticks out of a panel, or of the viewport, at the current width. */
async function overflowing(panel: Locator): Promise<string[]> {
  return panel.evaluate((root) => {
    const viewport = document.documentElement.clientWidth;
    const box = root.getBoundingClientRect();
    const out: string[] = [];
    if (box.right > viewport + 1 || box.left < -1) out.push(`panel spans ${Math.round(box.left)}..${Math.round(box.right)} in a viewport of ${viewport}`);
    // Content inside its own scroll box (a wide log, a long event list) is allowed to be wider.
    const scrolls = (node: Element): boolean => {
      for (let p = node.parentElement; p && p !== root.parentElement; p = p.parentElement) {
        if (['auto', 'scroll', 'hidden'].includes(getComputedStyle(p).overflowX)) return true;
      }
      return false;
    };
    for (const node of root.querySelectorAll('*')) {
      const r = node.getBoundingClientRect();
      if (!r.width || scrolls(node)) continue;
      if (r.right > box.right + 1) {
        out.push(`${node.tagName.toLowerCase()}.${String(node.className)} "${(node.textContent ?? '').trim().slice(0, 24)}" ends at ${Math.round(r.right)}, panel at ${Math.round(box.right)}`);
      }
    }
    return out.slice(0, 6);
  });
}

type Look = { light: LowContrast[]; dark: LowContrast[]; pageOverflow: Record<number, number> };

/** Looks at panels of the current page in the dark theme (a screenshot of each, and the text
 *  contrast in both themes), then at 1024 and 768 pixels, where nothing may stick out. */
async function inspect(page: Page, name: string, panels: Locator[], looks: Record<string, Look>): Promise<void> {
  const contrast = async (): Promise<LowContrast[]> => (await Promise.all(panels.map(lowContrast))).flat();
  const light = await contrast();
  // The suite runs with reduced motion (playwright.config.ts), so a theme switch is immediate;
  // each screenshot still waits for the page to be painted in that theme.
  const painted = (): Promise<void> => page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
  await painted();
  for (const [i, panel] of panels.entries()) await shot(panel, `${name}-dark${panels.length > 1 ? `-${i + 1}` : ''}`);
  const dark = await contrast();
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light'; });
  await painted();
  const pageOverflow: Record<number, number> = {};
  for (const width of [1024, 768]) {
    await page.setViewportSize({ width, height: 900 });
    for (const panel of panels) expect(await overflowing(panel), `${name} at ${width}px`).toEqual([]);
    pageOverflow[width] = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (width === 768) await shot(page, `${name}-768`);
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await painted();
  looks[name] = { light, dark, pageOverflow };
}

/** A second, read-only service on a copy of the workbench database (docs/workbench.md,
 *  "Open a read-only archive"). No job runs while the files are copied. */
async function startArchive(repository: string): Promise<{ url: string; stop: () => Promise<void> }> {
  const root = dirname(repository);
  const archive = resolve(root, 'archive-copy');
  mkdirSync(archive);
  for (const name of ['reviews.sqlite3', 'reviews.sqlite3-wal']) {
    if (existsSync(resolve(root, 'data', name))) copyFileSync(resolve(root, 'data', name), resolve(archive, name));
  }
  const proc = spawn('uv', ['run', 'python', '-m', 'multi_agent.web', '--repository', repository, '--data-dir', resolve(root, 'archive-view'),
    '--read-only-database', resolve(archive, 'reviews.sqlite3'), '--port', '0', '--frontend-dist', DIST], { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  proc.stderr.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
  const url = await new Promise<string>((res, rej) => {
    createInterface({ input: proc.stdout }).once('line', (line) => res(line.trim()));
    proc.once('exit', (code) => rej(new Error(`archive service exited with ${code}: ${stderr}`)));
    setTimeout(() => rej(new Error(`archive service printed no address: ${stderr}`)), 60_000);
  });
  return {
    url,
    stop: async () => {
      if (proc.exitCode !== null) return;
      const done = new Promise((r) => proc.once('exit', r));
      proc.kill('SIGINT');
      await Promise.race([done, new Promise((r) => setTimeout(r, 10_000))]);
    },
  };
}

const inbox = (page: Page): Locator => page.getByRole('region', { name: '需要我处理' });
const inboxRow = (page: Page, label: string): Locator =>
  inbox(page).locator('.attention-row').filter({ has: page.getByRole('link', { name: label, exact: true }) });
const section = (page: Page, title: string): Locator =>
  page.locator('section.section').filter({ has: page.getByRole('heading', { name: title, exact: true }) });

async function api<T>(page: Page, base: string, path: string): Promise<T> {
  const response = await page.request.get(`${base}/api/${path}`);
  expect(response.ok(), path).toBe(true);
  return (await response.json()) as T;
}

async function home(page: Page, base: string): Promise<void> {
  await page.goto(`${base}/`);
  await expect(inbox(page)).toBeVisible();
  await settled(page);
}

/** Advance a branch of the temporary repository by one commit with the same tree. */
function moveBranch(repository: string, ref: string): void {
  const git = (...args: string[]): string => execFileSync('git', ['-c', 'user.name=E2E', '-c', 'user.email=e2e@example.invalid', ...args], {
    cwd: repository, encoding: 'utf8', env: { ...process.env, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1' },
  }).trim();
  const head = git('rev-parse', '--verify', ref);
  const moved = git('commit-tree', `${head}^{tree}`, '-p', head, '-m', 'Move the target branch');
  git('update-ref', ref, moved, head);
}

async function chooseBranch(page: Page, name: string): Promise<void> {
  const select = page.getByLabel('或选分支');
  const value = await select.locator('option').filter({ hasText: name }).first().getAttribute('value');
  await select.selectOption(value!);
}

test('首次接入：按选中的提交检查规则，草稿留在本机，未登录也能直接确认原文', async ({ page }) => {
  const env = await OfflineEnv.start('workbench');
  const net = new NetworkLog(page);
  const errors = watchErrors(page);
  const looks: Record<string, Look> = {};
  page.on('dialog', (d) => void d.accept());
  try {
    await page.goto(`${env.url}/new`);
    const panel = section(page, '检查接入');
    const commands = panel.getByRole('list', { name: '已登记的命令' });

    // HEAD: the commands registered in that commit, and no provider confirmed yet.
    await expect(commands.getByText('build', { exact: true })).toBeVisible();
    await expect(commands.getByText('test', { exact: true })).toBeVisible();
    await expect(panel.getByText('就是当前 HEAD')).toBeVisible();
    await expect(panel.getByText('还没有确认登录')).toHaveCount(2);
    await expect(panel.getByText('现在就可以写下目标并直接确认原文；规则和登录可以之后再补。')).toBeVisible();
    await shot(page, 'p4-1-head-with-rules');

    // A commit without rules: the template can be copied; nothing is written or started.
    await chooseBranch(page, 'legacy/no-rules');
    await expect(panel.getByText('这个提交里没有规则文件')).toBeVisible();
    await expect(panel.getByText('不是当前 HEAD')).toBeVisible();
    await expect(commands).toHaveCount(0);
    await expect(panel.getByLabel('规则文件模板')).toContainText('version: 1');
    await panel.getByRole('button', { name: '复制模板' }).click();
    await expect(panel.getByText('已复制到剪贴板。')).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('commands:');
    await shot(page, 'p4-2-commit-without-rules');
    await inspect(page, 'p4-2-commit-without-rules', [panel], looks);

    // A commit whose rules file is invalid shows the concrete error.
    await chooseBranch(page, 'setup/invalid-rules');
    await expect(panel.getByText('规则文件无效')).toBeVisible();
    await expect(panel.locator('.facts-grid .action-why.problem').first()).not.toBeEmpty();
    await shot(panel, 'p4-3-invalid-rules');

    // A baseline that does not resolve is refused by the check and blocks confirmation.
    await page.getByLabel('基线提交').fill('no-such-branch');
    await expect(panel.getByText(/这个基线检查不了/)).toBeVisible();
    await page.getByLabel('原始目标').fill('A draft that survives a reload.');
    await expect(page.getByRole('button', { name: '直接确认为目标' })).toBeDisabled();
    await shot(page, 'p4-4-unresolved-baseline');

    // Back on the configured commit the registered commands are shown again.
    await chooseBranch(page, 'main');
    await expect(commands.getByText('build', { exact: true })).toBeVisible();
    await expect(panel.getByText('就是当前 HEAD')).toBeVisible();

    // The unsubmitted goal comes back after a reload, leaving the page, and a language switch.
    const goal = page.getByLabel('原始目标');
    await page.reload();
    await expect(goal).toHaveValue('A draft that survives a reload.');
    await expect(page.getByText(/已恢复这个仓库上次没提交的目标/)).toBeVisible();
    await shot(page, 'p4-5-draft-restored');
    await page.getByRole('link', { name: '作业', exact: true }).click();
    await page.locator('.app-tools').getByRole('link', { name: '提出目标', exact: true }).click();
    await expect(goal).toHaveValue('A draft that survives a reload.');
    await page.getByRole('radio', { name: 'English' }).click();
    await expect(page.getByLabel('Original goal')).toHaveValue('A draft that survives a reload.');
    await expect(page.getByRole('heading', { name: 'Setup check' })).toBeVisible();
    await shot(page, 'p4-6-english');
    await page.getByRole('radio', { name: '中文' }).click();
    await expect(goal).toHaveValue('A draft that survives a reload.');

    // Nothing so far has started a job.
    await net.ready();
    expect(net.posts()).toEqual([]);
    expect((await api<{ items: Job[] }>(page, env.url, 'jobs')).items).toEqual([]);

    // With a valid baseline the original text is confirmed before any provider is checked.
    await expect(panel.getByText('还没有确认登录')).toHaveCount(2);
    await act(page, '直接确认为目标');
    await expect(page).toHaveURL(/\/projects\//);
    await page.goto(`${env.url}/new`);
    await expect(goal).toHaveValue('');
    await expect(page.getByText(/已恢复这个仓库上次没提交的目标/)).toHaveCount(0);

    // After the environment checks the panel shows the observed sign-in.
    await refreshEnvironments(page);
    await expect(panel.getByText('已登录')).toHaveCount(2);
    await shot(panel, 'p4-7-providers-signed-in');
    await inspect(page, 'p4-7-providers-signed-in', [panel], looks);

    // A consultation that fails keeps the draft and leads back through the inbox.
    await goal.fill('FAIL-CONSULT Build the quarterly report.');
    await pick(page, '第一位顾问', 'claude-fable-5-2', 'low');
    await act(page, '请顾问完善');
    await expect(page).toHaveURL(/\/consultations\//);
    const consultationId = decodeURIComponent(new URL(page.url()).pathname.split('/').pop()!);
    await page.goto(`${env.url}/new`);
    await expect(goal).toHaveValue('FAIL-CONSULT Build the quarterly report.');
    await goal.fill('Another idea that is not submitted yet.');

    await home(page, env.url);
    const failed = inboxRow(page, 'FAIL-CONSULT Build the quarterly report.');
    await expect(failed).toContainText('需求咨询失败，重新咨询或直接确认原文');
    await shot(inbox(page), 'p1-0-failed-consultation');
    await failed.getByRole('link').click();
    await expect(page).toHaveURL(new RegExp(`/new\\?consultation=${consultationId}$`));
    await expect(goal).toHaveValue('FAIL-CONSULT Build the quarterly report.');
    await expect(page.getByText(/从没有完成的需求咨询带入了当时的原文、基线和项目/)).toBeVisible();
    await shot(page, 'p4-8-from-failed-consultation');
    await act(page, '直接确认为目标');
    await expect(page).toHaveURL(/\/projects\//);

    // The failed consultation left the inbox; the other draft of this repository is still there.
    await home(page, env.url);
    await expect(inboxRow(page, 'FAIL-CONSULT Build the quarterly report.').filter({ hasText: '需求咨询失败' })).toHaveCount(0);
    await page.goto(`${env.url}/new`);
    await expect(goal).toHaveValue('Another idea that is not submitted yet.');

    expect(net.lockViolations()).toEqual([]);
    // The only failed requests are the ones made on purpose (the baseline that does not resolve).
    expect(errors.thrown).toEqual([]);
    expect(errors.unexpected()).toEqual([]);
    await net.record('workbench-onboarding', { result: 'passed', consultation_id: consultationId, looks });
  } catch (error) {
    await net.record('workbench-onboarding', { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});

test('空仓库：提示先提交基线，不能确认目标', async ({ page }) => {
  const env = await OfflineEnv.start('workbench-empty');
  try {
    await page.goto(`${env.url}/new`);
    const panel = section(page, '检查接入');
    await expect(panel.getByText('仓库还没有提交。先在仓库里提交一次，作为基线。')).toBeVisible();
    await page.getByLabel('原始目标').fill('A goal for a repository without commits.');
    await expect(page.getByRole('button', { name: '直接确认为目标' })).toBeDisabled();
    await expect(page.getByText('仓库还没有提交，先提交一个基线。').first()).toBeVisible();
    await shot(page, 'p4-9-empty-repository');
    await page.goto(`${env.url}/`);
    await expect(page.getByText('还没有项目')).toBeVisible();
    await shot(page, 'p1-9-empty-home');
  } finally {
    await env.stop();
  }
});

test('待办、作业恢复、审查意见和交付摘要：从两个可领取阶段走到重新验收', async ({ page }) => {
  const env = await OfflineEnv.start('workbench');
  const net = new NetworkLog(page);
  const errors = watchErrors(page);
  const looks: Record<string, Look> = {};
  page.on('dialog', (d) => void d.accept());
  try {
    await goalToPlan(page, env.url);
    const planUrl = page.url();

    // ---- 1 首页待办: two stages of one plan can be claimed, each on its own row.
    await home(page, env.url);
    await expect(inboxRow(page, 'Stage amount')).toContainText('可以领取这个阶段');
    await expect(inboxRow(page, 'Stage report')).toContainText('可以领取这个阶段');
    await shot(page, 'p1-1-two-claimable-stages');
    await inboxRow(page, 'Stage report').getByRole('link').click();
    await expect(page).toHaveURL(/\/plans\/[^/]+\?stage=report$/);
    const located = page.locator('.stage-list > li[aria-current="location"]');
    await expect(located).toContainText('Stage report');
    await expect(located.getByRole('button', { name: '领取这个阶段' })).toBeVisible();
    await shot(page, 'p1-2-plan-located-stage');

    // Claiming it fails in planning: its row turns into the failed stage, the other stays.
    await pick(located, '阶段负责人', 'gpt-6.2-sol', 'low');
    await pick(located, '卡片复核', 'claude-opus-5-5', 'low');
    await act(page, '领取这个阶段', located);
    await home(page, env.url);
    await expect(inboxRow(page, 'Stage amount')).toContainText('可以领取这个阶段');
    await expect(inboxRow(page, 'Stage report')).toHaveCount(1);
    await expect(inboxRow(page, 'Stage report')).toContainText('规划没有完成，查看并恢复');
    await shot(inbox(page), 'p1-3-failed-stage-and-claimable');
    await inspect(page, 'p1-3-failed-stage-and-claimable', [inbox(page)], looks);

    // Delivering the prerequisite brings up the stage that depends on it.
    await page.goto(planUrl);
    await claim(page, 'amount', 'direct');
    await directStage(page);
    await home(page, env.url);
    await expect(inboxRow(page, 'Stage amount')).toHaveCount(0);
    await expect(inboxRow(page, 'Stage summary')).toContainText('可以领取这个阶段');
    await page.goto(planUrl);
    await claim(page, 'summary', 'delegate');
    await leavesToIntegration(page);
    const integrationUrl = page.url();
    const integrationId = decodeURIComponent(new URL(integrationUrl).pathname.split('/').pop()!);
    await act(page, '运行测试');

    // The tested candidate waits for reviewers; the inbox leads back to it (same page, no reload).
    await page.locator('.app-nav').getByRole('link', { name: '舞台', exact: true }).click();
    const toReview = inbox(page).locator('.attention-row').filter({ hasText: '选审核席位，开始集成审核' });
    await expect(toReview).toHaveCount(1);
    await toReview.getByRole('link').click();
    await expect(page).toHaveURL(integrationUrl);

    // ---- 2 作业进展: a two-seat review; the first report arrives, the second seat is still running.
    await pick(page, '审核席位 1', 'gpt-6.2-sol', 'low');
    await pick(page, '审核席位 2', 'claude-opus-5-5', 'low');
    const auto = page.getByLabel(/自动返工/);
    if (await auto.isChecked()) await auto.uncheck();
    await page.getByRole('button', { name: '开始审核', exact: true }).click();
    await page.locator('.job-bar').getByRole('link', { name: '作业详情' }).click();
    await expect(page).toHaveURL(/\/jobs\//);
    const jobUrl = page.url();
    const jobId = decodeURIComponent(new URL(jobUrl).pathname.split('/').pop()!);
    const seats = section(page, '审核席位');
    await expect(seats.locator('.seat-list li').filter({ hasText: 'seat-1' })).toContainText('报告有效', { timeout: 120_000 });
    await expect(seats.locator('.seat-list li').filter({ hasText: 'seat-2' })).toContainText('进行中');
    const events = page.getByRole('list', { name: '最近的事件' });
    await expect(events.locator('li').filter({ hasText: 'seat-2' }).filter({ hasText: '模型有输出' })).toHaveCount(1, { timeout: 120_000 });
    await expect(section(page, '执行者').locator('.job-actor').filter({ hasText: 'seat-2' })).toContainText('进行中');
    // Repeated polls never show an event twice.
    await page.waitForTimeout(3500);
    const live = await api<JobActivity>(page, env.url, `jobs/${jobId}/activity`);
    await expect(events.locator('li')).toHaveCount(live.events.length);
    expect(new Set(live.events.map((e) => e.sequence)).size).toBe(live.events.length);
    expect(live.recovery).toEqual([]);
    await expect(page.getByRole('heading', { name: '可以继续的动作' })).toHaveCount(0);
    await shot(page, 'p2-1-live-two-seats');

    // While the job holds the worktree the service confirms only part of the list. The row that
    // needs live evidence stays as it was last read, marked for refresh.
    await page.locator('.app-nav').getByRole('link', { name: '舞台', exact: true }).click();
    expect((await api<Attention>(page, env.url, 'attention')).complete).toBe(false);
    await expect(inbox(page).getByText(/这次只读到一部分待办/)).toBeVisible();
    await expect(inboxRow(page, 'Stage report')).toContainText('规划没有完成，查看并恢复');
    await expect(inboxRow(page, 'Stage report')).not.toContainText('待刷新');
    await expect(toReview).toContainText('待刷新');
    await shot(inbox(page), 'p1-4-partial-while-job-runs');

    // The service is stopped hard in the middle of the review and comes back.
    await env.hardRestart();
    await page.goto(jobUrl);
    await expect(page.locator('.page-aside .badge')).toContainText('已中断');
    await expect(seats.locator('.seat-list li').filter({ hasText: 'seat-1' })).toContainText('报告有效');
    await expect(seats.locator('.seat-list li').filter({ hasText: 'seat-2' })).toContainText('中断时还在运行，没有报告');
    await expect(section(page, '执行者').locator('.job-actor').filter({ hasText: 'seat-2' })).toContainText('中断前最后观测于');
    const recover = page.getByRole('button', { name: '接管并补跑中断的席位（seat-2）' });
    await expect(recover).toBeVisible();
    const interrupted = await api<JobActivity>(page, env.url, `jobs/${jobId}/activity`);
    expect(interrupted.recovery.map((r) => r.args.seat_keys)).toEqual([['seat-2']]);
    await shot(page, 'p2-2-interrupted-keeps-first-report');
    await inspect(page, 'p2-2-interrupted-job', [section(page, '执行者'), seats, section(page, '可以继续的动作'), section(page, '最近的事件')], looks);

    // Closed and opened again: the same report, the same single missing seat.
    await page.goto(`${env.url}/jobs`);
    await page.goto(jobUrl);
    await expect(seats.locator('.seat-list li').filter({ hasText: 'seat-1' })).toContainText('报告有效');
    await expect(recover).toBeVisible();
    const roundId = interrupted.reviews[0]!.round_id;
    const snapshotId = interrupted.reviews[0]!.snapshot_id;
    await act(page, '接管并补跑中断的席位（seat-2）');
    await page.goto(jobUrl);
    await expect(section(page, '可以继续的动作')).toContainText(/没有可以直接继续的动作|没有需要继续的动作/);
    const repaired = await api<JobActivity>(page, env.url, `jobs/${jobId}/activity`);
    expect(repaired.recovery).toEqual([]);
    expect(repaired.reviews[0]).toMatchObject({ round_id: roundId, snapshot_id: snapshotId });
    expect(repaired.reviews[0]!.seats.map((s) => [s.seat, s.state, s.attempt])).toEqual([['seat-1', 'succeeded', 1], ['seat-2', 'succeeded', 2]]);
    await shot(page, 'p2-3-after-recovery');

    // ---- 3 审查意见: both seats used the same finding id.
    await page.goto(`${env.url}/reviews/${encodeURIComponent(roundId)}`);
    const cards = page.locator('.finding-card');
    await expect(cards).toHaveCount(2);
    const first = cards.nth(0);
    const second = cards.nth(1);
    await expect(first).toHaveAttribute('data-seat', 'seat-1');
    await expect(second).toHaveAttribute('data-seat', 'seat-2');
    await expect(first).toHaveAttribute('data-finding', 'status-blocker');
    await expect(second).toHaveAttribute('data-finding', 'status-blocker');
    await expect(page.getByText('各席都独立报告了这个位置（相同证据）。')).toBeVisible();

    // A decision on seat-2's finding is recorded under seat-2 only.
    await second.getByText('记录你的判断').click();
    await second.getByRole('radio', { name: '存疑' }).click();
    await second.getByLabel('判断的理由').fill('This branch is already excluded upstream.');
    await second.getByRole('button', { name: '记录判断' }).click();
    await expect(second.getByText('This branch is already excluded upstream.')).toBeVisible();
    await expect(first.getByText('This branch is already excluded upstream.')).toHaveCount(0);

    // A follow-up goes to the reviewer of that seat and the answer hangs under the same finding.
    await second.getByText('追问原审核者').click();
    await second.getByLabel('要问的问题').fill('Which input shows the draft status?');
    await act(page, '提交追问', second);
    await expect(second.getByText('Which input shows the draft status?')).toBeVisible();
    await expect(second.getByText(/The frozen line still returns the draft status/)).toBeVisible();
    await expect(first.locator('.finding-thread')).toHaveCount(0);

    // A different decision on the other seat's finding; both stay apart after a reload.
    await first.getByText('记录你的判断').click();
    await first.getByRole('radio', { name: '确认存在' }).click();
    await first.getByLabel('判断的理由').fill('The summary really shows draft.');
    await first.getByRole('button', { name: '记录判断' }).click();
    await expect(first.getByText('The summary really shows draft.')).toBeVisible();
    await page.reload();
    await expect(first.getByText('The summary really shows draft.')).toBeVisible();
    await expect(second.getByText('This branch is already excluded upstream.')).toBeVisible();
    await expect(second.getByText(/The frozen line still returns the draft status/)).toBeVisible();
    const stored = await api<ReviewFindings>(page, env.url, `reviews/${encodeURIComponent(roundId)}/findings`);
    expect(stored.groups[0]!.members.map((m) => [m.anchor.seat, m.anchor.finding_id, m.activity.map((a) => a.kind)])).toEqual([
      ['seat-1', 'status-blocker', ['decision']],
      ['seat-2', 'status-blocker', ['decision', 'answer']],
    ]);
    // The verdict of the round is the reviewers', whatever the user recorded.
    expect(stored.status).toBe('needs_rework');
    await expect(section(page, '意见').getByText('需要返工', { exact: true })).toBeVisible();
    await shot(page, 'p3-1-findings-same-id-two-seats');

    // The frozen line the finding cites is still one click away.
    await second.getByRole('link', { name: 'frontend/status.js:1', exact: true }).click();
    await expect(page).toHaveURL(/\/snapshots\/.+\/file\?/);
    await expect(page.locator('tr#F1')).toHaveClass(/hot/);
    await page.goBack();

    // A linked correction starts from the reviewed commit and is bound to seat-2's finding.
    await second.getByRole('link', { name: '发起关联修复' }).click();
    await expect(page).toHaveURL(/\/task-plans\/new\?review=/);
    await expect(page.getByLabel('基线提交')).toHaveValue(stored.source.correction_baseline!);
    await expect(page.getByLabel('基线提交')).not.toBeEditable();
    await expect(section(page, '来源意见')).toContainText('seat-2');
    await shot(page, 'p3-2-linked-correction-form');
    // The substitute Codex catalog has no default review model, so one is chosen here.
    await page.getByLabel('指定复核模型', { exact: true }).check();
    await pick(page, '规划复核', 'gpt-6.2-sol', 'low');
    await act(page, '开始单卡规划');
    await expect(page).toHaveURL(/\/task-plans\/(?!new)/);
    const planningRunId = decodeURIComponent(new URL(page.url()).pathname.split('/').pop()!);
    await page.goto(`${env.url}/reviews/${encodeURIComponent(roundId)}`);
    await expect(second.getByRole('link', { name: /单卡规划/ })).toHaveAttribute('href', `/task-plans/${planningRunId}`);
    await expect(first.getByRole('link', { name: /单卡规划/ })).toHaveCount(0);

    // Cross-confirmation: each seat's answer sits on the finding of the seat it was asked about.
    await page.goto(integrationUrl);
    await act(page, '请两席交叉确认');
    await page.goto(`${env.url}/reviews/${encodeURIComponent(roundId)}`);
    await expect(first.getByRole('list', { name: '交叉确认' })).toContainText('seat-2');
    await expect(second.getByRole('list', { name: '交叉确认' })).toContainText('seat-1');
    await shot(page, 'p3-3-cross-confirmation-and-thread');
    await second.getByText('记录你的判断').click();
    await second.getByText('追问原审核者').click();
    await inspect(page, 'p3-3-findings', [section(page, '意见')], looks);

    // The integration page says who owns the blockers before the rework is offered.
    await page.goto(integrationUrl);
    const rework = page.getByLabel('返工资格');
    await expect(rework).toContainText('可以返工');
    await expect(rework.getByRole('list', { name: '阻塞意见的归属' }).locator('li')).toHaveCount(2);
    await expect(rework.getByRole('link', { name: 'T-0800' }).first()).toBeVisible();
    await shot(page, 'p3-4-rework-eligibility');
    await inspect(page, 'p3-4-rework-eligibility', [rework], looks);
    await act(page, '按阻塞意见返工（每条谱系一次）');
    await expect(page).not.toHaveURL(integrationUrl);
    await expect(page).toHaveURL(/\/integrations\//);
    const childUrl = page.url();
    const childId = decodeURIComponent(new URL(childUrl).pathname.split('/').pop()!);

    // On the reworked parent the refusal the command would give is shown, and no button.
    await page.goto(integrationUrl);
    await expect(rework).toContainText('现在不能返工');
    await expect(rework).toContainText('This integration lineage has already reserved its rework.');
    await expect(page.getByRole('button', { name: '按阻塞意见返工（每条谱系一次）' })).toHaveCount(0);
    await shot(rework, 'p3-5-rework-refusal-reason');

    // ---- 1 again: a failed stage and a candidate waiting for acceptance, side by side.
    await page.goto(childUrl);
    if (await page.getByRole('button', { name: '运行测试', exact: true }).count()) await integrationReview(page);
    await expect(page.getByText('审核通过').first()).toBeVisible();
    await expect(page.getByText('交付摘要：待验收。接受后可以在这里预览和复制。')).toBeVisible();
    expect((await page.request.get(`${env.url}/api/integrations/${encodeURIComponent(childId)}/delivery`)).status()).toBe(409);
    await home(page, env.url);
    const waiting = inbox(page).locator('.attention-row').filter({ hasText: '测试和审核都通过，可以接受' });
    await expect(waiting).toHaveCount(1);
    await expect(inboxRow(page, 'Stage report')).toContainText('规划没有完成，查看并恢复');
    await shot(page, 'p1-5-failed-stage-and-acceptance');
    await waiting.getByRole('link').click();
    await expect(page).toHaveURL(childUrl);
    await act(page, '接受');
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();
    await home(page, env.url);
    await expect(inbox(page).locator('.attention-row').filter({ hasText: '测试和审核都通过，可以接受' })).toHaveCount(0);
    await expect(inboxRow(page, 'Stage report')).toContainText('规划没有完成，查看并恢复');
    await shot(inbox(page), 'p1-6-after-acceptance');

    // ---- 5 验收交付: the summary of the accepted candidate, copied and pasted.
    await page.goto(childUrl);
    const accepted = await readback<IntegrationRecord>(page, `${env.url}/api/resources/integration/${encodeURIComponent(childId)}`);
    const summary = page.getByLabel('交付摘要', { exact: true });
    const receipt = await api<Delivery>(page, env.url, `integrations/${encodeURIComponent(childId)}/delivery`);
    expect(receipt.receipt.candidate.commit).toBe(accepted.candidate!.commit);
    await expect(summary).toContainText(receipt.receipt.candidate.commit);
    await expect(summary).toContainText(receipt.receipt.target_baseline);
    await expect(summary).toContainText(receipt.receipt.test_evidence_hash);
    await expect(summary).toContainText(receipt.receipt.review_checkpoint_hash);
    await expect(summary.getByRole('link', { name: '审核依据' })).toHaveAttribute('href', receipt.links.review);
    await summary.getByRole('button', { name: '复制交付摘要' }).click();
    await expect(summary.getByText('已复制到剪贴板。')).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(receipt.markdown);
    await shot(page, 'p5-1-delivery-summary');
    await inspect(page, 'p5-1-delivery-summary', [summary], looks);

    // Pasted into a text box: the full SHA, the test results and the review evidence are there.
    await page.goto(`${env.url}/reviews/new`);
    const box = page.locator('textarea').first();
    await box.focus();
    await page.keyboard.press('ControlOrMeta+V');
    const pasted = await box.inputValue();
    expect(pasted).toContain(receipt.receipt.candidate.commit);
    expect(pasted).toContain(`测试第 ${receipt.receipt.test_attempt} 次通过`);
    for (const command of receipt.tests) expect(pasted).toContain(`退出码 ${command.exit_code}`);
    expect(pasted).toContain(receipt.receipt.review_round_id);
    expect(pasted).toContain(receipt.receipt.review_checkpoint_hash);
    await shot(page, 'p5-2-pasted-summary');

    // The target branch moves and a rebuilt candidate is accepted: the earlier summary stays.
    moveBranch(env.repository!, receipt.receipt.target_ref);
    await page.goto(childUrl);
    await expect(page.getByText('目标分支移动过，需要重建')).toBeVisible();
    await expect(summary).toContainText(receipt.receipt.candidate.commit);
    await shot(page, 'p5-3-target-moved-summary-kept');
    await act(page, '在新的目标上重建');
    await expect(page).not.toHaveURL(childUrl);
    const rebuiltUrl = page.url();
    const rebuiltId = decodeURIComponent(new URL(rebuiltUrl).pathname.split('/').pop()!);
    await integrationReview(page);
    await act(page, '接受');
    await expect(page.getByText('已接受', { exact: true })).toBeVisible();
    const again = await api<Delivery>(page, env.url, `integrations/${encodeURIComponent(rebuiltId)}/delivery`);
    expect(again.receipt.acceptance_id).not.toBe(receipt.receipt.acceptance_id);
    expect(again.receipt.target_baseline).not.toBe(receipt.receipt.target_baseline);
    await expect(summary).toContainText(again.receipt.candidate.commit);
    await shot(page, 'p5-4-rebuilt-summary');
    await page.goto(childUrl);
    await expect(summary).toContainText(receipt.receipt.candidate.commit);
    await expect(summary).toContainText(receipt.receipt.acceptance_id);
    expect(await api<Delivery>(page, env.url, `integrations/${encodeURIComponent(childId)}/delivery`)).toEqual(receipt);

    expect(net.lockViolations()).toEqual([]);
    expect(errors.thrown).toEqual([]);
    expect(errors.unexpected()).toEqual([]);

    // ---- The same records through a read-only archive: everything can be read, nothing offered to write.
    const archive = await startArchive(env.repository!);
    const archived: Record<string, string[]> = {};
    try {
      const open = async (name: string, path: string): Promise<void> => {
        errors.clear();
        await page.goto(`${archive.url}${path}`);
        await expect(page.getByText('只读', { exact: true })).toBeVisible();
        await settled(page);
        archived[name] = [...errors.logged];
        expect(errors.thrown, name).toEqual([]);
      };
      await open('home', '/');
      await expect(page).toHaveURL(/\/archive$/);
      await expect(inbox(page)).toHaveCount(0);
      await open('new-goal', '/new');
      await expect(page.getByText('工作区为只读')).toBeVisible();
      await expect(page.getByRole('heading', { name: '检查接入' })).toHaveCount(0);

      await open('review', `/reviews/${encodeURIComponent(roundId)}`);
      await expect(cards).toHaveCount(2);
      await expect(second.getByText('This branch is already excluded upstream.')).toBeVisible();
      await expect(second.getByText(/The frozen line still returns the draft status/)).toBeVisible();
      await expect(second.getByRole('link', { name: /单卡规划/ })).toBeVisible();
      await expect(page.getByText('记录你的判断')).toHaveCount(0);
      await expect(page.getByText('追问原审核者')).toHaveCount(0);
      await expect(page.getByRole('link', { name: '发起关联修复' })).toHaveCount(0);
      await shot(page, 'archive-review');

      await open('accepted-integration', `/integrations/${encodeURIComponent(childId)}`);
      await expect(summary).toContainText(receipt.receipt.candidate.commit);
      await expect(summary.getByRole('button', { name: '复制交付摘要' })).toBeVisible();
      await expect(page.getByLabel('返工资格')).toHaveCount(0);
      await expect(page.getByRole('button', { name: '在新的目标上重建' })).toHaveCount(0);
      await shot(page, 'archive-accepted-integration');
      await open('reworked-integration', `/integrations/${encodeURIComponent(integrationId)}`);
      await expect(page.getByRole('heading', { name: '候选', exact: true })).toBeVisible();
      await expect(page.getByLabel('返工资格')).toHaveCount(0);
      await expect(page.getByRole('button', { name: '按阻塞意见返工（每条谱系一次）' })).toHaveCount(0);

      // The archive service keeps no jobs of the original one.
      await open('job', `/jobs/${jobId}`);
      await expect(page.getByText('找不到这个作业')).toBeVisible();
      await expect(page.getByRole('heading', { name: '最近的事件' })).toHaveCount(0);
      for (const [name, logged] of Object.entries(archived)) {
        expect(logged.filter((text) => name !== 'job' || !text.startsWith('Failed to load resource')), `console on ${name}`).toEqual([]);
      }
    } finally {
      await archive.stop();
    }

    await net.record('workbench-flow', {
      result: 'passed', integration_id: integrationId, review_round_id: roundId, reworked_integration_id: childId,
      rebuilt_integration_id: rebuiltId, linked_planning_run_id: planningRunId, review_job_id: jobId, looks, archive_console: archived,
    });
  } catch (error) {
    await net.record('workbench-flow', { result: 'failed', error: String(error) });
    throw error;
  } finally {
    await env.stop();
  }
});

test('离线演示模式：回放录制的样例，新面板按回放的步骤出现，提交只重放录制的结果', async ({ page }) => {
  // `pnpm mock`: the dev server replays docs/web-api/samples and needs no service.
  const vite = spawn(resolve(REPO, 'frontend/node_modules/.bin/vite'), ['--mode', 'mock'], { cwd: resolve(REPO, 'frontend'), stdio: ['ignore', 'pipe', 'pipe'] });
  const errors = watchErrors(page);
  try {
    const base = await new Promise<string>((res, rej) => {
      createInterface({ input: vite.stdout }).on('line', (line) => {
        // Vite colours its output; the address is matched around the escape sequences.
        const address = /http:\/\/127\.0\.0\.1:\d+/.exec(line.split(String.fromCharCode(27)).map((part) => part.replace(/^\[[0-9;]*m/, '')).join(''));
        if (address) res(address[0]);
      });
      vite.once('exit', (code) => rej(new Error(`vite exited with ${code}`)));
      setTimeout(() => rej(new Error('vite printed no address')), 60_000);
    });
    const seen: Record<string, string[]> = {};
    // `at` is the request number of the recording the replay starts at (docs/web-api/samples/<scenario>/index.json).
    let last: string | null = null;
    // What a page logged or threw after it had been opened (while it was used) counts too.
    const closePage = (): void => {
      if (last === null) return;
      seen[last] = [...errors.logged];
      expect(errors.thrown, last).toEqual([]);
    };
    const open = async (name: string, path: string, scenario = 'rework', at?: number): Promise<void> => {
      closePage();
      errors.clear();
      last = name;
      await page.goto(`${base}${path}?mock=${scenario}${at === undefined ? '' : `&at=${at}`}`);
      await expect(page.locator('.app-head')).toBeVisible();
      await settled(page);
    };
    const ROUND = '10000360000000000000000000000000';
    const PARENT = '10000330000000000000000000000000';
    const CHILD = '10000390000000000000000000000000';
    const card = page.locator('.finding-card[data-seat="seat-1"]');
    const thread = card.locator('.finding-thread');

    // Every page says that it replays a recording.
    await open('home', '/');
    await expect(page.getByRole('note')).toContainText('演示：这里回放录制好的样例「rework」');
    await expect(page.getByRole('heading', { name: '最近的记录' })).toBeVisible();
    await expect(inbox(page)).toHaveCount(1);
    await expect(inbox(page).getByText('阶段审核已通过，创建集成')).toBeVisible();
    await expect(inbox(page).getByText('可以领取这个阶段')).toBeVisible();
    await shot(page, 'demo-home');

    // The accepted integration's job: the activity is recorded, with nothing left to continue.
    await open('job', '/jobs/100003e0000000000000000000000000');
    await expect(page.getByRole('heading', { name: '概况', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '可以继续的动作', exact: true })).toBeVisible();
    await expect(page.getByText('没有需要继续的动作。')).toBeVisible();

    // Step 55: the parent integration may be reworked.
    await open('rework-55', `/integrations/${PARENT}`, 'rework', 55);
    await expect(page.getByLabel('返工资格')).toContainText('可以返工');

    // Step 56: the findings, nothing recorded under them yet.
    await open('review-56', `/reviews/${ROUND}`, 'rework', 56);
    await expect(card).toBeVisible();
    await expect(thread).toHaveCount(0);
    // A judgement the recording does not hold is refused, in words that say this is a demo.
    await card.getByText('记录你的判断').click();
    await card.getByRole('radio', { name: '存疑' }).click();
    await card.getByLabel('判断的理由').fill('Something the recording never said.');
    await card.getByRole('button', { name: '记录判断' }).click();
    await expect(card.getByRole('alert')).toContainText('这是演示。只能回放录制过的那一次判断');
    await expect(card.getByRole('alert')).toContainText('The candidate still shows draft.');
    // The findings were read again after the refusal: still nothing under this one.
    await settled(page);
    await expect(thread).toHaveCount(0);
    // Step 57, the recorded judgement, and then what the recording read after it (step 58).
    await card.getByRole('radio', { name: '确认存在' }).click();
    await card.getByLabel('判断的理由').fill('The candidate still shows draft.');
    await card.getByRole('button', { name: '记录判断' }).click();
    await expect(thread).toContainText('确认存在');
    await expect(thread).toContainText('The candidate still shows draft.');
    await expect(thread.locator('.thread-entry')).toHaveCount(1);
    await shot(page, 'demo-review-judged');

    // Steps 58, 61 and 67 opened directly: the judgement, then the answer, then the linked planning.
    await open('review-58', `/reviews/${ROUND}`, 'rework', 58);
    await expect(thread.locator('.thread-entry')).toHaveCount(1);
    await open('review-61', `/reviews/${ROUND}`, 'rework', 61);
    await expect(thread.locator('.thread-entry')).toHaveCount(2);
    await expect(thread).toContainText('原审核者的回答');
    // A follow-up in other words is refused: the recorded answer answers only the recorded question.
    await card.getByText('追问原审核者').click();
    await card.getByLabel('要问的问题').fill('A question the recording never asked?');
    await card.getByRole('button', { name: '提交追问' }).click();
    await expect(card.getByRole('alert')).toContainText('这是演示。只能回放录制过的那一次追问');
    await expect(card.getByRole('alert')).toContainText('Which status does the frozen candidate expose?');
    await expect(card.getByLabel('要问的问题')).toHaveValue('A question the recording never asked?');
    // The recorded question replays the recorded job; the answer it got is the one already shown.
    await card.getByLabel('要问的问题').fill('Which status does the frozen candidate expose?');
    await card.getByRole('button', { name: '提交追问' }).click();
    await expect(page.locator('.job-bar')).toContainText('已结束', { timeout: 30_000 });
    await settled(page);
    await expect(thread.locator('.thread-entry')).toHaveCount(2);
    await expect(card.getByLabel('要问的问题')).toHaveValue('');
    await open('review-67', `/reviews/${ROUND}`, 'rework', 67);
    await expect(thread.locator('.thread-entry')).toHaveCount(3);
    await expect(thread.getByRole('link', { name: /单卡规划/ })).toHaveAttribute('href', '/task-plans/10000400000000000000000000000000');
    await shot(page, 'demo-review');

    // Step 72: the lineage has used its rework, and the page says why nothing is offered.
    await open('rework-72', `/integrations/${PARENT}`, 'rework', 72);
    await expect(page.getByLabel('返工资格')).toContainText('现在不能返工');
    await expect(page.getByLabel('返工资格')).toContainText('This integration lineage has already reserved its rework.');
    // Step 71 read the parent again: it now points at the integration the rework produced.
    await expect(page.getByRole('link', { name: '打开返工后的集成' })).toHaveAttribute('href', `/integrations/${CHILD}`);
    await expect(page.getByRole('button', { name: '按阻塞意见返工（每条谱系一次）' })).toHaveCount(0);

    // Step 78: the delivery summary of the accepted child.
    await open('delivery-78', `/integrations/${CHILD}`, 'rework', 78);
    await expect(page.getByLabel('交付摘要', { exact: true })).toBeVisible();
    await expect(page.getByLabel('摘要预览（Markdown）')).toHaveValue(/集成交付摘要/);
    await expect(page.getByText(/读不到/)).toHaveCount(0);
    await shot(page, 'demo-integration');

    // The other recordings, at the steps the handoff names.
    await open('accept-59', `/integrations/${PARENT}`, 'accept', 59);
    await expect(page.getByLabel('交付摘要', { exact: true })).toBeVisible();

    await open('dual-partial-49', '/jobs/10000350000000000000000000000000', 'dual-partial', 49);
    await expect(page.getByText('报告有效')).toHaveCount(1);
    await expect(page.getByRole('button', { name: '只补跑缺失的席位（seat-1）' })).toBeVisible();
    // Step 54 read the activity again after the retry: both reports stand, nothing is left to retry.
    await open('dual-partial-54', '/jobs/10000350000000000000000000000000', 'dual-partial', 54);
    await expect(page.getByText('报告有效')).toHaveCount(2);
    await expect(page.getByRole('button', { name: /只补跑缺失的席位/ })).toHaveCount(0);

    await open('cancel-41', '/jobs/100002a0000000000000000000000000', 'cancel', 41);
    await expect(page.getByRole('heading', { name: '执行者', exact: true })).toBeVisible();
    await expect(page.locator('.job-actor').first()).toContainText('进行中');
    await open('cancel-48', '/jobs/100002a0000000000000000000000000', 'cancel', 48);
    await expect(page.locator('.page-aside, .app-main').getByText('已取消').first()).toBeVisible();
    // Step 49 read the activity again after the cancellation: the executor is no longer shown as running.
    await open('cancel-49', '/jobs/100002a0000000000000000000000000', 'cancel', 49);
    await expect(page.locator('.job-actor').first()).not.toContainText('进行中');
    await open('cancel-55', '/jobs/100002f0000000000000000000000000', 'cancel', 55);
    await expect(page.getByText('已中断').first()).toBeVisible();
    await expect(page.locator('.job-actor').first()).toContainText('中断前最后观测于');

    // The setup check is part of the recording too; the goal form still works beside it.
    await open('new-goal', '/new');
    await expect(page.getByRole('heading', { name: '检查接入' })).toBeVisible();
    await page.getByLabel('原始目标').fill('A goal typed in the recorded demo.');
    await expect(page.getByRole('button', { name: '直接确认为目标' })).toBeEnabled();
    await page.getByLabel('原始目标').fill('');
    await shot(page, 'demo-new-goal');
    closePage();

    for (const [name, logged] of Object.entries(seen)) expect(logged, `console on ${name}`).toEqual([]);
  } finally {
    vite.kill('SIGTERM');
  }
});
