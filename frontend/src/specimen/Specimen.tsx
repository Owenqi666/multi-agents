// Step 0 of T-0070: one page that fixes the look before the other pages are built. The
// stage, colours, type, both themes, status badges, diff lines, log blocks and empty states
// are all drawn from hand-written samples in the contract's object shapes.

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Badge, JobBadge, Requested, Unconfirmed } from '../components/Badge';
import { DiffView } from '../components/DiffView';
import { EmptyState } from '../components/EmptyState';
import { LogBlock } from '../components/LogBlock';
import { ThemeSwitch, type ThemeChoice } from '../components/ThemeSwitch';
import { LanguageSwitch } from '../components/LanguageSwitch';
import * as sample from '../fixtures/specimen';
import { deriveStory } from '../stage/derive';
import { familyOfTone, toneVar, type Tone } from '../stage/models';
import { glyph } from '../stage/draw';
import { Glyph, RelayPlate, Stage } from '../stage/Stage';
import { tx } from '../i18n';

function Sheet({ id, title, intro, children }: { id: string; title: string; intro: ReactNode; children: ReactNode }) {
  return (
    <section className="sheet" id={id} aria-labelledby={`${id}-h`}>
      <div className="sheet-side">
        <h2 id={`${id}-h`}>{title}</h2>
        <div className="sheet-intro">{intro}</div>
      </div>
      <div className="sheet-body">{children}</div>
    </section>
  );
}

const TONES: { tone: Tone; name: string; family: string }[] = [
  { tone: 'opus', name: 'Opus', family: 'Claude，铜' },
  { tone: 'sonnet', name: 'Sonnet', family: 'Claude，香槟' },
  { tone: 'fable', name: 'Fable', family: 'Claude，玫瑰金' },
  { tone: 'sol', name: 'Sol', family: 'GPT，青' },
  { tone: 'astra', name: 'Astra', family: 'GPT，钢蓝' },
  { tone: 'terra', name: 'Terra', family: 'GPT，靛' },
  { tone: 'you', name: '你', family: '墨，暗色里是白光' },
  { tone: 'bench', name: '工作台', family: '钢灰，不用模型' },
];

/** A short lit filament in one tone, with its pin. */
function Filament({ tone }: { tone: Tone }) {
  const c = toneVar(tone);
  return (
    <svg className="filament" viewBox="0 0 120 24" width="120" height="24" aria-hidden="true" style={{ ['--c' as string]: c }}>
      <line className="f-groove" x1="6" y1="12" x2="114" y2="12" />
      <line className="f-bloom" x1="6" y1="12" x2="84" y2="12" />
      <line className="f-core" x1="6" y1="12" x2="84" y2="12" />
      <line className="f-hi" x1="6" y1="12" x2="84" y2="12" />
      <g transform="translate(84 12)" className="f-pin">
        <circle className="ring" r="7" />
        <g className="in" dangerouslySetInnerHTML={{ __html: glyph({ tone }, 3.1) }} />
      </g>
    </svg>
  );
}

function useCssValue(name: string, theme: ThemeChoice): string {
  const [value, setValue] = useState('');
  useEffect(() => {
    const read = () => setValue(getComputedStyle(document.documentElement).getPropertyValue(name).trim());
    read();
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', read);
    return () => mq.removeEventListener('change', read);
  }, [name, theme]);
  return value;
}

function ToneRow({ tone, name, family, theme }: { tone: Tone; name: string; family: string; theme: ThemeChoice }) {
  const hex = useCssValue(`--tone-${tone}`, theme);
  return (
    <li className="tone-row">
      <Filament tone={tone} />
      <span className="tone-name"><Glyph holder={{ tone }} size={12} />{tx(name)}</span>
      <span className="tone-family">{tx(family)}</span>
      <code className="tone-hex">{hex}</code>
      <span className="tone-kind">{familyOfTone(tone) === 'claude' ? tx('圆') : familyOfTone(tone) === 'gpt' ? tx('菱形') : familyOfTone(tone) === 'human' ? tx('空心圆') : tx('方')}</span>
    </li>
  );
}

function StatusRow({ name, theme, tone, word, use }: { name: string; theme: ThemeChoice; tone: 'ok' | 'fail' | 'warn'; word: string; use: string }) {
  const hex = useCssValue(`--${name}`, theme);
  return (
    <li className="tone-row status">
      <Badge tone={tone}>{word}</Badge>
      <span className="tone-family">{use}</span>
      <code className="tone-hex">{hex}</code>
    </li>
  );
}

export function Specimen() {
  const [theme, setTheme] = useState<ThemeChoice>('light');
  const onTheme = useCallback((t: ThemeChoice) => setTheme(t), []);
  const hero = useMemo(() => deriveStory({ consultation: sample.consultation, goal: sample.goal, plan: sample.plan, stage: sample.directStage }), []);
  const variant = useMemo(() => deriveStory({ stage: sample.delegateStage }), []);
  const weave = useMemo(() => deriveStory({ consultation: sample.consultation, goal: sample.goal, plan: sample.plan, stage: sample.collabStage, integrationHistory: sample.collabHistory }), []);
  const [opened, setOpened] = useState<string | null>(null);

  return (
    <div className="page">
      <header className="sheet-head">
        <span className="brand">
          <svg width="34" height="8" viewBox="0 0 34 8" aria-hidden="true">
            <line x1="1" y1="4" x2="33" y2="4" strokeWidth="1" style={{ stroke: 'var(--ink-3)' }} />
            <circle cx="5" cy="4" r="2.4" style={{ fill: 'var(--tone-opus)' }} />
            <circle cx="17" cy="4" r="2.4" style={{ fill: 'var(--tone-sol)' }} />
            <circle cx="29" cy="4" r="2.4" style={{ fill: 'var(--tone-sonnet)' }} />
          </svg>
          {tx('Multi-agent 本地工作台')}
        </span>
        <dl className="title-block">
          <div><dt>{tx('日期')}</dt><dd>2026-11-19</dd></div>
          <div><dt>{tx('主题')}</dt><dd><ThemeSwitch onChange={onTheme} /></dd></div>
          <div><dt>{tx('语言')}</dt><dd><LanguageSwitch /></dd></div>
        </dl>
      </header>

      <main>
        <section className="hero" aria-labelledby="weave-h">
          <div className="weave-head">
            <h2 id="weave-h">{tx('首页舞台：织带，纵轴是模型等级')}</h2>
            <p>{tx('每个席位一条泳道，按等级从上到下：Astra 和 Fable，Sol 和 Opus，Terra 和 Sonnet。同一档里 GPT 在上、Claude 在下，所以跨家族的交接和并行工作会画成冷暖泳道之间交错的细线。你和工作台在下方两条轨道上，不分等级。例子是一个分派阶段：Terra 和 Sonnet 并行实施，Opus 和 Astra 盲审，Terra 返工一次后通过。')}</p>
          </div>
          <Stage story={weave} kind="weave" handle="stage" foot={tx('泳道和颜色跟着席位；模型名只用运行时报告值，返工记录没有报告值，所以那一格是虚线')} />
        </section>

        <section className="weave-section" aria-labelledby="relay-h">
          <div className="weave-head">
            <h2 id="relay-h">{tx('备选：接力线')}</h2>
            <p>{tx('同一套步骤数据画成一条线，颜色随持有者变化，双席盲审分成两条支线，返工时合回。例子是一个直接阶段，reviewer-2 第一轮没有报告模型，显示「未确认」。')}</p>
          </div>
          <Stage story={hero} handle="relay" keys={false} foot={tx('样例按约定的对象形状手写，正式版由运行状态换算')} />
        </section>

        <Sheet id="variants" title={tx('接力线的另一种形状')} intro={<>
          <p>{tx('分派阶段：两张子卡并行复核、并行实施，工作台合并后由一位审核者审。')}</p>
          <p>{tx('单席只画一条线；没有返工，所以没有回环。并行支线不标「互不可见」。')}</p>
        </>}>
          <RelayPlate story={variant} label={tx('分派阶段的接力线，已走到接受')} />
        </Sheet>

        <Sheet id="colour" title={tx('色彩')} intro={<>
          <p>{tx('颜色跟着席位走：Claude 三个模型同一暖色相，GPT 三个模型同一冷色相。暗色里已完成的线带微光，亮色是墨线。')}</p>
          <p>{tx('状态色只用在徽标和 diff 上，不上舞台。')}</p>
        </>}>
          <ul className="tones">
            {TONES.map((t) => <ToneRow key={t.tone} {...t} theme={theme} />)}
          </ul>
          <ul className="tones">
            <StatusRow name="ok" theme={theme} tone="ok" word={tx('通过')} use={tx('测试通过、审核通过、已核验、diff 新增行')} />
            <StatusRow name="warn" theme={theme} tone="warn" word={tx('要处理')} use={tx('需要返工、已中断、未核验、已请求取消')} />
            <StatusRow name="fail" theme={theme} tone="fail" word={tx('失败')} use={tx('被拒绝、命令失败、阻塞意见、diff 删除行')} />
          </ul>
        </Sheet>

        <Sheet id="type" title={tx('字体')} intro={<p>{tx('标题用 IBM Plex Sans 细字重，中文落到苹方。ID、哈希、模型 ID、diff 和日志用 IBM Plex Mono。字体随页面打包，不走网络。')}</p>}>
          <div className="type-rows">
            <div className="type-row"><span className="type-spec">60 / 300</span><span className="t-display">{tx('代码审核')}</span></div>
            <div className="type-row"><span className="type-spec">28 / 300</span><span className="t-title">{tx('阶段：作业面板')}</span></div>
            <div className="type-row"><span className="type-spec">15 / 400</span><p className="t-body">{tx('同一份冻结快照同时交给两位审核者，互相看不到对方的意见。结论出来之前，页面只显示席位状态。')}</p></div>
            <div className="type-row"><span className="type-spec">13 / 400</span><p className="t-small">{tx('按钮、说明和表头。读取时间、字节范围这类次要信息用更浅的墨色。')}</p></div>
            <div className="type-row"><span className="type-spec">12.5 / mono</span><code className="t-mono">gpt-6.2-sol  7b21d04  reviewer-1  art-dGVzdC0x</code></div>
          </div>
        </Sheet>

        <Sheet id="badges" title={tx('状态徽标')} intro={<>
          <p>{tx('作业状态按退出码和读回的领域状态显示，不看 error 是否为空。')}</p>
          <p>{tx('模型和档位只写报告值；没有报告时写「未确认」，请求值另外标「请求」。')}</p>
        </>}>
          <div className="badge-grid">
            <h3>{tx('作业')}</h3>
            <ul className="badge-list">
              {sample.jobs.map(({ label, job }) => (
                <li key={label}><JobBadge job={job} /><code>{label}</code></li>
              ))}
            </ul>
            <h3>{tx('模型证据')}</h3>
            <ul className="badge-list wide">
              <li><span className="evidence"><Glyph holder={{ tone: 'opus' }} size={12} />Claude Opus 5.5<span className="evidence-effort">high</span></span><code>{tx('报告值')}</code></li>
              <li><span className="evidence"><Glyph holder={{ tone: 'astra' }} size={12} /><Unconfirmed what={tx('模型')} /><Requested>gpt-6.2-astra · xhigh</Requested></span><code>{tx('没有报告')}</code></li>
              <li><span className="evidence"><Glyph holder={{ tone: 'sol' }} size={12} />echo-not-evidence<span className="evidence-effort">{tx('档位')} <Unconfirmed what={tx('档位')} /></span></span><code>{tx('原样显示')}</code></li>
            </ul>
            <h3>{tx('核验和审核')}</h3>
            <ul className="badge-list">
              <li><Badge tone="ok">{tx('已核验')}</Badge></li>
              <li><Badge tone="warn">{tx('未核验')}</Badge></li>
              <li><Badge tone="warn">{tx('仓库被占用，结果未核验')}</Badge></li>
              <li><Badge tone="ok">{tx('审核通过')}</Badge></li>
              <li><Badge tone="warn">{tx('需要返工')}</Badge></li>
              <li><Badge tone="plain">{tx('需要你判断')}</Badge></li>
              <li><Badge tone="fail">{tx('阻塞 P1')}</Badge></li>
            </ul>
            <h3>{tx('读取')}</h3>
            <ul className="badge-list wide">
              <li><Badge tone="muted">{tx('作业运行中，结束后可读')}</Badge></li>
              <li><span className="stamp">{tx('读取于 20:14:03')}</span><code>{tx('lock_busy 时保留上次内容')}</code></li>
            </ul>
          </div>
        </Sheet>

        <Sheet id="diff" title={tx('diff 行')} intro={<>
          <p>{tx('返工增量：修订候选相对返工前的候选。阻塞意见挂在它指向的行下面，可以跳到冻结文件。')}</p>
          <p>{tx('重命名、二进制文件和带转义的中文路径都按原样解析。')}</p>
        </>}>
          <div className="diff-bar">
            <div className="seg small" role="radiogroup" aria-label={tx('diff 范围')}>
              <button type="button" role="radio" aria-checked="false">{tx('累计')}</button>
              <button type="button" role="radio" aria-checked="true" className="on">{tx('返工增量')}</button>
            </div>
            <span className="stamp">{tx('修订候选 7b21d04，相对 3f9a1c2')}</span>
          </div>
          <DiffView diff={sample.reworkDiff} annotations={[sample.reworkFinding]} onOpenFinding={(a) => setOpened(`${a.finding.path}:${a.finding.line_start}`)} />
          {opened ? <p className="opened">{tx('会打开冻结文件 {0}（样张里不跳转）', opened)}</p> : null}
          <DiffView diff={null} emptyNote={<EmptyState compact title={tx('还没有可看的 diff')} body={tx('候选还没有核验通过（unavailable_reason: not_verified）。实施作业结束后再读。')} />} />
        </Sheet>

        <Sheet id="logs" title={tx('日志块')} intro={<>
          <p>{tx('日志按纯文本显示，每页最多 64 KiB。头部写清是哪次测试、哪条命令。')}</p>
          <p>{tx('退出码 2 且没有结果时，原因只在 stderr，按原文显示。')}</p>
        </>}>
          <div className="log-stack">
            <LogBlock title="frontend-check" coords={tx('测试 2 · 命令 3 · 日志 1')} stream="stdout" page={sample.testLog} onMore={() => undefined} />
            <div className="rejected">
              <div className="rejected-head">
                <Badge tone="fail">{tx('被拒绝')}</Badge>
                <code>integration.accept</code>
                <span className="stamp">{tx('退出码 2，没有结果')}</span>
              </div>
              <LogBlock title={tx('拒绝原因')} stream="stderr" page={sample.rejectedStderr} />
            </div>
            <LogBlock title="unit" coords={tx('测试 3 · 命令 2 · 日志 1')} stream="stdout" page={sample.lockBusyPage} previous={sample.testLog} readAt="2026-11-19T20:14:03" />
            <LogBlock title="lint" coords={tx('测试 3 · 命令 1 · 日志 1')} stream="stdout" page={sample.lockBusyPage} />
          </div>
        </Sheet>

        <Sheet id="empty" title={tx('空状态和按钮')} intro={<>
          <p>{tx('空的地方说下一步做什么。')}</p>
          <p>{tx('提交后到作业结束前，按钮不能再点；取消只在运行中出现。')}</p>
        </>}>
          <div className="empty-grid">
            <EmptyState title={tx('还没有项目')} body={tx('写下想做成的事。顾问会先读它，帮你写成可以确认的目标。')} action={<button type="button" className="btn primary">{tx('提出目标')}</button>} />
            <EmptyState title={tx('还没有 Codex 的模型')} body={tx('Sol、Terra、Astra 要等第一次环境检查成功后才出现在选择器里。')} action={<button type="button" className="btn">{tx('检查 Codex 环境')}</button>} />
          </div>
          <div className="button-rows">
            <div className="button-row"><button type="button" className="btn primary">{tx('领取阶段')}</button><button type="button" className="btn">{tx('补跑审核')}</button><span className="btn-note">{tx('可点')}</span></div>
            <div className="button-row"><button type="button" className="btn busy" disabled>{tx('已提交，等作业结束')}</button><span className="btn-note">{tx('防重复提交')}</span></div>
            <div className="button-row"><button type="button" className="btn primary" disabled>{tx('接受')}</button><span className="btn-note">{tx('审核结论不是当前测试的，不能接受')}</span></div>
            <div className="button-row"><Badge tone="live">{tx('运行中')}</Badge><button type="button" className="btn small quiet">{tx('取消作业')}</button><Badge tone="warn">{tx('已请求取消')}</Badge><span className="btn-note">{tx('取消后不再显示按钮')}</span></div>
          </div>
        </Sheet>
      </main>

      <footer className="page-foot">
        <p>{tx('样张只有画面，不连服务。确认之后再铺项目流程页面。')}</p>
      </footer>
    </div>
  );
}
