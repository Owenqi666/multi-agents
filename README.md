# Multi-agents

Multi-agents is a local macOS workbench for coordinating Codex and Claude Code through their existing CLI logins. Start with a project goal, let the models propose a plan, and decide how to split the work. When a change is ready, the workbench shows the Git diff, test results, and independent reviews for the exact commit you are accepting.

The first release is complete. [Accepted code](https://github.com/Owenqi666/multi-agents/commit/12c10e995009bef5ea6a28ddc5705664d06b89f2) · [Acceptance report](docs/acceptance/first-release.md)

## How a project moves through the workbench

1. **Confirm the goal.** Ask the models for input on a requirement. Once you confirm the goal, the planner breaks it into stages and writes a brief for each one.
2. **Choose who implements it.** One model can complete a whole stage, or you can delegate smaller tasks with clear inputs and outputs. Independent tasks can run in parallel Git worktrees. Repository rules can assign paths to a particular provider. You can select a model and reasoning level for each role.
3. **Review a fixed commit.** The workbench runs the full set of tests registered for the candidate. Choose one reviewer or ask two models to review the same frozen snapshot independently. When both reports arrive, you can read the combined findings and follow each comment to the line it refers to.
4. **Rework and accept.** A verified blocker that belongs to one task can trigger one automatic rework cycle. The revised commit goes through the original tests and reviewers again. You decide when to accept it. If the target branch moves, the candidate must be validated against the new baseline.

You can also start a standalone review from two commits or a pasted patch. For each attempt, the workbench distinguishes the settings you requested, the model it assigned, and the model, reasoning level, and usage the runtime actually reported. Missing runtime evidence is marked as unconfirmed. [Web interface guide](frontend/README.md)

## Quick start

You need Git, Python 3.12 or later, uv, and Node.js with Corepack. Node.js 24.15 or later in the 24.x series is recommended. The frontend pins pnpm 10.34.9. To run live model tasks, install and sign in to each provider CLI you intend to use.

```sh
git clone https://github.com/Owenqi666/multi-agents.git
cd multi-agents
uv sync --frozen
(cd frontend && corepack pnpm install --frozen-lockfile && corepack pnpm build)
```

From the workbench source directory, set `REPO` to the Git repository you want to work on. It should contain at least one commit. Give that project its own private data directory outside the repository and all its worktrees. The service creates the directory if needed.

```sh
REPO="/absolute/path/to/your-project"
DATA="$HOME/.local/share/multi-agents/my-project"
uv run python -m multi_agent.web --repository "$REPO" --data-dir "$DATA" --port 0
```

Open the `http://127.0.0.1:<port>` address printed in the terminal. The same service hosts the web interface and API. Before planning and implementing work in your own project, commit its role assignments, write rules, and named test commands to `.multi-agent/rules.yaml` on the target baseline. [Rule format and example](docs/task-rules.md) · [Service options and read-only archives](docs/project-service.md)

### Try it offline

After installing the frontend dependencies, run the following from the workbench source directory and open the printed address. The default recording follows a project through acceptance. Append `?mock=rework` to see a change go through rework. Both replay saved responses without calling a model. [Recorded scenarios](docs/web-api/samples/README.md)

```sh
(cd frontend && corepack pnpm mock)
```

## Documentation

The workbench runs locally for one user, with one Git repository per service instance. The web interface uses React, TypeScript, and Vite. The local service and CLI use Python. SQLite stores project state and the index of review evidence. The detailed guides below are in Chinese.

| Task | Guide |
| --- | --- |
| Turn a requirement into a stage plan and implementation tasks | [Project workflow](docs/project-workflow.md) |
| Run tasks in parallel, integrate dependencies, and handle rework | [Integration workflow and CLI examples](docs/phase-three-development.md) |
| Freeze a diff and request an independent review from the terminal | [Review CLI](docs/review-cli.md) |
| Read job results or open an archive through the local API | [HTTP service](docs/project-service.md) · [Workspace API](docs/workspace-api.md) |
| Develop and test the workbench | [Contributor instructions](AGENTS.md) · [Frontend checks](frontend/README.md#%E6%A3%80%E6%9F%A5) · [Project plan](PROJECT_PLAN.md) |
