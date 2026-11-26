# Web interface

The React and TypeScript frontend shows the handoff between models, task worktrees, test results and review findings. The interface is available in Chinese and English; the switch in the top corner keeps the choice in the browser, and the first visit follows the browser language. The same build works with the local Python service and with the offline integration environment. [Project guide](../docs/workbench.md)

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

## Interface text

The Chinese text in the code is the key: write it inside `tx()`, for example `tx('{0} 条阻塞意见', count)`, and add the English to `src/i18n/en.ts`. Word tables defined at module level go through `translated()`, so they follow the current language when read. Use `joinList()` and `pair()` for lists and labels, so English gets commas and colons instead of 、 and ：. The component tests fail when a Chinese string has no English entry, or when an English page still shows Chinese text.

## Check the interface

Run these commands in `frontend/`. The browser suite also uses the Python dependencies installed by `uv sync --frozen` at the repository root.

```sh
corepack pnpm run check
corepack pnpm exec playwright install chromium
corepack pnpm run e2e
```

The first command checks TypeScript, ESLint and the component tests. The last builds the production page and runs the Playwright suite. Workflow scenarios exercise the actual loopback service, Git worktrees and SQLite database with substitute model CLIs. The browser race tests control response timing to reproduce delayed requests. [Browser scenarios](e2e/)

`e2e/workbench.spec.ts` walks the inbox on the home page, job activity and recovery after a hard stop, finding discussions, the setup check on the new-goal page, and the delivery summary. Each new panel is also checked in the dark theme and at 1024 and 768 pixels, and the same records are opened through a read-only archive and through the recorded demo. Screenshots are saved under `e2e/records/workbench-shots/`, and the measured text contrast in the run records next to them. To run it alone after a build: `corepack pnpm exec playwright test e2e/workbench.spec.ts`.
