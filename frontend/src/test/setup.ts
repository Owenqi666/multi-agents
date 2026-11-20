// jsdom has no SVG geometry, ResizeObserver or matchMedia; the stage drawings need them.
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import { jobs } from '../api/jobs';
import { clearReadCache } from '../api/reads';
import { http } from '../api/transport';
import { setTransport } from '../api/client';

const proto = (globalThis as unknown as { SVGElement: { prototype: Record<string, unknown> } }).SVGElement.prototype;
proto.getComputedTextLength = () => 48;
proto.getTotalLength = () => 100;
proto.getPointAtLength = () => ({ x: 0, y: 0 });

class RO {
  observe(): void {}
  disconnect(): void {}
  unobserve(): void {}
}
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO;
window.matchMedia ??= ((q: string) => ({ matches: q.includes('reduce'), media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false })) as typeof window.matchMedia;
Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 1240 });

afterEach(() => {
  cleanup();
  jobs.reset();
  clearReadCache();
  setTransport(http);
});
