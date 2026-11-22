import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// The dev server forwards /api to the bridge named by BRIDGE_URL (for example
// http://127.0.0.1:8765). The bridge only accepts Host 127.0.0.1:<port> and an Origin equal
// to its own address, so both headers are rewritten on the way through.
const bridge = process.env.BRIDGE_URL;

// Recorded bridge samples drive mock mode and the component tests. They live in the repo at
// docs/web-api/samples (written by the offline HTTP recorder); SAMPLES_DIR can point elsewhere.
const samples = resolve(import.meta.dirname, process.env.SAMPLES_DIR ?? '../docs/web-api/samples');

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@samples': samples } },
  server: {
    fs: { allow: [import.meta.dirname, samples] },
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    proxy: bridge
      ? {
          '/api': {
            target: bridge,
            changeOrigin: true,
            configure: (proxy) => {
              proxy.on('proxyReq', (req) => {
                req.setHeader('origin', new URL(bridge).origin);
              });
            },
          },
        }
      : undefined,
  },
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, 'index.html'),
        specimen: resolve(import.meta.dirname, 'specimen.html'),
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    exclude: ['e2e/**', 'node_modules/**'],
  },
});
