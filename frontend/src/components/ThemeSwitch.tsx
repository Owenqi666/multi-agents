import { useEffect, useState } from 'react';
import { tx } from '../i18n';

export type ThemeChoice = 'auto' | 'light' | 'dark';
const KEY = 'multi-agent.theme';

/** Light is the default (Owen, 2026-11-20); a choice made here is kept per browser. */
export const DEFAULT_THEME: ThemeChoice = 'light';

function stored(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' || v === 'auto' ? v : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** 亮 is the default; 自动 follows the system like the iPhone; 暗 pins the dark theme. */
export function ThemeSwitch({ onChange }: { onChange?: (t: ThemeChoice) => void }) {
  const [theme, setTheme] = useState<ThemeChoice>(stored);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'auto') delete root.dataset.theme;
    else root.dataset.theme = theme;
    try {
      if (theme === DEFAULT_THEME) window.localStorage.removeItem(KEY);
      else window.localStorage.setItem(KEY, theme);
    } catch {
      /* storage can be unavailable; the choice then lasts for this page only */
    }
    onChange?.(theme);
  }, [theme, onChange]);
  const options: [ThemeChoice, string][] = [['light', tx('亮')], ['dark', tx('暗')], ['auto', tx('自动')]];
  return (
    <div className="seg" role="radiogroup" aria-label={tx('主题')}>
      {options.map(([value, label]) => (
        <button key={value} type="button" role="radio" aria-checked={theme === value} className={theme === value ? 'on' : ''}
          onClick={() => setTheme(value)}>{label}</button>
      ))}
    </div>
  );
}
