# Codex App Server 与 Codex CLI

状态的含义见 `SKILL.md`。本表按 2026-11-14 生成的快照核对过（`protocol/codex/`，Codex CLI 0.169.0）。标 `快照` 的条目在 schema 或帮助输出里能找到，名字和结构可信，行为没有验证。与快照不一致时以快照为准，并更新本表。

## 官方文档

- App Server：https://learn.chatgpt.com/docs/app-server （模型目录 `#models`，回合 `#turns`，沙箱 `#sandbox-read-access-readonlyaccess`，取消 `#interrupt-a-turn`，事件 `#events`）
- 模型与版本要求：https://learn.chatgpt.com/docs/models#choose-a-model
- 模型选择：https://learn.chatgpt.com/docs/model-selection
- Skills：https://learn.chatgpt.com/docs/build-skills

## 本机记录

| 事实 | 状态 | 证据 |
| --- | --- | --- |
| Codex CLI 为 0.169.0，由官方安装脚本从 0.145.0 升级而来 | `已验证` 2026-11-13、2026-11-14 | 计划「本机接入待验证项」；`protocol/codex/VERSION` |
| `codex app-server generate-json-schema --out DIR` 能导出 schema | `已验证` 2026-11-14，0.169.0 | `protocol/codex/schema/`，329 个文件 |
| `codex debug models` 列出 `gpt-6-astra`、`gpt-6-luna`、`gpt-6-sol`、`gpt-6.1-sol`、`gpt-6.1-terra` | `已验证` 2026-11-13，0.169.0 | 计划 |
| `codex exec`（只读沙箱）短任务分别请求 `gpt-6.1-sol` `high`、`gpt-6.1-terra` `medium`、`gpt-6-astra` `high`、`gpt-6-luna` `medium`，都收到回包 | `已验证` 2026-11-13，0.169.0，仅连通性 | 计划 |
| 上述短任务实际使用的模型和生效的档位 | `未确认` | 计划注明以可观察证据为准，当时没有记录 |
| 0.145.0 的模型目录里没有 GPT‑6.x。调用 Sol 和 Terra 时报账号不支持该模型，调用 Astra 时报需要更新版本的 Codex | `已验证` 2026-11-13，0.145.0 | 计划。错误原文没有记录，写失败分类的匹配规则前要重新取得 |
| `gpt-6-luna` `medium` 的一次短任务记录了 9,024 token | `已验证` 2026-11-13，单次 | 计划。统计口径没有记录 |

## 版本与命令

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| `gpt-6.1-terra` 需要 Codex CLI 0.159.0 或以上 | `文档` | 计划，引自模型页 |
| `codex app-server` 在帮助里标为 `[experimental]`，子命令有 `daemon`、`proxy`、`generate-ts`、`generate-json-schema` | `快照` | `app-server-help.txt` |
| `codex --help` 里没有 `mcp-server` 命令。不要基于它写代码 | `快照` | `help.txt`。第三方文章称它在 0.154.0 被移除 |
| `codex exec` 非交互执行。选项含 `-m/--model`、`-s/--sandbox`（`read-only`、`workspace-write`、`danger-full-access`）、`--json`（事件以 JSONL 输出）、`--output-schema <FILE>`、`--ephemeral`、`-C/--cd` | `快照` | `exec-help.txt` |
| `codex exec --ignore-user-config` 不加载 `$CODEX_HOME/config.toml`，认证仍用 `CODEX_HOME`。`--ignore-rules` 不加载用户和项目的 execpolicy `.rules` 文件 | `快照` | `exec-help.txt` |
| 上面两个选项是否影响 skill 和 `AGENTS.md` 的加载 | `未确认` | |
| `codex review` 非交互执行一次代码审核 | `快照` | `help.txt`。它的选项没有进快照 |
| `codex debug models` 列出本机可用的模型 ID | `已验证` 2026-11-13 | 计划 |
| `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` 升级本机 Codex | `已验证` 2026-11-13 | 计划 |

## 传输与消息格式

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| `codex app-server` 默认使用 stdio 传输，一行一条 JSON | `文档` | 官方文档 |
| 消息是 JSON-RPC 2.0，线上省略 `"jsonrpc":"2.0"` 字段 | `文档` | 官方文档。与快照一致：请求只有 `id`、`method`、`params`、`trace` |
| 通知只有 `method`、`params`，没有 `id` | `快照` | `JSONRPCNotification.json` |
| 错误响应是 `id` 加 `error`，`error` 里有 `code`、`message`、`data` | `快照` | `JSONRPCError.json` |
| 连接后先发 `initialize`。`clientInfo`（`name`、`title`、`version`）必填，`capabilities` 可选 | `快照` | `v1/InitializeParams.json` |
| `initialize` 的响应带 `userAgent`、`codexHome`、`platformFamily`、`platformOs` | `快照` | `v1/InitializeResponse.json` |
| 客户端通知只有一个：`initialized` | `快照` | `ClientNotification.json`。发送时机见官方文档 |
| 使用实验性方法或字段，要在 `capabilities.experimentalApi` 里声明，否则请求被拒绝 | `文档` | 官方文档。`experimentalApi` 字段在快照里 |
| 快照是否包含实验性的方法和字段 | `未确认` | 导出时没有加任何额外选项 |
| WebSocket 传输是实验性的，官方不提供支持。本项目只用 stdio | `文档` | 官方文档 |
| WebSocket 模式下请求队列满时返回错误码 `-32001`，应退避后重试 | `文档` | 官方文档 |
| stdio 模式下是否会出现 `-32001` | `未确认` | |

## 客户端请求

只列本项目会用到的。完整清单在 `ClientRequest.json`。

| 方法 | 参数与响应 | 状态 | 出处 |
| --- | --- | --- | --- |
| `model/list` | 参数 `includeHidden`、`cursor`、`limit`、`modelProvider`，都可选。`includeHidden` 为真时包含默认选择器里隐藏的模型，只用于诊断。响应是 `data` 和 `nextCursor`，要翻页 | `快照` | `v2/ModelListParams.json`、`v2/ModelListResponse.json` |
| `model/list` 的模型条目 | 字段含 `id`、`model`、`displayName`、`hidden`、`isDefault`、`supportedReasoningEfforts`、`defaultReasoningEffort` | `快照` | 合并包里的 `Model` |
| `thread/start` | 参数都可选：`model`、`cwd`、`sandbox`（`read-only`、`workspace-write`、`danger-full-access`）、`approvalPolicy`、`config`、`baseInstructions`、`developerInstructions`、`ephemeral` 等 | `快照` | `v2/ThreadStartParams.json` |
| `thread/start` 的响应 | 报告 `model`、`modelProvider`、`reasoningEffort`、`sandbox`、`approvalPolicy`、`cwd`、`instructionSources`、`thread`。这些是运行时报告的值，可以进配置证据 | `快照` | `v2/ThreadStartResponse.json` |
| `turn/start` | `threadId`、`input` 必填。可选 `model`、`effort`、`sandboxPolicy`、`approvalPolicy`、`cwd`、`outputSchema` 等。响应是 `turn` | `快照` | `v2/TurnStartParams.json` |
| `turn/start` 的覆盖值 | `model`、`effort`、`sandboxPolicy`、`approvalPolicy` 对本回合和之后的回合都生效。每个回合都显式传入，不依赖上一回合留下的值 | `快照` | `v2/TurnStartParams.json` 的字段说明 |
| `turn/start` 的 `sandboxPolicy` | 按 `type` 区分：`readOnly`、`workspaceWrite`、`dangerFullAccess`、`externalSandbox`。`readOnly` 另有布尔字段 `networkAccess` | `快照` | 合并包里的 `SandboxPolicy` |
| `turn/start` 的 `effort` | 非空字符串，取值由模型公布，不是固定枚举。合法值从 `model/list` 的 `supportedReasoningEfforts` 取 | `快照` | 合并包里的 `ReasoningEffort` |
| `turn/start` 的 `outputSchema` | 可选的 JSON Schema，用来约束本回合最终的助手消息 | `快照` | `v2/TurnStartParams.json` |
| `turn/start` 的 `input` | 输入项的类型：`text`、`image`、`localImage`、`audio`、`localAudio`、`skill`、`mention` | `快照` | 合并包里的 `UserInput` |
| `turn/interrupt` | `threadId`、`turnId` 都必填 | `快照` | `v2/TurnInterruptParams.json` |
| `turn/interrupt` 之后 | 等待该回合的终态通知，以终态为取消依据 | `文档` | 计划 |
| `review/start` | `threadId`、`target` 必填，`delivery` 可选（目前只有 `inline`）。`target` 按 `type` 区分：`uncommittedChanges`、`baseBranch`（带 `branch`）、`commit`（带 `sha`、`title`）、`custom`（带 `instructions`）。响应是 `reviewThreadId` 和 `turn` | `快照` | `v2/ReviewStartParams.json`、`v2/ReviewStartResponse.json` |
| `skills/list` | 参数 `cwds`、`forceReload`。每个 cwd 返回 `skills` 和 `errors`，每个 skill 有 `name`、`path`、`scope`、`enabled` 等 | `快照` | `v2/SkillsListParams.json`、合并包里的 `SkillMetadata` |
| `skills/config/write` | `enabled` 必填，用 `path` 或 `name` 指定 skill | `快照` | `v2/SkillsConfigWriteParams.json` |
| `skills/config/write` 写的是用户级配置 | | `文档` | Codex 仓库 README |
| `account/rateLimits/read` | 读取限流状态，结构见下面的 `account/rateLimits/updated` | `快照` | `ClientRequest.json` |
| 其他线程方法 | `thread/resume`、`thread/read`、`thread/fork`、`thread/list`、`thread/archive` 等 | `快照` | `ClientRequest.json` |

## 服务器通知

只列本项目会用到的。完整清单在 `ServerNotification.json`。

| 通知 | 字段 | 状态 | 出处 |
| --- | --- | --- | --- |
| `turn/started`、`turn/completed` | `threadId` 和 `turn`。`turn.status` 的取值：`inProgress`、`completed`、`interrupted`、`failed`。失败时 `turn.error` 带 `message`、`codexErrorInfo`、`additionalDetails` | `快照` | `v2/TurnCompletedNotification.json`，合并包里的 `Turn`、`TurnError` |
| `item/started`、`item/completed`，以及 `item/…/delta` 一类 | 回合内的过程项。`item/completed` 带 `item`、`threadId`、`turnId` | `快照` | `ServerNotification.json`、`v2/ItemCompletedNotification.json` |
| `model/rerouted` | `fromModel`、`toModel`、`reason`、`threadId`、`turnId`。`reason` 目前只有 `highRiskCyberActivity` | `快照` | `v2/ModelReroutedNotification.json` |
| `thread/tokenUsage/updated` | `threadId`、`turnId`、`tokenUsage`。`tokenUsage` 里有 `total`、`last`、`modelContextWindow`，明细字段是 `inputTokens`、`cachedInputTokens`、`cacheWriteInputTokens`、`outputTokens`、`reasoningOutputTokens`、`totalTokens` | `快照` | `v2/ThreadTokenUsageUpdatedNotification.json` |
| `error` | `error`、`threadId`、`turnId`、`willRetry` | `快照` | `v2/ErrorNotification.json` |
| `account/rateLimits/updated` | `rateLimits` 里有 `primary` 和 `secondary` 两个窗口，各带 `usedPercent`、`resetsAt`、`windowDurationMins`，另有 `rateLimitReachedType` | `快照` | 合并包里的 `RateLimitSnapshot`、`RateLimitWindow` |
| `thread/status/changed` | `threadId` 和 `status`（`notLoaded`、`idle`、`active`、`systemError`） | `快照` | `v2/ThreadStatusChangedNotification.json` |
| 这些通知在本机的实际顺序和触发时机 | | `未确认` | 等事件流录制恢复后补 |

## 错误信息 codexErrorInfo

`turn.error.codexErrorInfo` 的取值，状态 `快照`，出处是合并包里的 `CodexErrorInfo`：

- 字符串：`unauthorized`、`rateLimitExceeded`、`usageLimitExceeded`、`sessionBudgetExceeded`、`modelNotFound`、`serverOverloaded`、`policyCapacityUnavailable`、`flexUnavailable`、`contextWindowExceeded`、`internalServerError`、`badRequest`、`sandboxError`、`threadRollbackFailed`、`cyberPolicy`、`misalignmentPolicyViolation`、`tooManyDenials`、`inferenceHookDenied`、`other`。
- 带 `httpStatusCode` 的对象：`httpConnectionFailed`、`responseStreamConnectionFailed`、`responseStreamDisconnected`、`responseTooManyFailedAttempts`。
- 其他对象：`activeTurnNotSteerable`。

这些值怎么映射到计划里的失败分类（认证失效、限流、模型不可用等）还没有定，写映射要走任务卡。各个值在什么情况下出现：`未确认`。

## 服务器发来的请求

服务器会向客户端发请求并等应答，状态 `快照`，出处 `ServerRequest.json`：

`item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`item/permissions/requestApproval`、`item/tool/requestUserInput`、`item/tool/call`、`mcpServer/elicitation/request`、`applyPatchApproval`、`execCommandApproval`、`account/chatgptAuthTokens/refresh`、`attestation/generate`。

- 审批类请求的应答带 `decision`（`快照`，`CommandExecutionRequestApprovalResponse.json`）。
- `approvalPolicy` 的取值是 `untrusted`、`on-request`、`never`，或一个 `granular` 对象（`快照`，合并包里的 `AskForApproval`）。
- 无人值守的会话要明确两件事：用哪个 `approvalPolicy`，以及收到这些请求时怎么应答。按 JSON-RPC 的语义，不应答服务器就会一直等。本机的实际表现：`未确认`。

## 与会话隔离有关

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| Codex 从工作目录到仓库根的每一级 `.agents/skills`、`$HOME/.agents/skills` 和 `/etc/codex/skills` 发现 skill，并把清单放进上下文 | `文档` | 官方文档 |
| 停用 skill 按路径或名字逐个进行：`~/.codex/config.toml` 的 `[[skills.config]]`，或 `skills/config/write` | `文档` | 官方文档。方法在快照里 |
| `skills/extraRoots/set` 方法存在 | `快照` | `ClientRequest.json`。用途没有查 |
| `thread/start` 响应里的 `instructionSources` 是该线程当前加载的指令文件路径，可用来记录会话实际加载了什么 | `快照` | `v2/ThreadStartResponse.json` 的字段说明。内容格式 `未确认` |
| 候选提交里新增的 `.agents/skills/` 会被以该检出为工作目录的会话加载 | `未确认` | 由前两条推出，没有实测 |

## 计划列出的待验证项

本机的模型目录返回、配置是否生效、只读权限、取消、用量和模型报告。App Server 检查和事件流录制暂缓。
