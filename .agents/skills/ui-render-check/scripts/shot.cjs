#!/usr/bin/env node
// Screenshot check for the web workbench pages.
//
// Usage:
//   node shot.cjs <url> <out-dir> [--beats 0,5,6,8]
//
// <url> is a running page, for example the Vite dev server in mock mode
// (http://127.0.0.1:5173/) or the bridge (http://127.0.0.1:8765/).
// Renders light and dark at 1280 px and light at 400 px with reduced motion, so every
// shot is a settled state. If the page exposes window.stage.goto(i), it also shoots the
// listed beats and one frame with motion on. Prints horizontal overflow and console
// errors, then "CHECK: ok" or the number of problems. Exit code 1 when problems remain.
//
// Needs Playwright with a Chromium build: pnpm add -D playwright && pnpm exec playwright install chromium
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const url = args[0];
const outDir = args[1];
const beatsArg = args.indexOf('--beats') >= 0 ? args[args.indexOf('--beats') + 1] : null;
if (!url || !outDir) {
  console.error('usage: node shot.cjs <url> <out-dir> [--beats 0,5,6,8]');
  process.exit(2);
}
fs.mkdirSync(outDir, { recursive: true });

async function settledShots(browser, scheme, width, height) {
  const ctx = await browser.newContext({
    viewport: { width, height },
    colorScheme: scheme,
    deviceScaleFactor: 1,
    reducedMotion: 'reduce',
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.waitForTimeout(400);

  const tag = scheme + '-' + width;
  const hasStage = await page.evaluate(() => !!(window.stage && typeof window.stage.goto === 'function'));
  if (hasStage) {
    const count = await page.evaluate(() => window.stage.count || 1);
    const beats = beatsArg
      ? beatsArg.split(',').map(Number).filter((n) => n >= 0 && n < count)
      : [0, Math.min(5, count - 1), Math.min(6, count - 1), count - 1];
    for (const i of [...new Set(beats)]) {
      await page.evaluate((n) => { window.stage.pause && window.stage.pause(); window.stage.goto(n); }, i);
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(outDir, tag + '-beat' + i + '.png'), fullPage: true });
    }
  } else {
    await page.screenshot({ path: path.join(outDir, tag + '.png'), fullPage: true });
  }

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  console.log(tag, '| horizontal overflow px:', overflow, '| console errors:', errors.length ? errors.join(' || ') : 'none');
  await ctx.close();
  return (overflow > 0 ? 1 : 0) + (errors.length ? 1 : 0);
}

async function motionShot(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, colorScheme: 'light', deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(url, { waitUntil: 'networkidle' });
  const hasStage = await page.evaluate(() => !!(window.stage && typeof window.stage.goto === 'function'));
  if (hasStage) {
    await page.evaluate(() => { window.stage.goto(4); window.stage.play && window.stage.play(); });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(outDir, 'motion-midflight.png'), fullPage: true });
  }
  console.log('motion pass | errors:', errors.length ? errors.join(' || ') : 'none');
  await ctx.close();
  return errors.length ? 1 : 0;
}

(async () => {
  const browser = await chromium.launch();
  let problems = 0;
  problems += await settledShots(browser, 'light', 1280, 860);
  problems += await settledShots(browser, 'dark', 1280, 860);
  problems += await settledShots(browser, 'light', 400, 800);
  problems += await motionShot(browser);
  await browser.close();
  console.log(problems ? 'CHECK: ' + problems + ' problem(s)' : 'CHECK: ok');
  console.log('screenshots in', path.resolve(outDir));
  process.exit(problems ? 1 : 0);
})();
