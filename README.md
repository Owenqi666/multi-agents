# Multi-agents

Multi-agents is a local macOS workbench for building and reviewing software with Codex and Claude Code. Each reviewer sees a frozen copy of the change, its requirements, and any available test evidence. You can trace a finding back to the code the reviewer saw.

You can plan a multi-stage project, start with a single task, or review existing work by choosing two Git commits or pasting a patch. Each instance serves one user and one repository. The interface is available in English and Chinese.

[Try the offline demo](#explore-the-recorded-demo) · [Explore the architecture](#how-a-browser-action-reaches-a-model) · [Run on your own repository](#run-against-your-own-repository)

## Follow a change from request to acceptance

In the recorded demo, a report should display `ready` once its backend value is available. The candidate still displays `draft`. Its tests pass because they accept either value.

1. **Plan and implement.** Repository rules assign the backend to Codex and the frontend to Claude Code. The planner schedules the frontend after the backend. Independent tasks can run in separate Git worktrees. The home inbox links to stages, tasks, and reviews that need your attention.
2. **Test and review.** One or two independent reviewers inspect the tested candidate. In the demo, a reviewer flags `draft` and links to `frontend/status.js:1`. You can record your judgment, ask that reviewer a question about the frozen snapshot, or start a repair task linked to the finding.
3. **Repair and deliver.** A verified blocker traced to one task can trigger one repair cycle, followed by a full retest and a fresh review. When you accept the revised candidate, you get a receipt and can copy a Chinese Markdown summary with the commit, tests, and review. Merging is a separate Git operation. [Browser regression](frontend/e2e/rework.spec.ts)

<a href="docs/images/workbench.png"><img src="docs/images/workbench.png" alt="Two reviewers link findings to lines in the same frozen patch" width="640"></a>

*Two independent reviews of a frozen patch, captured in Chromium with scripted model responses.*

## How a browser action reaches a model

| Layer | Design |
| --- | --- |
| React and TypeScript | Implementation and reviewer follow-ups use `POST /api/jobs`. The service returns HTTP 202 and the browser polls by job ID. A human decision on a finding goes to `POST /api/reviews/{id}/decisions`, which returns HTTP 201 with the saved decision. [Typed client](frontend/src/api/client.ts) |
| Python service and CLI | One loopback service serves the UI and API. It translates jobs into Python CLI invocations, owns the subprocess, and records its exit status. Codex communicates through its local App Server over JSON-RPC. Claude Code streams JSON from a headless subprocess. [Job controller](src/multi_agent/web/jobs.py) · [Provider adapters](src/multi_agent/adapters/runtime.py) |
| Git and SQLite | The workbench checks each candidate diff against the task's allowed paths before integration. SQLite records review attempts transactionally, so a late reply stays attached to its original attempt. At acceptance the service rechecks the candidate and target refs. [Candidate checks](src/multi_agent/cli/task_candidates.py) · [State transitions](src/multi_agent/core/transitions.py) · [Acceptance checks](src/multi_agent/cli/integration_verification.py) |

The job page shows each reviewer's progress. After an interruption, you can retry just the missing reviewers on the same snapshot once recovery is available. An eligible task test can run again on the same candidate under the original conditions. Earlier reviews stay attached to the test run they examined. You can reopen historical runs in a read-only archive. [Archive guide](docs/workbench.md#open-a-read-only-archive)

## Quick start

### Explore the recorded demo

Use Node.js 24.15 or later in the 24.x series with Corepack. The frontend pins pnpm 10.34.9.

```sh
git clone https://github.com/Owenqi666/multi-agents.git
cd multi-agents
(cd frontend && corepack pnpm install --frozen-lockfile && corepack pnpm mock)
```

Open the URL printed by Vite. The default scenario ends in acceptance. Append `?mock=rework` to inspect the missed requirement and its repair. Form submissions replay recorded outcomes. The demo runs entirely in the browser without a Python service or provider login. [All four scenarios](docs/web-api/samples/README.md)

### Run against your own repository

Install Git, Python 3.12 or later, uv, and the Node.js toolchain above. Single-task planning requires both the `codex` and `claude` executables. Sign in to the providers you use. The project test runner requires **Codex CLI 0.169.0** even when Claude implements the change. The Claude implementation adapter requires **Claude Code 2.1.325**.

From the workbench source checkout, point the service at a Git repository with at least one commit and a private data directory outside that repository and its worktrees.

```sh
uv sync --frozen
(cd frontend && corepack pnpm install --frozen-lockfile && corepack pnpm build)

REPO="/absolute/path/to/your-project"
DATA="$HOME/.local/share/multi-agents/my-project"
uv run python -m multi_agent.web --repository "$REPO" --data-dir "$DATA" --port 0
```

Open the printed URL. On **New goal**, check provider login status, inspect the rules and registered test commands for your chosen baseline, or copy a rules template. Commit your rules to `.multi-agent/rules.yaml` on that baseline. The setup check lists the commands. The first test run checks whether they execute in the test environment. [Project setup guide](docs/workbench.md#set-the-project-rules)

## Verification

```sh
scripts/verify.sh
(cd frontend && corepack pnpm run check)
(cd frontend && corepack pnpm exec playwright install chromium && corepack pnpm run e2e)
```

The Python checks cover linting, formatting, types, unit tests, and event replay. The frontend checks cover types, linting, and component tests. Chromium exercises the local service, CLI, Git, and SQLite with scripted providers, including recovery after a hard stop and reopening an accepted change. [Python checks](scripts/verify.sh) · [Workbench browser tests](frontend/e2e/workbench.spec.ts)
