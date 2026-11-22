# Running the workbench on a project

Start in the workbench source checkout. The [root README](../README.md#quick-start) covers installation. The [offline demo](../frontend/README.md) runs without provider credentials. For live runs, the Codex isolation profile is pinned to Codex CLI 0.169.0, and the Claude implementation adapter checks Claude Code 2.1.325. Use the local login for each provider you select. The checked-in [protocol snapshots](../protocol/) correspond to these versions.

## Set the project rules

In the **target project**, create `.multi-agent/rules.yaml`. This example is for a Python project whose tests use `unittest`. It sends changes under `frontend/` to Claude and uses Codex as the default for other paths.

```yaml
version: 1
commands:
  unit:
    executable: python3
    args: ["-m", "unittest", "discover", "-s", "tests"]
protected_paths: [".multi-agent/**", "tests/**"]
implementation:
  - paths: ["frontend/**"]
    provider: claude-code
    default_model: claude-sonnet-5-5
    default_effort: medium
default_implementation:
  provider: codex
  default_model: gpt-6.2-sol
  default_effort: medium
```

Use commands that work in a fresh checkout of your project, and select models available to your account. A task refers to each command by its registered name, such as `unit`. Arguments are passed separately to the executable. Paths under `protected_paths` are outside the implementation scope. Here that includes the test suite and the rules themselves.

Commit the file on the branch you will use as the project baseline. Planning and dispatch read that commit's Git object, so tasks keep the same rules when retried. [Rules reader](../src/multi_agent/cli/task_inputs.py)

## Start the service

From the workbench source checkout, point the service at the **target repository** and a private data directory outside that repository and its worktrees.

```sh
REPO="/absolute/path/to/your-project"
DATA="$HOME/.local/share/multi-agents/my-project"
uv run python -m multi_agent.web --repository "$REPO" --data-dir "$DATA" --port 0
```

Open the loopback URL printed by the process. The service serves the built frontend and API from the same origin. Choose a goal and baseline in the browser, then a direct or delegated implementation stage. If a test needs an executable outside the default search directories, use `--tool-directory` at startup. Additional system runtime files can be made available with `--runtime-read-path`. Both options can be repeated.

## Review two existing commits

A standalone review can also start from the terminal. This example reviews the latest commit against its parent. It requires a target repository with at least two commits and an existing requirements text file. Run these commands from the workbench source directory with the same `REPO` and `DATA` values as above.

```sh
BASELINE=$(git -C "$REPO" rev-parse HEAD^)
CANDIDATE=$(git -C "$REPO" rev-parse HEAD)
uv run python -m multi_agent --data-dir "$DATA" freeze \
  --repo "$REPO" --baseline "$BASELINE" --candidate "$CANDIDATE" \
  --requirements /absolute/path/to/requirements.txt \
  --acceptance "The total equals the sum of all input amounts."
```

Use the returned `snapshot_id` in the next commands. The snapshot contains the committed diff and requirements. To attach an existing test run, add `--tests evidence.json` to `freeze`. Each result identifies the candidate commit, command argument array, exit code and log text. Standalone freezing imports this evidence. The project workflow has a separate command runner that executes the registered tests on a fixed candidate.

```sh
SNAPSHOT_ID="paste-the-snapshot-id"
uv run python -m multi_agent --data-dir "$DATA" snapshot "$SNAPSHOT_ID"
uv run python -m multi_agent --data-dir "$DATA" run "$SNAPSHOT_ID" \
  --seat codex gpt-6.2-sol high --seat claude claude-opus-5-5 high
```

The final command starts two live reviewer sessions. Supply just one `--seat` for a single review. Replace the sample models and effort levels as needed. Use the returned `round_id` with `show` to read the assessment, attempts and available evidence. With `retry`, the same snapshot and seats are reused for failed or unstarted attempts.

```sh
ROUND_ID="paste-the-round-id"
uv run python -m multi_agent --data-dir "$DATA" show "$ROUND_ID" --history
```

## Inspect a run through HTTP

Use the printed service URL as the origin. A job ID identifies one command invocation. The IDs listed in the job's `resources` lead to durable project, review or task records.

| Request | Use |
| --- | --- |
| `GET /api/workspace` | Read the repository, branch, HEAD, model choices and available rules. |
| `POST /api/jobs` | Submit an action with a JSON body containing `action` and `args`. HTTP 202 returns a job ID. |
| `GET /api/jobs/{job_id}` | Poll the state and inspect the exit code, result, error and linked resources. |
| `GET /api/resources/{kind}/{id}` | Reopen a domain record and locate available artifacts. |
| `GET /api/diffs/snapshot/{snapshot_id}` | Read the frozen patch used by a particular review. |
| `GET /api/artifacts/{artifact_id}?offset=0&limit=4096` | Read a page of a registered log. The response includes the next byte offset and the SHA-256 of the complete artifact. |

For example, `curl "$ORIGIN/api/workspace"` returns the same workspace data the browser receives when `ORIGIN` is set to the printed URL. [Recorded HTTP requests and responses](web-api/samples/README.md)

## Open a read-only archive

Start a separate service with the original target repository, a sealed database, and a fresh private data directory. Keep any associated WAL alongside the database. Open the printed URL and choose the record type and ID in the archive view. Log content is available when the associated evidence files are accessible.

```sh
uv run python -m multi_agent.web --repository "$REPO" \
  --data-dir "$HOME/.local/share/multi-agents/archive-view" \
  --read-only-database /absolute/path/to/reviews.sqlite3 --port 0
```
