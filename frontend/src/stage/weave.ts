// The weave: one lane per seat, stacked by model tier, and threads that carry the work from
// lane to lane, step by step. The vertical axis is the tier: Astra and Fable on top,
// Sol and Opus in the middle, Terra and Sonnet at the bottom, with GPT and Claude side by
// side in every tier so that cross-family handoffs and parallel work read as threads
// crossing between warm and cool lanes. You and the workbench sit on two rails below,
// outside the tiers.
//
// Same material as the relay line: finished threads are lit filaments, threads ahead are
// unlit grooves in the plate, the handoff object is a glass capsule with an indicator light.

import { ease, esc, fmt, glyph, Meter, type StageElements, type StageRenderer } from './draw';
import { toneVar, type Tone } from './models';
import type { Holder, Story } from './story';
import { tx } from '../i18n';

export interface TierDef {
  label: string;
  lanes: [Tone, string][];
}

/** Top to bottom. Within a tier GPT sits above Claude, so Astra is the top lane and
 *  Sonnet the lowest. Lanes follow the seat a holder was given (see toneFor). */
export const TIERS: TierDef[] = [
  { label: '顾问与规划', lanes: [['astra', 'Astra'], ['fable', 'Fable']] },
  { label: '阶段与复核', lanes: [['sol', 'Sol'], ['opus', 'Opus']] },
  { label: '子卡实施', lanes: [['terra', 'Terra'], ['sonnet', 'Sonnet']] },
];
const RAILS: [Tone, string][] = [['you', '你'], ['bench', '工作台']];

const MIN_GUTTER = 172;
const RIGHT = 22;
const MIN_COL = 62;
const LANE = 30;
const TIER_GAP = 12;
const RAIL_GAP = 26;
const BAR_W = 26;
const BAR_H = 12;
const GLIDE = 1200;
const FIRST_GLIDE = 800;

interface Lane {
  key: Tone;
  label: string;
  y: number;
}

interface Thread {
  i: number;
  d: string;
  el?: SVGPathElement;
  len?: number;
}

let instances = 0;

export class WeaveRenderer implements StageRenderer {
  private readonly uid = `wv${++instances}`;
  private readonly meter: Meter;
  private lanes = new Map<Tone, Lane>();
  private x: number[] = [];
  private colW = 0;
  /** Left margin with the tier axis and lane names; measured, since English labels run longer. */
  private gutter = MIN_GUTTER;
  private glyphX = 112;
  private nameX = 124;
  private W = 0;
  private H = 0;
  private carryY = 0;
  private threads: Thread[] = [];
  private rev: SVGRectElement | null = null;
  private band: SVGRectElement | null = null;
  private cap: { g: SVGGElement; cap: SVGRectElement; hi: SVGRectElement; led: SVGCircleElement; ledB: SVGCircleElement; text: SVGTextElement } | null = null;
  private beads: SVGGElement[] = [];
  private raf = 0;
  private bandTop = 0;
  private bandBottom = 0;
  lastFrameWidth = -1;

  constructor(
    private readonly el: StageElements,
    private readonly story: Story,
    private readonly opts: { layoutAttr?: boolean } = {},
  ) {
    this.meter = new Meter(el.meter);
  }

  get count(): number {
    return this.story.beats.length;
  }

  signature(): string {
    return this.story.beats.map((b) => (b.carry ? fmt(this.meter.width(b.carry, 'baton')) : 0)).join();
  }

  clearMeasures(): void {
    this.meter.clear();
  }

  private lane(h: Holder): Lane {
    return this.lanes.get(h.tone) ?? this.lanes.get('unknown')!;
  }

  /** Holders of step i grouped by lane, with a small offset when two share a lane. */
  private spots(i: number): { h: Holder; lane: Lane; dx: number; dy: number }[] {
    const holders = this.story.beats[i]?.holders ?? [];
    const per = new Map<Tone, number>();
    for (const h of holders) per.set(this.lane(h).key, (per.get(this.lane(h).key) ?? 0) + 1);
    const seen = new Map<Tone, number>();
    return holders.map((h) => {
      const lane = this.lane(h);
      const n = per.get(lane.key) ?? 1;
      const k = seen.get(lane.key) ?? 0;
      seen.set(lane.key, k + 1);
      const o = k - (n - 1) / 2;
      return { h, lane, dx: o * 5, dy: o * 5 };
    });
  }

  private capsuleWidth(label: string | null): number {
    return label ? Math.ceil(this.meter.width(label, 'baton')) + 36 : 26;
  }

  build(): void {
    const { frame, box, svg } = this.el;
    const beats = this.story.beats;
    const N = beats.length;
    if (!N) { svg.innerHTML = ''; return; }
    this.lastFrameWidth = frame.clientWidth;
    if (this.opts.layoutAttr) frame.dataset.layout = frame.clientWidth >= 900 ? 'h' : 's';
    box.dataset.layout = 'w';
    const cs = getComputedStyle(box);
    const inner = box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const tierW = Math.max(...TIERS.map((tier) => this.meter.width(tx(tier.label), 'sub')));
    const names = [...TIERS.flatMap((tier) => tier.lanes.map(([, label]) => tx(label))), ...RAILS.map(([, label]) => tx(label)), tx('未认出')];
    const nameW = Math.max(...names.map((name) => this.meter.width(name, 'sub') * 1.18));
    this.glyphX = Math.ceil(30 + tierW + 16);
    this.nameX = this.glyphX + 12;
    this.gutter = Math.max(MIN_GUTTER, Math.ceil(this.nameX + nameW + 14));
    // Each column is at least as wide as its step title; a long weave then scrolls sideways.
    // Titles are 12.5px sans, measured with the 11px mono meter: CJK runs wider, Latin narrower.
    const titleW = Math.max(...beats.map((b) => this.meter.width(b.title, 'sub') * (/[\u4e00-\u9fff]/.test(b.title) ? 1.15 : 0.95)));
    const colMin = Math.max(MIN_COL, Math.ceil(titleW + 10));
    this.W = Math.max(Math.floor(inner), this.gutter + RIGHT + N * colMin);
    this.colW = (this.W - this.gutter - RIGHT) / N;
    this.x = beats.map((_, i) => this.gutter + this.colW * (i + 0.5));

    const rulerY = 22;
    const titleY = 52;
    const top = 84;
    this.lanes.clear();
    let y = top + LANE / 2;
    const tierSpans: { label: string; y1: number; y2: number }[] = [];
    TIERS.forEach((tier, t) => {
      const y1 = y;
      tier.lanes.forEach(([key, label], k) => {
        this.lanes.set(key, { key, label, y });
        if (k < tier.lanes.length - 1) y += LANE;
      });
      tierSpans.push({ label: tier.label, y1, y2: y });
      y += LANE + (t < TIERS.length - 1 ? TIER_GAP : 0);
    });
    const axisBottom = y - LANE / 2;
    const usesUnknown = beats.some((b) => b.holders.some((h) => !this.lanes.has(h.tone) && h.tone !== 'you' && h.tone !== 'bench'));
    if (usesUnknown) { this.lanes.set('unknown', { key: 'unknown', label: tx('未认出'), y }); y += LANE; }
    y += RAIL_GAP - LANE / 2;
    const railsTop = y;
    y += LANE / 2;
    RAILS.forEach(([key, label]) => { this.lanes.set(key, { key, label, y }); y += LANE; });
    const lanesBottom = y - LANE / 2;
    this.bandTop = top - 4;
    this.bandBottom = lanesBottom + 4;
    this.carryY = lanesBottom + 26;
    const bracketY = this.carryY + 36;
    this.H = this.story.loops.length ? bracketY + 28 : this.carryY + 24;

    const u = this.uid;
    let defs = '';
    let base = '';
    let gutter = '';
    // Tier bands, the tier axis and the lane grooves.
    tierSpans.forEach((s, t) => {
      if (t % 2 === 0) {
        base += `<rect class="tier-band" x="8" y="${fmt(s.y1 - LANE / 2)}" width="${fmt(this.W - 16)}" height="${fmt(s.y2 - s.y1 + LANE)}" rx="6"/>`;
        gutter += `<rect class="tier-band" x="8" y="${fmt(s.y1 - LANE / 2)}" width="${this.gutter}" height="${fmt(s.y2 - s.y1 + LANE)}" rx="6"/>`;
      }
      const cy = (s.y1 + s.y2) / 2;
      const label = `<line class="tier-tick" x1="16" y1="${fmt(cy)}" x2="22" y2="${fmt(cy)}"/>`
        + `<text class="tier" x="30" y="${fmt(cy + 4)}">${esc(tx(s.label))}</text>`;
      base += label;
      gutter += label;
    });
    const axis = `<line class="tier-axis" x1="16" y1="${fmt(top - 10)}" x2="16" y2="${fmt(axisBottom)}"/>`
      + `<path class="tier-axis" d="M12.5 ${fmt(top - 5)} L16 ${fmt(top - 11)} L19.5 ${fmt(top - 5)}"/>`
      + tx('<text class="tier" x="16" y="{0}" text-anchor="middle">等级</text>', fmt(top - 18));
    base += axis + `<line class="rail-rule" x1="8" y1="${fmt(railsTop - 6)}" x2="${fmt(this.W - 8)}" y2="${fmt(railsTop - 6)}"/>`;
    gutter += axis + `<line class="rail-rule" x1="8" y1="${fmt(railsTop - 6)}" x2="${this.gutter}" y2="${fmt(railsTop - 6)}"/>`;
    for (const lane of this.lanes.values()) {
      const label = `<g class="lane-label" style="--c:${toneVar(lane.key)}" transform="translate(${fmt(this.glyphX)} ${fmt(lane.y)})"><g class="in">${glyph({ tone: lane.key }, 3.6)}</g></g>`
        + `<text class="lane-name" x="${fmt(this.nameX)}" y="${fmt(lane.y + 4.5)}">${esc(tx(lane.label))}</text>`;
      base += label + `<g class="trk groove"><path d="M${this.gutter - 6} ${fmt(lane.y)} L${fmt(this.W - RIGHT + 6)} ${fmt(lane.y)}"/></g>`;
      gutter += label;
    }

    // Threads: every holder of step i-1 hands to every holder of step i, except that equal
    // counts pair up in order (leaf A to leaf A).
    this.threads = [];
    const seen = new Set<string>();
    const first = this.spots(0);
    for (const s of first) {
      const x2 = this.x[0]! - BAR_W / 2 + s.dx;
      this.threads.push({ i: 0, d: `M${this.gutter - 6} ${fmt(s.lane.y + s.dy)} L${fmt(x2)} ${fmt(s.lane.y + s.dy)}` });
    }
    for (let i = 1; i < N; i++) {
      const A = this.spots(i - 1), B = this.spots(i);
      const pairs: [typeof A[number], typeof B[number]][] = [];
      if (A.length === B.length) A.forEach((a, k) => pairs.push([a, B[k]!]));
      else for (const a of A) for (const b of B) pairs.push([a, b]);
      for (const [a, b] of pairs) {
        const key = `${i}|${a.lane.key}|${a.dy}|${b.lane.key}|${b.dy}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const x1 = this.x[i - 1]! + BAR_W / 2 + a.dx, y1 = a.lane.y + a.dy;
        const x2 = this.x[i]! - BAR_W / 2 + b.dx, y2 = b.lane.y + b.dy;
        const c = (x2 - x1) * 0.55;
        const d = Math.abs(y2 - y1) < 0.5
          ? `M${fmt(x1)} ${fmt(y1)} L${fmt(x2)} ${fmt(y2)}`
          : `M${fmt(x1)} ${fmt(y1)} C${fmt(x1 + c)} ${fmt(y1)} ${fmt(x2 - c)} ${fmt(y2)} ${fmt(x2)} ${fmt(y2)}`;
        const n = this.threads.length;
        defs += `<linearGradient id="${u}-g${n}" gradientUnits="userSpaceOnUse" x1="${fmt(x1)}" y1="0" x2="${fmt(x2)}" y2="0">`
          + `<stop offset="0.2" style="stop-color:${toneVar(a.h.tone)}"/><stop offset="0.8" style="stop-color:${toneVar(b.h.tone)}"/></linearGradient>`;
        this.threads.push({ i, d });
      }
    }
    // The lead-in thread takes the first holder's colour.
    first.forEach((s, k) => {
      defs += `<linearGradient id="${u}-g${k}" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="1" y2="0"><stop offset="0" style="stop-color:${toneVar(s.h.tone)}"/></linearGradient>`;
    });
    const grooves = this.threads.map((t, n) => `<path d="${t.d}" data-n="${n}" data-i="${t.i}"/>`).join('');
    const lit = this.threads.map((t, n) => `<path d="${t.d}" data-i="${t.i}" stroke="url(#${u}-g${n})"/>`).join('');
    const hi = this.threads.map((t) => `<path d="${t.d}" data-i="${t.i}"/>`).join('');

    // Columns: title, fork note, slots, and the lit bars (clipped with the threads).
    let cols = '';
    let bars = '';
    beats.forEach((b, i) => {
      const x = this.x[i]!;
      let inner = `<rect class="hit" x="${fmt(x - this.colW / 2)}" y="0" width="${fmt(this.colW)}" height="${this.H}"/>`
        + `<text class="ttl" x="${fmt(x)}" y="${titleY}" text-anchor="middle">${esc(b.title)}</text>`;
      const spots = this.spots(i);
      if (b.fork) inner += `<text class="wl${b.fork === 'parallel' ? ' par' : ''}" x="${fmt(x + 1)}" y="${titleY + 15}" text-anchor="middle">${b.fork === 'blind' ? tx('互不可见') : tx('并行')}</text>`;
      if (b.fork === 'blind' && spots.length > 1) {
        const ys = spots.map((s) => s.lane.y).sort((p, q) => p - q);
        const ya = ys[0]!, yb = ys[ys.length - 1]!;
        if (yb - ya > BAR_H + 6) inner += `<line class="blind" x1="${fmt(x)}" y1="${fmt(ya + BAR_H / 2 + 4)}" x2="${fmt(x)}" y2="${fmt(yb - BAR_H / 2 - 4)}"/>`;
      }
      for (const s of spots) {
        const bx = x - BAR_W / 2 + s.dx, by = s.lane.y - BAR_H / 2 + s.dy;
        const style = `style="--c:${toneVar(s.h.tone)}"`;
        inner += `<rect class="halo" x="${fmt(bx - 6)}" y="${fmt(by - 6)}" width="${BAR_W + 12}" height="${BAR_H + 12}" rx="${(BAR_H + 12) / 2}" ${style}/>`
          + `<rect class="slot" x="${fmt(bx)}" y="${fmt(by)}" width="${BAR_W}" height="${BAR_H}" rx="${BAR_H / 2}"/>`;
        const unconfirmed = !s.h.plain && s.h.reportedModel === null;
        bars += `<g class="bar${unconfirmed ? ' unconfirmed' : ''}" data-i="${i}" ${style}>`
          + `<rect class="bar-glow" x="${fmt(bx)}" y="${fmt(by)}" width="${BAR_W}" height="${BAR_H}" rx="${BAR_H / 2}"/>`
          + `<rect class="bar-body" x="${fmt(bx)}" y="${fmt(by)}" width="${BAR_W}" height="${BAR_H}" rx="${BAR_H / 2}"/>`
          + `<line class="bar-hi" x1="${fmt(bx + 5)}" y1="${fmt(by + 2.6)}" x2="${fmt(bx + BAR_W - 5)}" y2="${fmt(by + 2.6)}"/></g>`;
      }
      cols += `<g class="st col" data-i="${i}">${inner}</g>`;
    });

    // Phase ruler and rework bracket, like dimension lines on a drawing.
    let ruler = '';
    for (const span of this.story.phases) {
      const x1 = this.x[span.from]! - this.colW / 2 + 4, x2 = this.x[span.to]! + this.colW / 2 - 4;
      const cx = (x1 + x2) / 2, half = this.meter.width(span.label, 'sub') / 2 + 8;
      ruler += `<g class="ruler"><line x1="${fmt(x1)}" y1="${rulerY - 5}" x2="${fmt(x1)}" y2="${rulerY + 5}"/><line x1="${fmt(x2)}" y1="${rulerY - 5}" x2="${fmt(x2)}" y2="${rulerY + 5}"/>`
        + (x2 - x1 > 2 * half + 8 ? `<line x1="${fmt(x1)}" y1="${rulerY}" x2="${fmt(cx - half)}" y2="${rulerY}"/><line x1="${fmt(cx + half)}" y1="${rulerY}" x2="${fmt(x2)}" y2="${rulerY}"/>` : '')
        + `<text class="rt" x="${fmt(cx)}" y="${rulerY + 4}" text-anchor="middle">${esc(span.label)}</text></g>`;
    }
    let bracket = '';
    for (const loop of this.story.loops) {
      const xa = this.x[loop.from]!, xb = this.x[loop.to]!, yb = bracketY;
      bracket += `<g class="bracket" data-to="${loop.to}"><path d="M${fmt(xb)} ${yb - 9} L${fmt(xb)} ${yb} L${fmt(xa)} ${yb} L${fmt(xa)} ${yb - 11}"/>`
        + `<path class="head" d="M${fmt(xa - 3.5)} ${yb - 6.5} L${fmt(xa)} ${yb - 12} L${fmt(xa + 3.5)} ${yb - 6.5}"/>`
        + `<text class="rt" x="${fmt((xa + xb) / 2)}" y="${yb + 15}" text-anchor="middle">${esc(loop.label)}</text></g>`;
    }

    const pool = Math.max(1, ...beats.map((_, i) => this.threads.filter((t) => t.i === i).length));
    let beads = '';
    for (let k = 0; k < pool; k++) beads += '<g class="bead" style="display:none"><circle class="spark-b" r="4.5"/><circle class="spark-c" r="1.7"/></g>';

    svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`);
    svg.setAttribute('width', String(this.W));
    svg.setAttribute('height', String(this.H));
    svg.innerHTML = `<defs>${defs}`
      + `<linearGradient id="${u}-cap" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--cap-top)"/><stop offset="1" style="stop-color:var(--cap-bot)"/></linearGradient>`
      + `<filter id="${u}-blur" filterUnits="userSpaceOnUse" x="0" y="0" width="${this.W}" height="${this.H}"><feGaussianBlur stdDeviation="2.4"/></filter>`
      + `<clipPath id="${u}-reveal"><rect x="0" y="0" width="${this.W}" height="${this.H}"/></clipPath></defs>`
      + `<rect class="now-band" x="0" y="${fmt(this.bandTop)}" width="${fmt(this.colW)}" height="${fmt(this.bandBottom - this.bandTop)}" rx="8"/>`
      + base + ruler
      + `<g class="trk groove threads">${grooves}</g>`
      + `<g class="cols">${cols}</g>`
      + `<g class="lit" clip-path="url(#${u}-reveal)">`
      + `<g class="trk bloom" filter="url(#${u}-blur)">${lit}</g>`
      + `<g class="trk core thread-core">${lit}</g><g class="trk core-hi">${hi}</g>`
      + `<g class="bars" >${bars}</g></g>`
      + bracket
      + `<line class="carry-rule" x1="${this.gutter - 6}" y1="${fmt(this.carryY)}" x2="${fmt(this.W - RIGHT + 6)}" y2="${fmt(this.carryY)}"/>`
      + tx('<text class="tier" x="30" y="{0}">交接物</text>', fmt(this.carryY + 4))
      + '<g class="baton" style="display:none"><rect class="cap" y="-12.5" height="25" rx="12.5"/>'
      + '<rect class="cap-hi" y="-11.5" height="23" rx="11.5"/><circle class="led-b" r="5.5"/><circle class="led" r="2.4"/>'
      + '<text class="baton-t" y="3.8"></text></g>'
      + `<g class="spark">${beads}</g>`;

    svg.querySelectorAll<SVGRectElement>('.cap').forEach((r) => r.setAttribute('fill', `url(#${u}-cap)`));
    svg.querySelectorAll<SVGRectElement>('.bar-glow').forEach((r) => r.setAttribute('filter', `url(#${u}-blur)`));
    this.rev = svg.querySelector<SVGRectElement>(`#${u}-reveal rect`);
    this.band = svg.querySelector<SVGRectElement>('.now-band');
    svg.querySelectorAll<SVGPathElement>('.threads > path').forEach((p) => {
      const t = this.threads[Number(p.dataset.n)];
      if (t) { t.el = p; t.len = p.getTotalLength(); }
    });
    const g = svg.querySelector<SVGGElement>('.baton')!;
    this.cap = {
      g,
      cap: g.querySelector<SVGRectElement>('.cap')!,
      hi: g.querySelector<SVGRectElement>('.cap-hi')!,
      led: g.querySelector<SVGCircleElement>('.led')!,
      ledB: g.querySelector<SVGCircleElement>('.led-b')!,
      text: g.querySelector<SVGTextElement>('.baton-t')!,
    };
    this.beads = [...svg.querySelectorAll<SVGGElement>('.bead')];
    this.buildGutter(gutter + tx('<text class="tier" x="30" y="{0}">交接物</text>', fmt(this.carryY + 4)));
  }

  /** When the plate is narrower than the weave it scrolls sideways; a copy of the lane
   *  labels then stays pinned on the left so every lane keeps its name. */
  private buildGutter(content: string): void {
    const box = this.el.box;
    let g = box.querySelector<SVGSVGElement>(':scope > svg.weave-gutter');
    if (!g) {
      g = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      g.setAttribute('class', 'weave-gutter');
      g.setAttribute('aria-hidden', 'true');
      box.appendChild(g);
      box.addEventListener('scroll', () => box.classList.toggle('scrolled', box.scrollLeft > 4), { passive: true });
    }
    g.setAttribute('viewBox', `0 0 ${(this.gutter + 8)} ${this.H}`);
    g.setAttribute('width', String((this.gutter + 8)));
    g.setAttribute('height', String(this.H));
    g.innerHTML = `<defs><linearGradient id="${this.uid}-fade" x1="0" y1="0" x2="1" y2="0"><stop offset="0.88" style="stop-color:var(--plate);stop-opacity:1"/><stop offset="1" style="stop-color:var(--plate);stop-opacity:0"/></linearGradient></defs>`
      + `<rect x="0" y="0" width="${(this.gutter + 8)}" height="${this.H}" fill="url(#${this.uid}-fade)"/>${content}`;
    box.classList.toggle('scrolled', box.scrollLeft > 4);
  }

  // ---- Painting ----

  private setReveal(x: number): void {
    this.rev?.setAttribute('width', String(fmt(Math.max(0, x))));
  }

  private placeCapsule(x: number, w: number, label: string, tone: string, textAlpha: number): number {
    const cx = Math.max(this.gutter + w / 2, Math.min(this.W - RIGHT - w / 2, x));
    const c = this.cap;
    if (!c) return cx;
    c.g.setAttribute('transform', `translate(${fmt(cx)} ${fmt(this.carryY)})`);
    c.g.style.setProperty('--c', tone);
    c.cap.setAttribute('x', String(fmt(-w / 2)));
    c.cap.setAttribute('width', String(fmt(w)));
    c.hi.setAttribute('x', String(fmt(-w / 2 + 1)));
    c.hi.setAttribute('width', String(fmt(Math.max(0, w - 2))));
    const ledX = label ? -w / 2 + 12.5 : 0;
    c.led.setAttribute('cx', String(fmt(ledX)));
    c.ledB.setAttribute('cx', String(fmt(ledX)));
    if (c.text.textContent !== label) c.text.textContent = label;
    c.text.setAttribute('x', String(fmt(-w / 2 + 23)));
    c.text.style.opacity = String(textAlpha);
    c.g.classList.toggle('empty', !label);
    c.g.style.display = '';
    return cx;
  }

  private keepInView(x: number, width: number): void {
    const box = this.el.box;
    if (box.scrollWidth <= box.clientWidth + 1) return;
    const left = box.scrollLeft + (box.scrollLeft > 4 ? (this.gutter + 8) : 0);
    const right = box.scrollLeft + box.clientWidth;
    const margin = width / 2 + 6;
    if (x - margin < left || x + margin > right) {
      box.scrollLeft = Math.max(0, x - (box.clientWidth + (this.gutter + 8)) / 2);
    }
  }

  settle(i: number): void {
    if (!this.x.length) return;
    this.setReveal(i < 0 ? this.gutter - 8 : this.x[i]! + BAR_W / 2 + 6);
    this.beads.forEach((b) => { b.style.display = 'none'; });
    if (i < 0) { if (this.cap) this.cap.g.style.display = 'none'; return; }
    const beat = this.story.beats[i]!;
    const width = this.capsuleWidth(beat.carry);
    const x = this.placeCapsule(this.x[i]!, width, beat.carry ?? '', toneVar(beat.holders[0]!.tone), 1);
    this.keepInView(x, width);
  }

  paintStates(i: number): void {
    const state = (j: number): string => (j < i ? 'past' : j === i ? 'now' : 'next');
    const svg = this.el.svg;
    svg.querySelectorAll<SVGGElement>('.col').forEach((g) => { g.dataset.s = state(Number(g.dataset.i)); });
    svg.querySelectorAll<SVGElement>('.lit [data-i]').forEach((p) => { p.dataset.s = state(Number(p.dataset.i)); });
    svg.querySelectorAll<SVGGElement>('.bracket').forEach((g) => { g.dataset.s = Number(g.dataset.to) <= i ? 'past' : 'next'; });
    if (this.band && this.x[i] !== undefined) {
      this.band.setAttribute('x', String(fmt(this.x[i]! - this.colW / 2 + 2)));
      this.band.setAttribute('width', String(fmt(this.colW - 4)));
    }
  }

  cancel(): void {
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = 0; }
  }

  /** The handoff: a bead runs along every thread into step i (a shuttle through the
   *  weave), the lit threads follow it, and the capsule slides along the bottom rail. */
  glide(i: number, done?: () => void): void {
    this.cancel();
    this.settle(i - 1);
    const beats = this.story.beats;
    const beat = beats[i]!;
    const movers = this.threads.filter((t) => t.i === i);
    const label = beat.carry ?? '';
    const prev = i > 0 ? beats[i - 1]! : null;
    const w0 = prev ? this.capsuleWidth(prev.carry) : 26, w1 = this.capsuleWidth(beat.carry);
    const x0 = i > 0 ? this.x[i - 1]! : this.gutter, x1 = this.x[i]!;
    const keep = (prev?.carry ?? '') === label;
    const tone = toneVar(beat.holders[0]!.tone);
    const dur = i === 0 ? FIRST_GLIDE : GLIDE;
    let start = 0;
    const step = (now: number): void => {
      if (!start) start = now;
      const raw = Math.min(1, (now - start) / dur), e = ease(raw), q = Math.min(1, raw / 0.28);
      let front = 0;
      movers.forEach((t, k) => {
        const bead = this.beads[k];
        if (!bead || !t.el || t.len === undefined) return;
        const p = t.el.getPointAtLength(e * t.len);
        bead.style.display = raw < 1 ? '' : 'none';
        bead.style.setProperty('--c', tone);
        bead.setAttribute('transform', `translate(${fmt(p.x)} ${fmt(p.y)})`);
        front = Math.max(front, p.x);
      });
      this.setReveal(front + 4);
      this.placeCapsule(x0 + (x1 - x0) * e, w0 + (w1 - w0) * q, label, tone, keep ? 1 : q);
      if (raw < 1) this.raf = requestAnimationFrame(step);
      else { this.raf = 0; this.settle(i); done?.(); }
    };
    this.raf = requestAnimationFrame(step);
  }
}
