# Web interface

The React and TypeScript frontend shows the handoff between models, task worktrees, test results and review findings. The interface is in Chinese. The same build works with the local Python service and with the offline integration environment. [Project guide](../docs/workbench.md)

## Run the recorded demo

Use Node.js 24.15 or later in the 24.x series and Corepack. From the repository root, install the pinned frontend dependencies and start Vite.

```sh
cd frontend
corepack pnpm install --frozen-lockfile
corepack pnpm mock
```

Open the URL printed in the terminal. The default `accept` scenario follows a project to an acceptance receipt. Append `?mock=rework` to inspect a blocking finding and the revised candidate, `?mock=dual-partial` to see a failed reviewer retry, or `?mock=cancel` for cancellation and interruption. The page reads saved HTTP responses and simulates job polling. It requires no model login or running Python service. [Sample format and recording](../docs/web-api/samples/README.md)

## Connect to a running service

Build the frontend from `frontend/` with `corepack pnpm build`. This writes `frontend/dist`, which the Python service serves by default. For a development server with hot reload, start the Python service, set `BRIDGE_URL` to its printed origin, then run the following from `frontend/`.

```sh
BRIDGE_URL=http://127.0.0.1:8765 corepack pnpm dev
```

The example port should match the one printed by your backend. Vite proxies `/api` to that origin. To explore the animated handoff stage by itself, run `corepack pnpm specimen`.

## Check the interface

Run these commands in `frontend/`. The browser suite also uses the Python dependencies installed by `uv sync --frozen` at the repository root.

```sh
corepack pnpm run check
corepack pnpm exec playwright install chromium
corepack pnpm run e2e
```

The first command checks TypeScript, ESLint and the component tests. The last builds the production page and runs the Playwright suite. Workflow scenarios exercise the actual loopback service, Git worktrees and SQLite database with substitute model CLIs. The browser race tests control response timing to reproduce delayed requests. [Browser scenarios](e2e/)
