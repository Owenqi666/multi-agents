// The relay line, drawn as SVG on an instrument plate. Ported from the 接力线 sample Owen
// chose (multi-agent/stage-samples/relay.html) and restyled after the 精致写实 draft: work
// that is done is a lit filament in the holder's colour, work ahead is an unlit groove in
// the plate, and the handoff object is a small glass capsule with an indicator light.
//
// Geometry: m runs along the line, lane is the sideways offset of a parallel branch.

import { ease, esc, fmt, glyph, type StageElements, type StageRenderer } from './draw';
import { toneVar } from './models';
import type { Beat, Story } from './story';
import { tx } from '../i18n';

const BLIND = '互不可见';
const PARALLEL = '并行';
const GLIDE = 1200;
const FIRST_GLIDE = 800;

interface Seg {
  i: number;
  ta: number;
  tb: number;
  mid: number;
  ca: string;
  cb: string;
  d: string;
  el?: SVGPathElement;
  len?: number;
}

interface Baton {
  g: SVGGElement;
  cap: SVGRectElement;
  hi: SVGRectElement;
  led: SVGCircleElement;
  ledB: SVGCircleElement;
  text: SVGTextElement;
}

export type RelayElements = StageElements;

export interface RelayOptions {
  /** Narrowest spacing between two stations before the line turns vertical. */
  minUnit?: number;
  /** Write data-layout="h" | "v" on the frame so the page grid can follow. */
  layoutAttr?: boolean;
  /** Widest the vertical drawing gets. */
  maxVertical?: number;
}

let instances = 0;

export class RelayRenderer implements StageRenderer {
  private readonly uid = `rl${++instances}`;
  private beats: Beat[];
  horiz = true;
  private W = 0;
  private H = 0;
  private axis = 0;
  private off = 0;
  private lead = 0;
  private t: number[] = [];
  private capW: number[] = [];
  private segs: Seg[] = [];
  private rev: SVGRectElement | null = null;
  private batons: Baton[] = [];
  private stations: SVGGElement[] = [];
  private walls: SVGGElement[] = [];
  private spark: SVGGElement | null = null;
  private raf = 0;
  private cache = new Map<string, number>();
  lastFrameWidth = -1;

  constructor(
    private readonly el: RelayElements,
    private story: Story,
    private readonly opts: RelayOptions = {},
  ) {
    this.beats = story.beats;
  }

  get count(): number {
    return this.beats.length;
  }

  setStory(story: Story): void {
    this.story = story;
    this.beats = story.beats;
  }

  clearMeasures(): void {
    this.cache.clear();
  }

  measure(text: string, kind: 'baton' | 'sub'): number {
    const key = `${kind}|${text}`;
    let w = this.cache.get(key);
    if (w === undefined) {
      const m = this.el.meter[kind];
      m.textContent = text;
      w = m.getComputedTextLength();
      this.cache.set(key, w);
    }
    return w;
  }

  /** Width signature of the capsule labels; changes once the web fonts arrive. */
  signature(): string {
    return this.beats.map((b) => (b.carry ? fmt(this.measure(b.carry, 'baton')) : 0)).join();
  }

  private multi(i: number): boolean {
    return (this.beats[i]?.holders.length ?? 0) > 1;
  }

  private lanes(i: number): number[] {
    const n = this.beats[i]?.holders.length ?? 1;
    return Array.from({ length: n }, (_, k) => (k - (n - 1) / 2) * 2 * this.off);
  }

  private P(m: number, lane: number): [number, number] {
    return this.horiz ? [m, this.axis + lane] : [this.axis + lane, m];
  }

  private rpath(pts: [number, number][], r: number): string {
    const p = pts.map(([m, lane]) => this.P(m, lane));
    const first = p[0]!;
    let d = `M${fmt(first[0])} ${fmt(first[1])}`;
    for (let i = 1; i < p.length - 1; i++) {
      const a = p[i - 1]!, b = p[i]!, c = p[i + 1]!;
      const l1 = Math.hypot(b[0] - a[0], b[1] - a[1]);
      const l2 = Math.hypot(c[0] - b[0], c[1] - b[1]);
      const r1 = Math.min(r, l1 / 2), r2 = Math.min(r, l2 / 2);
      d += ` L${fmt(b[0] - ((b[0] - a[0]) / l1) * r1)} ${fmt(b[1] - ((b[1] - a[1]) / l1) * r1)}`
        + ` Q${fmt(b[0])} ${fmt(b[1])} ${fmt(b[0] + ((c[0] - b[0]) / l2) * r2)} ${fmt(b[1] + ((c[1] - b[1]) / l2) * r2)}`;
    }
    const e = p[p.length - 1]!;
    return `${d} L${fmt(e[0])} ${fmt(e[1])}`;
  }

  private capsuleWidth(label: string | null): number {
    return label ? Math.ceil(this.measure(label, 'baton')) + 36 : 26;
  }

  build(): void {
    const { frame, box, svg } = this.el;
    const N = this.beats.length;
    const LAST = N - 1;
    if (!N) {
      svg.innerHTML = '';
      return;
    }
    const minUnit = this.opts.minUnit ?? 74;
    this.capW = this.beats.map((b) => this.capsuleWidth(b.carry));
    const anyFork = this.beats.some((_, i) => this.multi(i));
    const forkHalf = Math.max(0, ...this.beats.map((_, i) => (this.multi(i) ? (this.capW[i] ?? 0) / 2 : 0)));
    const maxHalf = Math.max(...this.capW) / 2;
    // Room at each end for the capsule resting there and, on a fork, the 互不可见 bar.
    const edge = (i: number): number => Math.max(58, (this.capW[i] ?? 0) / 2 + 10, this.multi(i) ? forkHalf + 24 : 0);
    const left = edge(0), right = edge(LAST);
    const wts: number[] = [];
    for (let i = 0; i < LAST; i++) wts.push(this.multi(i) || this.multi(i + 1) ? 1.45 : 1);
    const needed = left + right + wts.reduce((a, b) => a + b, 0) * minUnit;
    this.lastFrameWidth = frame.clientWidth;
    this.horiz = frame.clientWidth >= needed + 4;
    if (this.opts.layoutAttr) frame.dataset.layout = this.horiz ? 'h' : 'v';
    box.dataset.layout = this.horiz ? 'h' : 'v';
    const cs = getComputedStyle(box);
    const inner = box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    this.W = Math.max(280, Math.floor(this.horiz ? inner : Math.min(inner, this.opts.maxVertical ?? 440)));
    const t: number[] = [];
    let kink: number;
    let titleY = 0;
    let bracketY = 0;
    const rulerY = 22;

    if (this.horiz) {
      this.off = anyFork ? 46 : 0;
      this.lead = 34;
      kink = 28;
      const unit = (this.W - left - right) / Math.max(1, wts.reduce((a, b) => a + b, 0));
      t[0] = left;
      wts.forEach((w, i) => { t[i + 1] = t[i]! + unit * w; });
      titleY = rulerY + 34;
      this.axis = titleY + (anyFork ? 58 + this.off : 34);
      const bottom = this.axis + this.off + 51;
      bracketY = bottom + 26;
      this.H = this.story.loops.length ? bracketY + 30 : bottom + 18;
    } else {
      this.off = anyFork ? forkHalf + 9 : 0;
      this.lead = 22;
      kink = 18;
      this.axis = Math.max(maxHalf, this.off + forkHalf) + 6;
      t[0] = this.lead + 16;
      for (let i = 0; i < LAST; i++) {
        const gap = this.multi(i + 1) ? kink + this.off + 70 : this.multi(i) ? 46 + this.off + kink : 66;
        t[i + 1] = t[i]! + gap;
      }
      this.H = t[LAST]! + 40;
    }
    this.t = t;

    // Segments: one per handoff and branch, each blending from giver's colour to receiver's.
    const tone = (i: number, k: number): string => toneVar(this.beats[i]!.holders[k]!.tone);
    const segs: Seg[] = [{ i: 0, ta: t[0]! - this.lead, tb: t[0]!, mid: 0.5, ca: tone(0, 0), cb: tone(0, 0), d: this.rpath([[t[0]! - this.lead, 0], [t[0]!, 0]], 0) }];
    for (let i = 1; i < N; i++) {
      const A = this.lanes(i - 1), B = this.lanes(i), span = t[i]! - t[i - 1]!;
      for (let k = 0; k < Math.max(A.length, B.length); k++) {
        const ka = Math.min(k, A.length - 1), kb = Math.min(k, B.length - 1);
        const la = A[ka]!, lb = B[kb]!, d = Math.abs(lb - la);
        let pts: [number, number][] = [[t[i - 1]!, la], [t[i]!, lb]];
        let mid = 0.5;
        if (d && A.length <= B.length) {
          const s = t[i - 1]! + kink;
          pts = [[t[i - 1]!, la], [s, la], [s + d, lb], [t[i]!, lb]];
          mid = (kink + d / 2) / span;
        } else if (d) {
          const e = t[i]! - kink;
          pts = [[t[i - 1]!, la], [e - d, la], [e, lb], [t[i]!, lb]];
          mid = 1 - (kink + d / 2) / span;
        }
        segs.push({ i, ta: t[i - 1]!, tb: t[i]!, mid, ca: tone(i - 1, ka), cb: tone(i, kb), d: this.rpath(pts, 16) });
      }
    }
    this.segs = segs;

    const u = this.uid;
    let defs = '';
    let paths = '';
    segs.forEach((s, n) => {
      const [x1, y1] = this.P(s.ta, 0), [x2, y2] = this.P(s.tb, 0);
      defs += `<linearGradient id="${u}-g${n}" gradientUnits="userSpaceOnUse" x1="${fmt(x1)}" y1="${fmt(y1)}" x2="${fmt(x2)}" y2="${fmt(y2)}">`
        + `<stop offset="${fmt(Math.max(0, s.mid - 0.1) * 100)}%" style="stop-color:${s.ca}"/>`
        + `<stop offset="${fmt(Math.min(1, s.mid + 0.1) * 100)}%" style="stop-color:${s.cb}"/></linearGradient>`;
      paths += `<path d="${s.d}" data-n="${n}"/>`;
    });
    const litPaths = segs.map((s, n) => `<path d="${s.d}" stroke="url(#${u}-g${n})"/>`).join('');

    let pinsUnlit = '';
    let pinsLit = '';
    let labels = '';
    let walls = '';
    const lxFork = this.axis + this.off + forkHalf + 14;
    const lxSingle = this.axis + maxHalf + 14;

    this.beats.forEach((b, i) => {
      const ls = this.lanes(i);
      const n = ls.length;
      let inner = '';
      const [hx, hy] = this.P(t[i]!, 0);
      inner += this.horiz
        ? `<rect class="hit" x="${fmt(hx - 40)}" y="0" width="80" height="${this.H}"/>`
        : `<rect class="hit" x="0" y="${fmt(hy - (n > 1 ? 46 : 30))}" width="${this.W}" height="${n > 1 ? 92 : 60}"/>`;
      if (this.horiz) inner += `<text class="ttl" x="${fmt(hx)}" y="${titleY}" text-anchor="middle">${esc(b.title)}</text>`;

      b.holders.forEach((h, k) => {
        const [x, y] = this.P(t[i]!, ls[k]!);
        const style = `style="--c:${toneVar(h.tone)}"`;
        inner += `<circle class="halo" cx="${fmt(x)}" cy="${fmt(y)}" r="15" ${style}/>`;
        const pin = `<g class="pin" transform="translate(${fmt(x)} ${fmt(y)})" ${style}><circle class="ring" r="7"/><g class="in">${glyph(h, 3.1)}</g></g>`;
        pinsUnlit += pin;
        pinsLit += pin;
        const cls = !h.plain && h.reportedModel === null ? 'nm unconfirmed' : 'nm';
        if (this.horiz) {
          const up = n > 1 && ls[k]! < 0;
          inner += `<text class="${cls}" x="${fmt(x)}" y="${fmt(up ? y - 38 : y + 35)}" text-anchor="middle">${esc(h.name)}</text>`;
          if (h.sub) inner += `<text class="sub" x="${fmt(x)}" y="${fmt(up ? y - 23 : y + 51)}" text-anchor="middle">${esc(h.sub)}</text>`;
        } else if (n === 1) {
          inner += `<text class="ttl" x="${fmt(lxSingle)}" y="${fmt(y - 4)}">${esc(b.title)}</text>`;
          inner += `<text class="sub" x="${fmt(lxSingle)}" y="${fmt(y + 13)}"><tspan class="${cls === 'nm' ? 'sub-nm' : 'sub-nm unconfirmed'}">${esc(h.name)}</tspan>${h.sub ? `<tspan dx="6">${esc(h.sub)}</tspan>` : ''}</text>`;
        } else {
          // Narrow drawing: the parallel holders are listed beside the fork, tied to their
          // branch by mark and colour.
          if (k === 0) inner += `<text class="ttl" x="${fmt(lxFork)}" y="${fmt(t[i]! - (n * 17) / 2 - 2)}">${esc(b.title)}</text>`;
          const yk = t[i]! - (n * 17) / 2 + 17 * (k + 1) - 2;
          inner += `<g class="pin solid" transform="translate(${fmt(lxFork + 5)} ${fmt(yk - 4)})" ${style}><g class="in">${glyph(h, 3.4)}</g></g>`
            + `<text class="sub" x="${fmt(lxFork + 15)}" y="${fmt(yk)}"><tspan class="${cls === 'nm' ? 'sub-nm' : 'sub-nm unconfirmed'}">${esc(h.name)}</tspan>${h.sub ? `<tspan dx="6">${esc(h.sub)}</tspan>` : ''}</text>`;
        }
      });
      labels += `<g class="st" data-i="${i}">${inner}</g>`;

      if (n > 1) {
        const [x, y] = this.P(t[i]!, 0);
        if (b.fork === 'parallel') {
          walls += `<g class="wall par" data-i="${i}"><text class="wl" x="${fmt(this.horiz ? x : x)}" y="${fmt(this.horiz ? y + 3.5 : y - 26)}" text-anchor="middle">${tx(PARALLEL)}</text></g>`;
        } else if (this.horiz) {
          const L = forkHalf + 14;
          walls += `<g class="wall" data-i="${i}"><line x1="${fmt(x - L)}" y1="${y}" x2="${fmt(x - 34)}" y2="${y}"/><line x1="${fmt(x + 34)}" y1="${y}" x2="${fmt(x + L)}" y2="${y}"/>`
            + `<line x1="${fmt(x - L)}" y1="${y - 5}" x2="${fmt(x - L)}" y2="${y + 5}"/><line x1="${fmt(x + L)}" y1="${y - 5}" x2="${fmt(x + L)}" y2="${y + 5}"/>`
            + `<text class="wl" x="${fmt(x + 1.2)}" y="${y + 3.5}" text-anchor="middle">${tx(BLIND)}</text></g>`;
        } else {
          walls += `<g class="wall" data-i="${i}"><line x1="${x}" y1="${fmt(y - 56)}" x2="${x}" y2="${fmt(y - 42)}"/><line x1="${x}" y1="${fmt(y - 16)}" x2="${x}" y2="${fmt(y + 26)}"/>`
            + `<line x1="${x - 5}" y1="${fmt(y - 56)}" x2="${x + 5}" y2="${fmt(y - 56)}"/><line x1="${x - 5}" y1="${fmt(y + 26)}" x2="${x + 5}" y2="${fmt(y + 26)}"/>`
            + `<text class="wl" x="${fmt(x + 1.2)}" y="${fmt(y - 25)}" text-anchor="middle">${tx(BLIND)}</text></g>`;
        }
      }
    });

    // Phase ruler and rework bracket, drawn like dimension lines on a drawing.
    let ruler = '';
    let bracket = '';
    if (this.horiz) {
      for (const span of this.story.phases) {
        const x1 = span.from === 0 ? t[0]! - this.lead : (t[span.from - 1]! + t[span.from]!) / 2 + 4;
        const x2 = span.to === LAST ? t[LAST]! + this.lead : (t[span.to]! + t[span.to + 1]!) / 2 - 4;
        const cx = (x1 + x2) / 2;
        const half = this.measure(span.label, 'sub') / 2 + 8;
        const y = rulerY;
        ruler += `<g class="ruler">`
          + `<line x1="${fmt(x1)}" y1="${y - 5}" x2="${fmt(x1)}" y2="${y + 5}"/><line x1="${fmt(x2)}" y1="${y - 5}" x2="${fmt(x2)}" y2="${y + 5}"/>`
          + (x2 - x1 > 2 * half + 8
            ? `<line x1="${fmt(x1)}" y1="${y}" x2="${fmt(cx - half)}" y2="${y}"/><line x1="${fmt(cx + half)}" y1="${y}" x2="${fmt(x2)}" y2="${y}"/>`
            : '')
          + `<text class="rt" x="${fmt(cx)}" y="${y + 4}" text-anchor="middle">${esc(span.label)}</text></g>`;
      }
      for (const loop of this.story.loops) {
        const xa = t[loop.from]!, xb = t[loop.to]!, y = bracketY;
        bracket += `<g class="bracket" data-to="${loop.to}">`
          + `<path d="M${fmt(xb)} ${y - 9} L${fmt(xb)} ${y} L${fmt(xa)} ${y} L${fmt(xa)} ${y - 11}"/>`
          + `<path class="head" d="M${fmt(xa - 3.5)} ${y - 6.5} L${fmt(xa)} ${y - 12} L${fmt(xa + 3.5)} ${y - 6.5}"/>`
          + `<text class="rt" x="${fmt((xa + xb) / 2)}" y="${y + 15}" text-anchor="middle">${esc(loop.label)}</text></g>`;
      }
    }

    const pool = Math.max(...this.beats.map((b) => b.holders.length));
    let batonHtml = '';
    for (let k = 0; k < pool; k++) {
      batonHtml += '<g class="baton" style="display:none"><rect class="cap" y="-12.5" height="25" rx="12.5"/>'
        + '<rect class="cap-hi" y="-11.5" height="23" rx="11.5"/><circle class="led-b" r="5.5"/><circle class="led" r="2.4"/>'
        + '<text class="baton-t" y="3.8"></text></g>';
    }

    svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`);
    svg.setAttribute('width', String(this.W));
    svg.setAttribute('height', String(this.H));
    svg.innerHTML = `<defs>${defs}`
      + `<linearGradient id="${u}-cap" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--cap-top)"/><stop offset="1" style="stop-color:var(--cap-bot)"/></linearGradient>`
      + `<filter id="${u}-blur" filterUnits="userSpaceOnUse" x="0" y="0" width="${this.W}" height="${this.H}"><feGaussianBlur stdDeviation="2.6"/></filter>`
      + `<clipPath id="${u}-reveal"><rect x="0" y="0" width="${this.W}" height="${this.H}"/></clipPath></defs>`
      + ruler
      + `<g class="trk groove">${paths}<g class="in">${paths}</g></g>`
      + `<g class="pins unlit">${pinsUnlit}</g>`
      + walls
      + `<g class="lit" clip-path="url(#${u}-reveal)">`
      + `<g class="trk bloom" filter="url(#${u}-blur)">${litPaths}</g>`
      + `<g class="trk core">${litPaths}</g><g class="trk core-hi">${paths}</g>`
      + `<g class="pins">${pinsLit}</g></g>`
      + `<g class="labels">${labels}</g>${bracket}${batonHtml}`
      + '<g class="spark" style="display:none"><circle class="spark-b" r="5"/><circle class="spark-c" r="1.8"/></g>';

    svg.querySelectorAll<SVGRectElement>('.cap').forEach((r) => r.setAttribute('fill', `url(#${u}-cap)`));
    this.rev = svg.querySelector<SVGRectElement>(`#${u}-reveal rect`);
    svg.querySelectorAll<SVGPathElement>('.trk.groove > path').forEach((p) => {
      const s = this.segs[Number(p.dataset.n)];
      if (s) { s.el = p; s.len = p.getTotalLength(); }
    });
    this.batons = [...svg.querySelectorAll<SVGGElement>('.baton')].map((g) => ({
      g,
      cap: g.querySelector<SVGRectElement>('.cap')!,
      hi: g.querySelector<SVGRectElement>('.cap-hi')!,
      led: g.querySelector<SVGCircleElement>('.led')!,
      ledB: g.querySelector<SVGCircleElement>('.led-b')!,
      text: g.querySelector<SVGTextElement>('.baton-t')!,
    }));
    this.stations = [...svg.querySelectorAll<SVGGElement>('.labels .st')];
    this.walls = [...svg.querySelectorAll<SVGGElement>('.wall')];
    this.spark = svg.querySelector<SVGGElement>('.spark');
  }

  // ---- Painting ----

  private setReveal(m: number): void {
    this.rev?.setAttribute(this.horiz ? 'width' : 'height', String(fmt(Math.max(0, m))));
  }

  private placeBaton(b: Baton, x: number, y: number, w: number, label: string, tone: string, textAlpha: number, alpha: number): void {
    b.g.setAttribute('transform', `translate(${fmt(x)} ${fmt(y)})`);
    b.g.style.setProperty('--c', tone);
    b.cap.setAttribute('x', String(fmt(-w / 2)));
    b.cap.setAttribute('width', String(fmt(w)));
    b.hi.setAttribute('x', String(fmt(-w / 2 + 1)));
    b.hi.setAttribute('width', String(fmt(Math.max(0, w - 2))));
    const ledX = label ? -w / 2 + 12.5 : 0;
    b.led.setAttribute('cx', String(fmt(ledX)));
    b.ledB.setAttribute('cx', String(fmt(ledX)));
    if (b.text.textContent !== label) b.text.textContent = label;
    b.text.setAttribute('x', String(fmt(-w / 2 + 23)));
    b.g.classList.toggle('empty', !label);
    b.text.style.opacity = String(textAlpha);
    b.g.style.opacity = String(alpha);
    b.g.style.display = '';
  }

  /** The picture at rest for beat i (i = -1: nothing has started). */
  settle(i: number): void {
    const t = this.t;
    if (!t.length) return;
    this.setReveal(i < 0 ? t[0]! - this.lead - 8 : t[i]! + 12);
    if (this.spark) this.spark.style.display = 'none';
    const ls = i < 0 ? [] : this.lanes(i);
    this.batons.forEach((b, k) => {
      if (k >= ls.length) { b.g.style.display = 'none'; return; }
      const [x, y] = this.P(t[i]!, ls[k]!);
      const beat = this.beats[i]!;
      this.placeBaton(b, x, y, this.capW[i]!, beat.carry ?? '', toneVar(beat.holders[k]!.tone), 1, 1);
    });
  }

  paintStates(i: number): void {
    const state = (j: number): string => (j < i ? 'past' : j === i ? 'now' : 'next');
    this.stations.forEach((g) => { g.dataset.s = state(Number(g.dataset.i)); });
    this.walls.forEach((g) => { g.dataset.s = state(Number(g.dataset.i)); });
    this.el.svg.querySelectorAll<SVGGElement>('.bracket').forEach((g) => {
      g.dataset.s = Number(g.dataset.to) <= i ? 'past' : 'next';
    });
  }

  cancel(): void {
    if (this.raf) {
      cancelAnimationFrame(this.raf);
      this.raf = 0;
    }
  }

  /** The handoff: capsules glide along their stretch and the lit line follows them. */
  glide(i: number, done?: () => void): void {
    this.cancel();
    this.settle(i - 1);
    const movers = this.segs.filter((s) => s.i === i);
    const beat = this.beats[i]!;
    const label = beat.carry ?? '';
    const w0 = i > 0 ? this.capW[i - 1]! : 26, w1 = this.capW[i]!;
    const keep = i > 0 && (this.beats[i - 1]!.carry ?? '') === label;
    const dur = i === 0 ? FIRST_GLIDE : GLIDE;
    let start = 0;
    const step = (now: number): void => {
      if (!start) start = now;
      const raw = Math.min(1, (now - start) / dur), e = ease(raw), q = Math.min(1, raw / 0.28);
      let front = 0;
      let head: DOMPoint | null = null;
      movers.forEach((s, k) => {
        const b = this.batons[k];
        if (!b || !s.el || s.len === undefined) return;
        const pt = s.el.getPointAtLength(e * s.len);
        const kk = Math.min(k, beat.holders.length - 1);
        this.placeBaton(b, pt.x, pt.y, w0 + (w1 - w0) * q, label, toneVar(beat.holders[kk]!.tone), keep ? 1 : q, i === 0 ? q : 1);
        const m = this.horiz ? pt.x : pt.y;
        if (m >= front) { front = m; head = pt; }
      });
      for (let k = movers.length; k < this.batons.length; k++) this.batons[k]!.g.style.display = 'none';
      this.setReveal(front + 12);
      if (this.spark) {
        const p = head as DOMPoint | null;
        if (p && raw < 1) {
          this.spark.style.display = '';
          this.spark.style.setProperty('--c', toneVar(beat.holders[0]!.tone));
          this.spark.setAttribute('transform', `translate(${fmt(this.horiz ? p.x + 15 : p.x)} ${fmt(this.horiz ? p.y : p.y + 15)})`);
        }
      }
      if (raw < 1) this.raf = requestAnimationFrame(step);
      else {
        this.raf = 0;
        this.settle(i);
        done?.();
      }
    };
    this.raf = requestAnimationFrame(step);
  }

  /** Index of the station under a click, or -1. */
  stationAt(target: EventTarget | null): number {
    const g = (target as Element | null)?.closest?.('.st') as SVGGElement | null;
    return g ? Number(g.dataset.i) : -1;
  }
}

// ---- Playback ----------------------------------------------------------------------

export interface PlaybackState {
  index: number;
  playing: boolean;
}

export class Playback {
  index = 0;
  playing = false;
  private timer = 0;
  static readonly DWELL = 3500;
  static readonly LEAD_IN = 700;

  constructor(
    private readonly r: StageRenderer,
    private readonly reduce: () => boolean,
    private readonly onChange: (s: PlaybackState) => void,
  ) {}

  private get last(): number {
    return this.r.count - 1;
  }

  private clamp(i: number): number {
    return Math.max(0, Math.min(this.last, Math.trunc(i)));
  }

  private emit(): void {
    this.onChange({ index: this.index, playing: this.playing });
  }

  private moveTo(i: number): void {
    this.index = i;
    this.r.paintStates(i);
    if (this.reduce()) this.r.settle(i);
    else this.r.glide(i);
    this.emit();
  }

  private tick = (): void => {
    if (!this.playing) return;
    this.moveTo(this.index + 1);
    if (this.index >= this.last) {
      this.timer = window.setTimeout(() => { this.playing = false; this.emit(); }, GLIDE + 60);
    } else this.timer = window.setTimeout(this.tick, Playback.DWELL);
  };

  start(): void {
    if (this.reduce()) { this.goto(this.index); return; }
    this.playing = true;
    this.moveTo(0);
    this.timer = window.setTimeout(this.tick, Playback.DWELL);
    this.emit();
  }

  play(): void {
    clearTimeout(this.timer);
    this.playing = true;
    if (this.index >= this.last) {
      this.moveTo(0);
      this.timer = window.setTimeout(this.tick, Playback.DWELL);
    } else this.timer = window.setTimeout(this.tick, Playback.LEAD_IN);
    this.emit();
  }

  pause(): void {
    clearTimeout(this.timer);
    this.playing = false;
    this.emit();
  }

  /** Jump straight to the settled picture of beat i. */
  goto(i: number): void {
    this.r.cancel();
    this.index = this.clamp(i);
    this.r.paintStates(this.index);
    this.r.settle(this.index);
    if (this.playing) {
      clearTimeout(this.timer);
      this.timer = window.setTimeout(this.tick, Playback.DWELL);
    }
    this.emit();
  }

  /** A person picked a step: stop autoplay and hand over to it. */
  userGo(i: number): void {
    clearTimeout(this.timer);
    this.playing = false;
    this.moveTo(this.clamp(i));
  }

  /** Redraw after a resize or font change, keeping the current step. */
  rebuild(): void {
    this.r.cancel();
    this.r.build();
    this.r.paintStates(this.index);
    this.r.settle(this.index);
  }

  dispose(): void {
    clearTimeout(this.timer);
    this.r.cancel();
  }
}
