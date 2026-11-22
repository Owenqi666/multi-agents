# Multi-agents · 本地多模型开发工作台

在 Mac 上用一个网页协调已登录的 Codex 和 Claude Code。从一个项目目标出发，你可以让两个模型先给意见，确认目标后再制定阶段计划。每次实施都有独立的 Git 工作树，交付时可以回到同一个候选提交，查看改了什么、跑过哪些测试，以及各个审核席位的意见。

**首版已完成。** 最终产品提交为 [`4712251`](https://github.com/Owenqi666/multi-agents/commit/47122515fc0c6ea61982832b065788b326b60d48)。[首版验收回执](docs/acceptance/first-release.md)

## 一次开发怎样进行

1. **确认目标。** 从首页发起需求咨询，读完意见后由你确认目标。规划模型据此拆出阶段，给出阶段任务书。
2. **选择实施方式。** 每个阶段可由一名主执行者直接完成，也可以进一步拆成输入、输出明确的子卡。独立任务可在不同 worktree 并行；仓库规则可指定哪些路径由哪个供应商实施。角色的模型与推理档位可配置。
3. **查看候选与证据。** 工作台冻结候选提交，执行仓库登记的完整测试。可以选一个模型审核，也可以让两个模型分别审核同一冻结快照，再汇总意见。网页把意见定位到当时的差异，并保留各次尝试的测试和会话记录。
4. **返工或接受。** 符合任务范围且归属明确的阻塞问题可以自动返工一次。新候选重新跑原完整测试并复审，最后由你显式接受。目标分支前移时需要重新验证。

已有两个提交或一段补丁时，也可以直接进入「审核」，单独获取意见。运行记录区分请求的模型、工作台的派发选择与供应商实际报告的模型、推理档位和用量；缺失的运行时证据显示为「未确认」。[网页操作说明](frontend/README.md)

## 快速启动

准备 Git、Python 3.12 或更高版本、uv，以及带 Corepack 的 Node.js（建议 24.15 或更高的 24.x）。前端锁定 pnpm 10.34.9。运行真实模型任务前，请在本机安装并登录所选的 Codex CLI 或 Claude Code。

```sh
git clone https://github.com/Owenqi666/multi-agents.git
cd multi-agents
uv sync --frozen
(cd frontend && corepack pnpm install --frozen-lockfile && corepack pnpm build)
```

在工作台源码根目录启动服务。将 `REPO` 换成你要开发的 Git 仓库路径，该仓库应至少有一次提交。为这个项目选择独立的私有数据目录，放在目标仓库及其所有 worktree 之外，服务会创建缺失的目录。

```sh
REPO="/absolute/path/to/your-project"
DATA="$HOME/.local/share/multi-agents/my-project"
uv run python -m multi_agent.web --repository "$REPO" --data-dir "$DATA" --port 0
```

在浏览器打开终端打印的 `http://127.0.0.1:<端口>`。页面和 API 由同一服务提供。在真实项目中执行规划和实施前，将适用的角色、写入规则和命名测试提交到目标基线的 `.multi-agent/rules.yaml`。[规则格式与示例](docs/task-rules.md) · [服务参数与只读归档](docs/project-service.md)

**先离线体验。** 安装前端依赖后，在源码根目录执行下面的命令，用浏览器打开终端打印的地址。默认样例从规划走到接受，在地址后加 `?mock=rework` 可查看一次返工。这些页面回放仓内样例，模型调用次数为零。[四个录制场景](docs/web-api/samples/README.md)

```sh
(cd frontend && corepack pnpm mock)
```

## 进一步使用

| 想做什么 | 入口 |
| --- | --- |
| 从需求咨询走到阶段规划与实施 | [项目流程](docs/project-workflow.md) |
| 配置并行实施、依赖集成和一次返工 | [阶段 3 工作流与 CLI 示例](docs/phase-three-development.md) |
| 通过终端冻结差异并发起独立审核 | [审核 CLI](docs/review-cli.md) |
| 接入本机 HTTP、读取作业或归档 | [服务接口](docs/project-service.md) · [工作区 API](docs/workspace-api.md) |
| 开发或验证工作台本身 | [仓库约定](AGENTS.md) · [前端检查](frontend/README.md#检查) · [项目书](PROJECT_PLAN.md) |

本版本面向本机单用户、每个服务绑定一个 Git 仓库。网页使用 React、TypeScript 和 Vite，本地服务与 CLI 使用 Python，项目状态和审核证据索引保存在 SQLite。
