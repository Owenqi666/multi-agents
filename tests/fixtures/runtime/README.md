# 两边 CLI 的实机录制

2026-11-15 在本机用探测工具（`src/multi_agent/adapters/probe/`）录下的真实输出，一个步骤一个文件。适配器的测试拿它们当输入，runtime-protocols skill 的事实表拿它们当证据。

## 文件

| 目录 | 内容 |
| --- | --- |
| `codex-0.169.0/` | Codex app-server 的七个步骤：`codex-env`、`codex-sandbox`、`codex-turn`、`codex-structured`、`codex-readonly`、`codex-cancel`、`codex-bad-model` |
| `claude-2.1.325/` | `claude -p` 的六个步骤：`claude-env`、`claude-turn`、`claude-structured`、`claude-readonly`、`claude-cancel`、`claude-bad-model`，另有 `claude-auth-expired` |

每个目录里有一个 `manifest.json`，记着每个文件来自哪次运行、那次运行的参数和当时探测工具的提交。

每行是一条 JSON 记录，带 `seq`（从 1 递增）、`t`（从步骤开始算的秒数）和 `kind`。`kind` 是 `spawn`、`send`、`recv`、`stdin_closed`、`signal`、`exit`、`observation`、`timeout` 之一。记录格式的完整说明在探测工具的模块文档里。

| 步骤 | 录的是什么 |
| --- | --- |
| `codex-env`、`claude-env` | 版本和登录状态，Codex 另有模型目录和限流状态。不发模型请求 |
| `codex-sandbox` | 不经过模型，用 `command/exec` 在只读和可写两种沙箱策略下运行命令 |
| `codex-turn`、`claude-turn` | 一个最短的回合 |
| `codex-structured`、`claude-structured` | 带输出 schema 的回合 |
| `codex-readonly`、`claude-readonly` | 让模型读一个文件，再试着写一个文件。工作目录里放了带暗号的 `AGENTS.md` 和 `CLAUDE.md` |
| `codex-cancel`、`claude-cancel` | 回合进行到一半时取消 |
| `codex-bad-model`、`claude-bad-model` | 请求一个不存在的模型 |
| `claude-auth-expired` | 登录过期时的一次 `claude-turn` |

## 来源

| 运行 | 时间（UTC） | 探测工具的提交 | 文件 |
| --- | --- | --- | --- |
| `20261115T171610Z` | 2026-11-15 17:16 | `a097fc3` | `codex-env`、`codex-sandbox`、`claude-env` |
| `20261115T171614Z` | 2026-11-15 17:16 | `a097fc3` | 其余十个步骤 |
| `20261115T010545Z` | 2026-11-15 01:05 | `3672ef8` | `claude-auth-expired`。那次运行是手工去掉 `ANTHROPIC_API_KEY` 之后跑的，所以没有 `env_removed` 记录 |

原始录制在用户工作目录的 `probe-runs/` 里，不进 git。

## 脱敏

探测工具录制时已经把家目录换成 `~`，把步骤的工作目录换成 `<WORKDIR>`，把邮箱、账号和组织的标识、套餐、主机名、安装标识和余额换成 `<REDACTED>`。

入库时相对原始录制只改了一处：`codex-env.jsonl` 里 `account/rateLimits/read` 的响应，`rateLimitResetCredits.credits[].id` 换成了 `<REDACTED>`。它和账号绑定，探测工具现有的规则遮不到。其余文件和原始录制逐字节相同。

保留下来的有：线程、回合、条目、会话和请求的各种 id，用量百分比和重置时间，登录到期时间，用户配置的 MCP 服务器名字，Claude 的技能和插件清单，操作系统和终端的版本。

这些文件不手工修改。

## CLI 升级之后重新录制

探测会启动真实的 CLI、消耗额度，要先得到用户同意，由用户在自己的终端里运行：

```bash
uv run python -m multi_agent.adapters.probe codex-env codex-sandbox claude-env
uv run python -m multi_agent.adapters.probe codex-turn codex-structured codex-readonly codex-cancel codex-bad-model claude-turn claude-structured claude-readonly claude-cancel claude-bad-model
```

入库之前：

1. 确认每个步骤的结果都是 `finished`。
2. 检查脱敏：录制里没有邮箱、家目录、主机名和账号标识。
3. 放进新的 `<cli>-<版本>/` 目录，写 `manifest.json`。旧版本的目录保留。
4. 按新录制核对 runtime-protocols 的事实表。

## 下次重跑探测之前要处理的事

这几项是 T-0008 的审核和这次入库留下的，都不影响现有的夹具。

1. 重置额度 id 的脱敏规则还没有写进探测工具，这次是入库时处理的。
2. 文本脱敏在转义引号密集的长字符串上耗时是平方级的：16 kB 约 2.6 秒，32 kB 约 10 秒，期间步骤超时不会触发。现有录制里没有这样的字符串。修法是让文本规则的正则先跳过字符串之外的转义对。
3. 入库是一次性脚本做的，仓库里没有导出命令。
4. 探测工具有几处行为没有测试盯住，实现本身是对的：`--version` 和 `auth status` 的输出流被占住时不计入步骤超时；`claude-cancel` 和 Codex 的回合步骤在输出流被占住时按进程退出判定；`codex-cancel` 在 `thread/start` 或 `turn/start` 报错时恰好记一条 `cancel_skipped`；`codex-sandbox` 等到上一个响应再发下一个请求；`env_removed` 是 Claude 步骤的第一条记录；精确匹配的键名不按后缀匹配。
5. `thread/items/list` 在 0.169.0 返回 `not supported yet`。探测工具在每个回合之后仍然请求它，升级后重跑就能看到它是否可用。
