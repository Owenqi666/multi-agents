// jsdom has no SVG geometry, ResizeObserver or matchMedia; the stage drawings need them.
import { afterEach, beforeEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import { jobs } from '../api/jobs';
import { clearReadCache } from '../api/reads';
import { http } from '../api/transport';
import { setTransport } from '../api/client';
import { setLangForTest } from '../i18n';

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

// Tests read the Chinese text unless they switch on purpose (jsdom reports an English browser).
setLangForTest('zh');
beforeEach(() => setLangForTest('zh'));

afterEach(() => {
  cleanup();
  jobs.reset();
  clearReadCache();
  setTransport(http);
});
