import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { reportedFull, toneVar } from './models';
import { glyph } from './draw';
import type { StageRenderer } from './draw';
import { Playback, RelayRenderer, type PlaybackState } from './relay';
import { WeaveRenderer } from './weave';
import type { Beat, Holder, Story } from './story';
import './stage.css';
import { tx, joinList } from '../i18n';

/** The plain mark of a holder, for captions and legends. */
export function Glyph({ holder, size = 14 }: { holder: Pick<Holder, 'tone'>; size?: number }) {
  return (
    <svg className="glyph" viewBox="-7 -7 14 14" width={size} height={size} aria-hidden="true"
      style={{ ['--c' as string]: toneVar(holder.tone) }}
      dangerouslySetInnerHTML={{ __html: glyph(holder, 4.6) }} />
  );
}

export function RequestTag({ model, effort }: { model: string | null; effort: string | null }) {
  if (!model) return null;
  return <span className="tag-request"><b>{tx('请求')}</b>{model}{effort ? ` · ${effort}` : ''}</span>;
}

/** Who holds the work, with model evidence: reported values only, the request apart. */
export function HolderLine({ holder }: { holder: Holder }) {
  if (holder.plain) {
    return (
      <span className="actor">
        <Glyph holder={holder} />
        <span className="a-name">{holder.name}</span>
        <span className="a-role">{holder.key === 'you' ? tx('确认、领取与接受') : tx('测试、合并与快照，不用模型')}</span>
      </span>
    );
  }
  const unconfirmed = holder.reportedModel === null;
  const showRequest = unconfirmed || holder.reportedModel !== holder.requestedModel;
  return (
    <span className="actor">
      <Glyph holder={holder} />
      <span className={unconfirmed ? 'a-name unconfirmed' : 'a-name'}>{reportedFull(holder.reportedModel)}</span>
      <span className="a-role">
        <span>{holder.role}</span>
        <span>{tx('档位')} {holder.reportedEffort ?? tx('未确认')}</span>
        {showRequest ? <RequestTag model={holder.requestedModel} effort={holder.requestedEffort} /> : null}
      </span>
    </span>
  );
}

function Caption({ story, index }: { story: Story; index: number }) {
  const beat = story.beats[index];
  const prev = index > 0 ? story.beats[index - 1] : null;
  if (!beat) return null;
  const names = (b: Beat) => joinList([...new Set(b.holders.map((h) => (!h.plain && h.reportedModel === null ? tx('{0}（未确认）', h.role) : h.name)))]);
  return (
    <section className="caption" aria-live="polite">
      <div className="lead">
        <p className="eyebrow">
          <span className="swatch" aria-hidden="true">
            {beat.holders.map((h) => <i key={h.key} style={{ ['--c' as string]: toneVar(h.tone) }} />)}
          </span>
          <span className="phase">{beat.phase}</span>
          <span className="count">{index + 1} / {story.beats.length}</span>
        </p>
        <h1 key={`t${index}`} className="swap">{beat.title}</h1>
      </div>
      <div className="body swap" key={`b${index}`}>
        <p className="note">{beat.note}</p>
        <dl className="facts">
          <div>
            <dt>{tx('正在工作')}</dt>
            <dd className="who">{beat.holders.map((h) => <HolderLine key={h.key} holder={h} />)}</dd>
          </div>
          <div style={{ visibility: prev ? 'visible' : 'hidden' }}>
            <dt>{tx('交接')}</dt>
            <dd className="hand">
              {prev ? <>
                <span>{names(prev)}</span><span className="arrow" aria-hidden="true">→</span><span>{names(beat)}</span>
                {beat.carry ? <span className="carry" style={{ ['--c' as string]: toneVar(beat.holders[0]!.tone) }}><i />{beat.carry}</span> : null}
              </> : ' '}
            </dd>
          </div>
        </dl>
      </div>
    </section>
  );
}

const reduceQuery = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

interface PlateRefs {
  frame: React.RefObject<HTMLElement | null>;
  box: React.RefObject<HTMLDivElement | null>;
  svg: React.RefObject<SVGSVGElement | null>;
  mBaton: React.RefObject<SVGTextElement | null>;
  mSub: React.RefObject<SVGTextElement | null>;
}

export type StageKind = 'relay' | 'weave';

function usePlate(story: Story, refs: PlateRefs, layoutAttr: boolean, setup: (r: StageRenderer) => () => void, kind: StageKind = 'relay') {
  useLayoutEffect(() => {
    const frame = refs.frame.current, box = refs.box.current, svg = refs.svg.current;
    const baton = refs.mBaton.current, sub = refs.mSub.current;
    if (!frame || !box || !svg || !baton || !sub) return;
    const elements = { frame, box, svg, meter: { baton, sub } };
    const r: StageRenderer = kind === 'weave' ? new WeaveRenderer(elements, story, { layoutAttr }) : new RelayRenderer(elements, story, { layoutAttr });
    r.build();
    return setup(r);
  }, [story]); // the setup callback is rebuilt on every render; the drawing only follows the story
}

function Meter({ refs }: { refs: PlateRefs }) {
  return (
    <svg className="meter" aria-hidden="true" width="1" height="1">
      <text ref={refs.mBaton} className="baton-t" />
      <text ref={refs.mSub} className="sub" />
    </svg>
  );
}

export interface StageHandle {
  count: number;
  readonly index: number;
  goto(i: number): void;
  play(): void;
  pause(): void;
}

/** A stage with caption, controls and playback: the relay line or the weave. `handle`
 *  names a window property that exposes goto/play/pause for screenshot checks; `keys`
 *  lets the left and right arrow keys step this stage. */
export function Stage({ story, kind = 'relay', handle, keys = true, foot, autoplay = true, startAt }: {
  story: Story;
  kind?: StageKind;
  handle?: string;
  keys?: boolean;
  foot?: ReactNode;
  /** Play the handoffs from the first step on load (the specimen); the live home page
   *  instead opens settled on the latest step. */
  autoplay?: boolean;
  startAt?: number;
}) {
  const refs: PlateRefs = { frame: useRef(null), box: useRef(null), svg: useRef(null), mBaton: useRef(null), mSub: useRef(null) };
  const [state, setState] = useState<PlaybackState>({ index: 0, playing: false });
  const pb = useRef<Playback | null>(null);

  usePlate(story, refs, true, (r) => {
    const playback = new Playback(r, reduceQuery, setState);
    pb.current = playback;
    if (autoplay) playback.start();
    else playback.goto(startAt ?? r.count - 1);
    const frame = refs.frame.current!;
    const ro = new ResizeObserver(() => { if (frame.clientWidth !== r.lastFrameWidth) playback.rebuild(); });
    ro.observe(frame);
    let alive = true;
    const before = r.signature();
    void document.fonts?.ready.then(() => {
      if (!alive) return;
      r.clearMeasures();
      if (r.signature() !== before) playback.rebuild();
    });
    if (handle) {
      const api: StageHandle = {
        count: r.count,
        get index() { return playback.index; },
        goto: (i) => playback.goto(i),
        play: () => playback.play(),
        pause: () => playback.pause(),
      };
      (window as unknown as Record<string, StageHandle>)[handle] = api;
    }
    return () => {
      alive = false;
      ro.disconnect();
      playback.dispose();
      pb.current = null;
    };
  }, kind);

  useEffect(() => {
    if (!keys) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
      const target = ev.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      const p = pb.current;
      if (!p) return;
      if (ev.key === 'ArrowRight' && p.index < story.beats.length - 1) p.userGo(p.index + 1);
      else if (ev.key === 'ArrowLeft' && p.index > 0) p.userGo(p.index - 1);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [story, keys]);

  const go = useCallback((i: number) => pb.current?.userGo(i), []);
  const { index, playing } = state;
  const last = story.beats.length - 1;
  const tickState = (j: number) => (j < index ? 'past' : j === index ? 'now' : 'next');
  const current = story.beats[index];

  return (
    <div className={`stage-frame stage-${kind}`} ref={refs.frame as React.RefObject<HTMLDivElement>} data-layout="h">
      <Caption story={story} index={index} />
      <nav className="ctrl" aria-label={tx('演示控制')}>
        <div className="btns">
          <button type="button" className="btn" disabled={index <= 0} onClick={() => go(index - 1)}>{tx('上一步')}</button>
          <button type="button" className="btn primary" onClick={() => (playing ? pb.current?.pause() : pb.current?.play())}>
            {playing ? tx('暂停') : index >= last ? tx('重播') : tx('播放')}
          </button>
          <button type="button" className="btn" disabled={index >= last} onClick={() => go(index + 1)}>{tx('下一步')}</button>
        </div>
        <div className="ticks" role="group" aria-label={tx('步骤进度')}>
          {story.beats.map((b, j) => (
            <button key={b.id} type="button" className="tick" data-s={tickState(j)} aria-label={tx('第 {0} 步：{1}', j + 1, b.title)}
              aria-current={j === index ? 'step' : undefined} title={b.title}
              style={j === index && current ? { ['--c' as string]: toneVar(current.holders[0]!.tone) } : undefined}
              onClick={() => go(j)}><i /></button>
          ))}
        </div>
      </nav>
      <div className="plate" ref={refs.box}>
        <svg className="track" ref={refs.svg} role="img"
          aria-label={tx('接力线：共 {0} 步，当前第 {1} 步，{2}', story.beats.length, index + 1, current?.title ?? '')}
          onClick={(ev) => { const i = (ev.target as Element).closest?.('.st') as SVGGElement | null; if (i) go(Number(i.dataset.i)); }} />
      </div>
      {foot ? <p className="foot">{foot}</p> : null}
      <Meter refs={refs} />
    </div>
  );
}

/** A still plate settled at one step, for comparing variants side by side. */
export function RelayPlate({ story, at, label }: { story: Story; at?: number; label: string }) {
  const refs: PlateRefs = { frame: useRef(null), box: useRef(null), svg: useRef(null), mBaton: useRef(null), mSub: useRef(null) };
  usePlate(story, refs, false, (r) => {
    const settleAt = at ?? r.count - 1;
    r.paintStates(settleAt);
    r.settle(settleAt);
    const frame = refs.frame.current!;
    const ro = new ResizeObserver(() => {
      if (frame.clientWidth === r.lastFrameWidth) return;
      r.build();
      r.paintStates(settleAt);
      r.settle(settleAt);
    });
    ro.observe(frame);
    return () => ro.disconnect();
  });
  return (
    <div className="still" ref={refs.frame as React.RefObject<HTMLDivElement>}>
      <div className="plate" ref={refs.box}>
        <svg className="track" ref={refs.svg} role="img" aria-label={label} />
      </div>
      <Meter refs={refs} />
    </div>
  );
}
