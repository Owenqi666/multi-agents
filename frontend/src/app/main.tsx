import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../fonts';
import '../styles/tokens.css';
import '../styles/ui.css';
import './app.css';
import { setTransport } from '../api/client';
import { App } from './App';

/** Mock mode needs no bridge: `pnpm mock`, or ?mock=<scenario> on the dev server. It only
 *  replays recorded samples and a fixed job timeline; it implements no business rules. */
async function start(): Promise<void> {
  if (import.meta.env.DEV) {
    const params = new URLSearchParams(window.location.search);
    const scenario = params.get('mock') ?? (import.meta.env.MODE === 'mock' ? 'accept' : null);
    if (scenario) {
      const { MockTransport } = await import('../mock/transport');
      setTransport(new MockTransport(scenario, Number(params.get('at') ?? Infinity)));
    }
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
