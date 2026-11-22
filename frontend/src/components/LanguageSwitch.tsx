import { setLang, useLang, type Lang } from '../i18n';

/** 中 / EN in the corner. The choice is kept in this browser; on a first visit the page
 *  follows the browser's language. */
export function LanguageSwitch() {
  const lang = useLang();
  const options: [Lang, string, string][] = [['zh', '中', '中文'], ['en', 'EN', 'English']];
  return (
    <div className="seg" role="radiogroup" aria-label="语言 / Language">
      {options.map(([value, label, name]) => (
        <button key={value} type="button" role="radio" lang={value === 'zh' ? 'zh-CN' : 'en'} aria-label={name} aria-checked={lang === value}
          className={lang === value ? 'on' : ''} onClick={() => setLang(value)}>{label}</button>
      ))}
    </div>
  );
}
