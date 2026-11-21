import { expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { setTransport } from '../api/client';
import type { Query } from '../api/transport';
import { App } from '../app/App';
import { fakeTransport } from './fake';

it('decodes an archived transcript across byte pages and keeps the full digest on the last page', async () => {
  const digest = 'a'.repeat(64);
  const common = { byte_count: 5, sha256: digest, stream: 'stdout' };
  const pages = vi.fn((query: Query | undefined) => query?.offset === 0
    ? { ...common, offset: 0, next_offset: 2, text: 'a\uFFFD', raw_base64: 'YeQ=' }
    : { ...common, offset: 2, next_offset: null, text: '\uFFFD\uFFFDb', raw_base64: 'uK1i' });
  setTransport(fakeTransport({ '/jobs': { items: [] }, '/artifacts/transcript': pages }));
  window.history.replaceState(null, '', '/logs/transcript?title=模型会话日志');
  const view = render(<App />);
  try {
    const more = await screen.findByRole('button', { name: '读下一页' });
    expect(view.container.querySelector('.log-body')?.textContent).toBe('a');
    fireEvent.click(more);
    await screen.findByText('a中b');
    expect(pages).toHaveBeenCalledWith({ offset: 2, limit: 8192 });
    expect(screen.getByText(`sha256 ${digest}`)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '读下一页' })).toBeNull();
  } finally {
    window.history.replaceState(null, '', '/');
  }
});
