// Interface language. Every piece of interface text is written in Chinese in the code and
// passed through tx(); English comes from the dictionary in en.ts, keyed by the Chinese text.
// Text the backend returns (goals, model output, logs, diffs) is shown as written.
//
// Placeholders: tx('{0} 条阻塞意见', n) fills {0}, {1}, ... in either language.

import { useSyncExternalStore } from 'react';
import { EN } from './en';

export type Lang = 'zh' | 'en';
const KEY = 'multi-agent.lang';

function initial(): Lang {
  try {
    const stored = window.localStorage.getItem(KEY);
    if (stored === 'zh' || stored === 'en') return stored;
  } catch {
    /* storage may be unavailable */
  }
  const nav = typeof navigator !== 'undefined' ? navigator.language : 'zh';
  return /^zh\b/i.test(nav) ? 'zh' : 'en';
}

let current: Lang = typeof window === 'undefined' ? 'zh' : initial();
if (typeof document !== 'undefined') document.documentElement.lang = current === 'zh' ? 'zh-CN' : 'en';
const listeners = new Set<() => void>();

export function getLang(): Lang {
  return current;
}

export function setLang(lang: Lang): void {
  if (lang === current) return;
  current = lang;
  try {
    window.localStorage.setItem(KEY, lang);
  } catch {
    /* the choice then lasts for this page only */
  }
  if (typeof document !== 'undefined') document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  listeners.forEach((l) => l());
}

export function useLang(): Lang {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => listeners.delete(l); },
    () => current,
    () => current,
  );
}

const fill = (text: string, args: unknown[]): string =>
  args.length ? text.replace(/\{(\d+)\}/g, (m, i: string) => (Number(i) < args.length ? String(args[Number(i)]) : m)) : text;

/** Interface text in the current language. The key is the Chinese text. */
export function tx(zh: string, ...args: unknown[]): string {
  if (current === 'en') {
    const en = EN[zh];
    if (en !== undefined) return fill(en, args);
  }
  return fill(zh, args);
}

/** Same as tx, with a shorter English wording for a given place when the dictionary has
 *  one under "context|中文" (step titles on the stage drawings, where room is tight). */
export function txc(context: string, zh: string, ...args: unknown[]): string {
  if (current === 'en') {
    const en = EN[`${context}|${zh}`];
    if (en !== undefined) return fill(en, args);
  }
  return tx(zh, ...args);
}

const SEPARATOR = { '、': ', ', '，': ', ', '；': '; ' } as const;

/** Joins a list with the separator that fits the current language (、 and ， read as commas in English). */
export function joinList(items: readonly unknown[], sep: keyof typeof SEPARATOR = '、'): string {
  return items.map(String).join(current === 'en' ? SEPARATOR[sep] : sep);
}

/** "label：value" with the colon of the current language. */
export function pair(label: unknown, value: unknown): string {
  return `${String(label)}${current === 'en' ? ': ' : '：'}${String(value)}`;
}

/** A table of interface words whose values are translated on every read, so module-level
 *  tables follow the current language. */
export function translated<T extends Record<string, string>>(table: T): T {
  return new Proxy(table, {
    get(target, key, receiver) {
      const value = Reflect.get(target, key, receiver);
      return typeof value === 'string' ? tx(value) : value;
    },
  });
}

/** For tests: switch without touching storage. */
export function setLangForTest(lang: Lang): void {
  current = lang;
  listeners.forEach((l) => l());
}
