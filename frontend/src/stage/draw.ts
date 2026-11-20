// Shared pieces for the stage drawings (relay line and weave).

import { familyOfTone } from './models';
import type { Holder } from './story';

export const fmt = (n: number): number => Math.round(n * 10) / 10;
export const esc = (s: string): string =>
  s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] ?? c);
export const ease = (x: number): number => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

/** Plain geometric marks: circle = Claude, diamond = GPT, small ring = you,
 *  square = workbench. No company marks. */
export function glyph(holder: Pick<Holder, 'tone'>, r: number): string {
  const family = familyOfTone(holder.tone);
  if (family === 'gpt') {
    const s = fmt(r * 0.9);
    return `<rect x="${-s}" y="${-s}" width="${2 * s}" height="${2 * s}" transform="rotate(45)"/>`;
  }
  if (family === 'system') {
    const s = fmt(r * 0.86);
    return `<rect x="${-s}" y="${-s}" width="${2 * s}" height="${2 * s}"/>`;
  }
  if (family === 'human') return `<circle class="hollow" r="${fmt(r * 0.92)}"/>`;
  return `<circle r="${r}"/>`;
}

export interface StageElements {
  /** The element whose width decides the layout of the drawing. */
  frame: HTMLElement;
  /** The plate that holds the svg. */
  box: HTMLElement;
  svg: SVGSVGElement;
  /** Hidden text nodes used to measure label widths with the real fonts. */
  meter: { baton: SVGTextElement; sub: SVGTextElement };
}

/** What playback needs from a stage drawing. */
export interface StageRenderer {
  readonly count: number;
  lastFrameWidth: number;
  build(): void;
  /** The picture at rest for step i (-1: nothing has started). */
  settle(i: number): void;
  paintStates(i: number): void;
  /** Animate the handoff into step i, then settle there. */
  glide(i: number, done?: () => void): void;
  cancel(): void;
  /** Changes once the web fonts arrive and labels measure differently. */
  signature(): string;
  clearMeasures(): void;
}

/** Width of text in one of the meter styles, cached. */
export class Meter {
  private cache = new Map<string, number>();
  constructor(private readonly nodes: StageElements['meter']) {}
  width(text: string, kind: 'baton' | 'sub'): number {
    const key = `${kind}|${text}`;
    let w = this.cache.get(key);
    if (w === undefined) {
      const m = this.nodes[kind];
      m.textContent = text;
      w = m.getComputedTextLength();
      this.cache.set(key, w);
    }
    return w;
  }
  clear(): void {
    this.cache.clear();
  }
}
