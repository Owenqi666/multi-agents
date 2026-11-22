// Every interface string has an English entry, and pages render without Chinese in English
// mode (the recorded samples carry English data, so any Han character left is interface text).

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { setTransport } from '../api/client';
import { jobs } from '../api/jobs';
import type { Job } from '../api/types';
import { EN } from '../i18n/en';
import { setLangForTest, tx } from '../i18n';
import { ACTION_LABEL, KIND_LABEL } from '../app/labels';
import { IntegrationPage } from '../pages/IntegrationPage';
import { JobPage } from '../pages/JobPages';
import { ReviewPage } from '../pages/ReviewPage';
import { StagePage } from '../pages/StagePage';
import { fakeTransport } from './fake';

const HAN = /[一-鿿]/;
const sources = import.meta.glob<string>('../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true });
const files = import.meta.glob<unknown>('@samples/*/*.json', { eager: true, import: 'default' });

function sample<T>(scenario: string, file: string): T {
  const key = Object.keys(files).find((k) => k.endsWith(`/${scenario}/${file}`));
  if (!key) throw new Error(`missing sample ${scenario}/${file}`);
  return structuredClone(files[key]) as T;
}
const haveSamples = Object.keys(files).some((k) => k.endsWith('/accept/052.json'));

/** Chinese string literals in interface code: tx() keys and translated word tables. */
function keysInCode(): Map<string, string> {
  const keys = new Map<string, string>();
  for (const [path, raw] of Object.entries(sources)) {
    // The language switch names each language in that language on purpose.
    if (/\/(test|fixtures|i18n)\//.test(path) || path.includes('.test.') || path.endsWith('LanguageSwitch.tsx')) continue;
    const code = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"])\/\/[^\n]*/g, '$1');
    for (const m of code.matchAll(/'((?:[^'\\\n]|\\.)*)'/g)) {
      const text = m[1]!.replace(/\\'/g, "'").replace(/\\\\/g, '\\');
      if (HAN.test(text)) keys.set(text, path);
    }
  }
  return keys;
}

describe('English dictionary', () => {
  it('has an entry for every interface string', () => {
    const missing = [...keysInCode()].filter(([k]) => !(k in EN)).map(([k, p]) => `${p}: ${k}`);
    expect(missing).toEqual([]);
  });

  it('keeps every placeholder in the English text', () => {
    const broken = Object.entries(EN).filter(([zh, en]) => {
      const a = (zh.match(/\{\d+\}/g) ?? []).sort().join();
      const b = (en.match(/\{\d+\}/g) ?? []).sort().join();
      return a !== b;
    }).map(([zh]) => zh);
    expect(broken).toEqual([]);
  });

  it('switches text and fills placeholders in both languages', () => {
    setLangForTest('en');
    expect(tx('{0} 条阻塞意见', 2)).toBe('2 blocking findings');
    expect(ACTION_LABEL['integration.accept']).toBe('Accept');
    expect(KIND_LABEL.stage).toBe('Stage');
    setLangForTest('zh');
    expect(tx('{0} 条阻塞意见', 2)).toBe('2 条阻塞意见');
    expect(ACTION_LABEL['integration.accept']).toBe('接受');
  });
});

describe.skipIf(!haveSamples)('pages in English', () => {
  const workspace = () => sample('accept', '006.json');
  const noChinese = (container: HTMLElement) => {
    const text = container.textContent ?? '';
    // Han characters, and the full-width punctuation that would come with a Chinese separator.
    const found = [...text.matchAll(/[^\s]*[一-鿿：；，、（）。「」][^\s]*/g)].map((m) => m[0]);
    expect(found).toEqual([]);
  };

  it('job page with a refusal', async () => {
    setLangForTest('en');
    const job = sample<Job>('accept', '043.json');
    setTransport(fakeTransport({ [`/jobs/${job.job_id}`]: job }));
    const { container } = render(<JobPage id={job.job_id} />);
    await screen.findByText('Why it was refused');
    noChinese(container);
  });

  it('direct and delegate stages', async () => {
    setLangForTest('en');
    for (const file of ['027.json', '053.json']) {
      const stage = sample<{ data: { stage_run_id: string } }>('accept', file);
      setTransport(fakeTransport({ '/jobs': { items: [] }, '/workspace': workspace(), [`/resources/stage/${stage.data.stage_run_id}`]: stage }));
      await jobs.init();
      const view = render(<StagePage id={stage.data.stage_run_id} />);
      await screen.findByText('Gates');
      noChinese(view.container);
      view.unmount();
    }
  });

  it('integration and review round', async () => {
    setLangForTest('en');
    const integration = sample<{ data: { integration_id: string } }>('rework', '055.json');
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/workspace': workspace(), [`/resources/integration/${integration.data.integration_id}`]: integration }));
    await jobs.init();
    const a = render(<IntegrationPage id={integration.data.integration_id} />);
    await screen.findByText('Cumulative diff');
    noChinese(a.container);
    a.unmount();
    const review = sample<{ data: { round_id: string; snapshot_id: string } }>('rework', '046.json');
    setTransport(fakeTransport({ '/jobs': { items: [] }, '/workspace': workspace(), [`/resources/review/${review.data.round_id}`]: review }));
    await jobs.init();
    const b = render(<ReviewPage id={review.data.round_id} />);
    await screen.findByText('Findings');
    noChinese(b.container);
  });
});
