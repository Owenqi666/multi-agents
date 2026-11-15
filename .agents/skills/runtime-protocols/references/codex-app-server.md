# Codex App Server 与 Codex CLI

状态的含义见 `SKILL.md`。本表按 2026-11-14 生成的快照核对过（`protocol/codex/`，Codex CLI 0.169.0）。标 `快照` 的条目在 schema 或帮助输出里能找到，名字和结构可信，行为没有验证。与快照不一致时以快照为准，并更新本表。

2026-11-15 用探测工具（`src/multi_agent/adapters/probe/`）在本机做了实机录制，放在 `tests/fixtures/runtime/codex-0.169.0/`，一个步骤一个文件。下文的“夹具 `codex-turn`”指这个目录里的 `codex-turn.jsonl`。标 `已验证` 2026-11-15 的条目都能在对应的夹具里看到。录制时用的是 ChatGPT 登录，模型 `gpt-6.1-sol`，档位 `high`，线程以只读沙箱和 `approvalPolicy: never` 启动。

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
| 登录方式是 ChatGPT 账号：`account/read` 的响应里 `account.type` 为 `chatgpt`，`requiresOpenaiAuth` 为真 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-env` |
| `model/list` 返回 8 个可见模型：`gpt-6.1-sol`（`isDefault`）、`gpt-6-astra`、`gpt-6.1-terra`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`。带 `includeHidden` 时多出 `gpt-reserve` 和 `codex-auto-review`。一页返回完，`nextCursor` 为空 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-env` |
| 档位：Sol、Astra、Terra 各型号支持 `low`、`medium`、`high`、`xhigh`、`max`、`ultra`，Luna 和两个隐藏模型到 `max` 为止。`gpt-6.1-sol` 的 `defaultReasoningEffort` 是 `low` | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-env` |
| 只读沙箱在本机生效，见「只读与审批」 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-sandbox`、`codex-readonly` |
| 事件流不报告回合实际使用的模型和实际生效的档位。`thread/start` 的响应只回显请求的模型，档位报的是用户配置里的值 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-turn`、`codex-bad-model` |
| 一次只回复 OK 的回合用了约 2.1 万输入 token（其中 1.3 万命中缓存），`modelContextWindow` 是 475000 | `已验证` 2026-11-15，0.169.0，单次 | 夹具 `codex-turn` |

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
| `codex app-server` 默认使用 stdio 传输，一行一条 JSON | `已验证` 2026-11-15，0.169.0 | 官方文档；全部夹具 |
| 消息是 JSON-RPC 2.0，线上省略 `"jsonrpc":"2.0"` 字段 | `已验证` 2026-11-15，0.169.0 | 官方文档。与快照一致：请求只有 `id`、`method`、`params`、`trace`。夹具里两个方向的消息都没有这个字段 |
| 通知只有 `method`、`params`，没有 `id` | `快照` | `JSONRPCNotification.json` |
| 错误响应是 `id` 加 `error`，`error` 里有 `code`、`message`、`data` | `快照` | `JSONRPCError.json` |
| 连接后先发 `initialize`。`clientInfo`（`name`、`title`、`version`）必填，`capabilities` 可选 | `快照` | `v1/InitializeParams.json` |
| `initialize` 的响应带 `userAgent`、`codexHome`、`platformFamily`、`platformOs` | `已验证` 2026-11-15，0.169.0 | `v1/InitializeResponse.json`；夹具 `codex-env` |
| 客户端通知只有一个：`initialized`。收到 `initialize` 的响应之后发送，随后的请求都被接受 | `快照`；发送时机 `已验证` 2026-11-15，0.169.0 | `ClientNotification.json`；全部夹具 |
| 使用实验性方法或字段，要在 `capabilities.experimentalApi` 里声明，否则请求被拒绝 | `文档` | 官方文档。`experimentalApi` 字段在快照里 |
| 快照是否包含实验性的方法和字段 | `未确认` | 导出时没有加任何额外选项 |
| 不声明 `experimentalApi` 时这些方法可用：`account/read`、`model/list`、`account/rateLimits/read`、`command/exec`、`thread/start`、`turn/start`、`turn/interrupt` | `已验证` 2026-11-15，0.169.0 | 全部夹具 |
| 快照里有的方法不一定可用：`thread/items/list` 返回错误 `-32601`，消息是 `thread/items/list is not supported yet` | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-turn` 等五个带回合的步骤 |
| 实机的响应里有快照没有的字段：`account/read` 的 `workspaceRouting`，`thread/start` 响应的 `activePermissionProfile`、`multiAgentMode`、`runtimeWorkspaceRoots`，线程对象的 `environments`、`extra` 等。原样记录，不依赖它们 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-env`、`codex-turn` |
| 握手之后不经请求就会收到 `remoteControl/status/changed` 和 `account/updated` | `已验证` 2026-11-15，0.169.0 | 全部夹具 |
| app-server 不往标准错误写任何内容。关闭它的标准输入后，它在三十毫秒内以退出码 0 退出 | `已验证` 2026-11-15，0.169.0 | 全部夹具 |
| WebSocket 传输是实验性的，官方不提供支持。本项目只用 stdio | `文档` | 官方文档 |
| WebSocket 模式下请求队列满时返回错误码 `-32001`，应退避后重试 | `文档` | 官方文档 |
| stdio 模式下是否会出现 `-32001` | `未确认` | |

## 客户端请求

只列本项目会用到的。完整清单在 `ClientRequest.json`。

| 方法 | 参数与响应 | 状态 | 出处 |
| --- | --- | --- | --- |
| `model/list` | 参数 `includeHidden`、`cursor`、`limit`、`modelProvider`，都可选。`includeHidden` 为真时包含默认选择器里隐藏的模型，只用于诊断。响应是 `data` 和 `nextCursor`，要翻页（本机一页返回完） | `已验证` 2026-11-15，0.169.0 | `v2/ModelListParams.json`、`v2/ModelListResponse.json`；夹具 `codex-env` |
| `model/list` 的模型条目 | 字段含 `id`、`model`、`displayName`、`hidden`、`isDefault`、`supportedReasoningEfforts`、`defaultReasoningEffort`。`supportedReasoningEfforts` 的每一项是对象，档位名在 `reasoningEffort` 里 | `已验证` 2026-11-15，0.169.0 | 合并包里的 `Model`；夹具 `codex-env` |
| `thread/start` | 参数都可选：`model`、`cwd`、`sandbox`（`read-only`、`workspace-write`、`danger-full-access`）、`approvalPolicy`、`config`、`baseInstructions`、`developerInstructions`、`ephemeral` 等。实测传的是 `model`、`cwd`、`sandbox: read-only`、`approvalPolicy: never`、`ephemeral: true` | `快照`；实测的五个参数 `已验证` 2026-11-15，0.169.0 | `v2/ThreadStartParams.json`；夹具 `codex-turn` |
| `thread/start` 的响应 | 报告 `model`、`modelProvider`、`reasoningEffort`、`sandbox`、`approvalPolicy`、`cwd`、`instructionSources`、`thread`。`model` 只是回显请求的值，请求一个不存在的模型也照样回显。`reasoningEffort` 是用户配置里的值（本机是 `ultra`），和回合请求的 `effort` 无关。这两项不能当作“实际使用”的证据。`sandbox`（`type: readOnly`、`networkAccess: false`）和 `approvalPolicy` 与请求一致，`instructionSources` 与实际加载的指令文件一致，可以进配置证据 | `已验证` 2026-11-15，0.169.0 | `v2/ThreadStartResponse.json`；夹具 `codex-turn`、`codex-bad-model`、`codex-readonly` |
| `turn/start` | `threadId`、`input` 必填。可选 `model`、`effort`、`sandboxPolicy`、`approvalPolicy`、`cwd`、`outputSchema` 等。响应是 `turn`，此时 `status` 为 `inProgress`、`items` 为空、`itemsView` 为 `notLoaded` | `已验证` 2026-11-15，0.169.0 | `v2/TurnStartParams.json`；夹具 `codex-turn` |
| `turn/start` 的覆盖值 | `model`、`effort`、`sandboxPolicy`、`approvalPolicy` 对本回合和之后的回合都生效。每个回合都显式传入，不依赖上一回合留下的值 | `快照` | `v2/TurnStartParams.json` 的字段说明 |
| `turn/start` 的 `sandboxPolicy` | 按 `type` 区分：`readOnly`、`workspaceWrite`、`dangerFullAccess`、`externalSandbox`。`readOnly` 另有布尔字段 `networkAccess`。`readOnly` 的效果见「只读与审批」 | `快照`；`readOnly` `已验证` 2026-11-15，0.169.0 | 合并包里的 `SandboxPolicy`；夹具 `codex-sandbox` |
| `turn/start` 的 `effort` | 非空字符串，取值由模型公布，不是固定枚举。合法值从 `model/list` 的 `supportedReasoningEfforts` 取。`high` 被接受；回合实际生效的档位在事件流里观察不到 | `快照`；接受 `high` `已验证` 2026-11-15，0.169.0 | 合并包里的 `ReasoningEffort`；夹具 `codex-turn` |
| `turn/start` 的 `outputSchema` | 可选的 JSON Schema，用来约束本回合最终的助手消息。最终的 `agentMessage` 的 `text` 是符合 schema 的 JSON 字符串，要自己解析 | `已验证` 2026-11-15，0.169.0 | `v2/TurnStartParams.json`；夹具 `codex-structured` |
| `turn/start` 的 `input` | 输入项的类型：`text`、`image`、`localImage`、`audio`、`localAudio`、`skill`、`mention` | `快照` | 合并包里的 `UserInput` |
| `turn/interrupt` | `threadId`、`turnId` 都必填。响应是空对象 | `已验证` 2026-11-15，0.169.0 | `v2/TurnInterruptParams.json`；夹具 `codex-cancel` |
| `turn/interrupt` 之后 | 等待该回合的终态通知，以终态为取消依据。实测几毫秒后收到 `turn/completed`，`turn.status` 为 `interrupted`；线程回到 `idle`，app-server 继续运行 | `已验证` 2026-11-15，0.169.0 | 计划；夹具 `codex-cancel` |
| `review/start` | `threadId`、`target` 必填，`delivery` 可选（目前只有 `inline`）。`target` 按 `type` 区分：`uncommittedChanges`、`baseBranch`（带 `branch`）、`commit`（带 `sha`、`title`）、`custom`（带 `instructions`）。响应是 `reviewThreadId` 和 `turn` | `快照` | `v2/ReviewStartParams.json`、`v2/ReviewStartResponse.json` |
| `skills/list` | 参数 `cwds`、`forceReload`。每个 cwd 返回 `skills` 和 `errors`，每个 skill 有 `name`、`path`、`scope`、`enabled` 等 | `快照` | `v2/SkillsListParams.json`、合并包里的 `SkillMetadata` |
| `skills/config/write` | `enabled` 必填，用 `path` 或 `name` 指定 skill | `快照` | `v2/SkillsConfigWriteParams.json` |
| `skills/config/write` 写的是用户级配置 | | `文档` | Codex 仓库 README |
| `account/rateLimits/read` | 读取限流状态，结构见下面的 `account/rateLimits/updated`。响应的顶层是 `rateLimits`、`rateLimitsByLimitId`、`rateLimitResetCredits`、`ordinaryUsageAllowed`、`accountId`、`rateLimitUpsell` | `已验证` 2026-11-15，0.169.0 | `ClientRequest.json`、`v2/GetAccountRateLimitsResponse.json`；夹具 `codex-env` |
| `account/read` | 参数 `refreshToken`。响应是 `account`（`type`、`email`、`planType`、`workspaceName`）、`userId`、`requiresOpenaiAuth` | `已验证` 2026-11-15，0.169.0 | `v2/GetAccountParams.json`、`v2/GetAccountResponse.json`；夹具 `codex-env` |
| `command/exec` | `command`（参数列表）必填，可选 `cwd`、`sandboxPolicy` 等。不经过模型，直接在指定的沙箱策略下运行一条命令。响应是 `exitCode`、`stdout`、`stderr` | `已验证` 2026-11-15，0.169.0 | `v2/CommandExecParams.json`、`v2/CommandExecResponse.json`；夹具 `codex-sandbox` |
| `thread/items/list` | 快照里有（`threadId`、`cursor` 等），0.169.0 不支持，返回 `-32601` | `已验证` 2026-11-15，0.169.0 | `v2/ThreadItemsListParams.json`；夹具 `codex-turn` |
| 其他线程方法 | `thread/resume`、`thread/read`、`thread/fork`、`thread/list`、`thread/archive` 等 | `快照` | `ClientRequest.json` |

## 只读与审批

| 事实 | 状态 | 证据 |
| --- | --- | --- |
| `command/exec` 的 `sandboxPolicy` 为 `readOnly` 时可以读文件，写文件被系统拒绝（`touch` 退出码 1，`Operation not permitted`）。同一个目录在 `workspaceWrite` 且 `writableRoots` 含该目录时可以写 | `已验证` 2026-11-15，0.169.0，macOS | 夹具 `codex-sandbox` |
| 线程以 `sandbox: read-only` 启动、回合带 `sandboxPolicy.type: readOnly` 时，模型能读工作目录里的文件，写入失败，工作目录的内容前后一致 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-readonly` |
| 回合里被沙箱拒绝的那次写入尝试在事件流里没有任何 item，读文件的命令有。事件流不是完整的操作记录，只读要靠沙箱保证，不能靠检查事件 | `已验证` 2026-11-15，0.169.0，两次录制一致 | 夹具 `codex-readonly`（第一次录制没有入库） |
| `approvalPolicy` 为 `never`、沙箱只读时，五个带回合的步骤里服务器没有发来任何请求 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-turn` 等五个带回合的步骤 |

## 服务器通知

只列本项目会用到的。完整清单在 `ServerNotification.json`。

| 通知 | 字段 | 状态 | 出处 |
| --- | --- | --- | --- |
| `turn/started`、`turn/completed` | `threadId` 和 `turn`。`turn.status` 的取值：`inProgress`、`completed`、`interrupted`、`failed`。失败时 `turn.error` 带 `message`、`codexErrorInfo`、`additionalDetails`。`completed`、`interrupted`、`failed` 三种终态都录到了。`turn/completed` 里的 `turn.items` 只是摘要：正常结束时 `itemsView` 为 `summary`，只有最终的 `agentMessage`；取消和失败时 `itemsView` 为 `notLoaded`，`items` 为空 | `已验证` 2026-11-15，0.169.0 | `v2/TurnCompletedNotification.json`，合并包里的 `Turn`、`TurnError`；夹具 `codex-turn`、`codex-cancel`、`codex-bad-model` |
| `item/started`、`item/completed`，以及 `item/…/delta` 一类 | 回合内的过程项。`item/completed` 带 `item`、`threadId`、`turnId`。录到的 `item.type`：`userMessage`、`agentMessage`（`phase` 为 `commentary` 或 `final_answer`）、`commandExecution`（带 `command`、`aggregatedOutput`、`exitCode`）、`reasoning`。delta 只见到 `item/agentMessage/delta` | `已验证` 2026-11-15，0.169.0 | `ServerNotification.json`、`v2/ItemCompletedNotification.json`；夹具 `codex-readonly` |
| `model/rerouted` | `fromModel`、`toModel`、`reason`、`threadId`、`turnId`。`reason` 目前只有 `highRiskCyberActivity`。录制里没有出现过 | `快照` | `v2/ModelReroutedNotification.json` |
| `thread/tokenUsage/updated` | `threadId`、`turnId`、`tokenUsage`。`tokenUsage` 里有 `total`、`last`、`modelContextWindow`，明细字段是 `inputTokens`、`cachedInputTokens`、`cacheWriteInputTokens`、`outputTokens`、`reasoningOutputTokens`、`totalTokens`。每次模型调用之后各有一条；被取消和失败的回合没有 | `已验证` 2026-11-15，0.169.0 | `v2/ThreadTokenUsageUpdatedNotification.json`；夹具 `codex-readonly`、`codex-cancel` |
| `error` | `error`、`threadId`、`turnId`、`willRetry` | `已验证` 2026-11-15，0.169.0 | `v2/ErrorNotification.json`；夹具 `codex-bad-model` |
| `account/rateLimits/updated` | `rateLimits` 里有 `primary` 和 `secondary` 两个窗口，各带 `usedPercent`、`resetsAt`、`windowDurationMins`，另有 `rateLimitReachedType`。回合里每次模型调用之后会出现 | `已验证` 2026-11-15，0.169.0 | 合并包里的 `RateLimitSnapshot`、`RateLimitWindow`；夹具 `codex-turn` |
| `thread/status/changed` | `threadId` 和 `status`（`notLoaded`、`idle`、`active`、`systemError`）。回合开始时变 `active`，结束或取消后回到 `idle`，模型不存在时变 `systemError` | `已验证` 2026-11-15，0.169.0 | `v2/ThreadStatusChangedNotification.json`；夹具 `codex-turn`、`codex-bad-model` |
| `thread/started` | `thread/start` 的响应之后到达，带同一个线程对象 | `已验证` 2026-11-15，0.169.0 | `ServerNotification.json`；夹具 `codex-turn` |
| `mcpServer/startupStatus/updated` | `threadId`、`name`、`status`（录到 `starting`、`ready`、`failed`）、`error`。线程启动后每个 MCP 服务器各有几条 | `已验证` 2026-11-15，0.169.0 | `ServerNotification.json`；夹具 `codex-turn` |
| `warning` | `threadId`、`message`。录到两种：用户配置开启了开发中的功能；请求的模型没有元数据 | `已验证` 2026-11-15，0.169.0 | `v2/WarningNotification.json`；夹具 `codex-turn`、`codex-bad-model` |
| `account/updated`、`remoteControl/status/changed` | 前者带 `authMode`、`planType`，后者带 `status` 和本机的标识。握手之后不经请求就到达 | `已验证` 2026-11-15，0.169.0 | `v2/AccountUpdatedNotification.json`、`v2/RemoteControlStatusChangedNotification.json`；夹具 `codex-env` |
| 一个正常回合的顺序 | `turn/start` 的响应 → `thread/status/changed`（`active`）→ `turn/started` → `item/started` 和 `item/completed`（`userMessage`）→ `item/started`（`agentMessage`）→ `item/agentMessage/delta` → `item/completed` → `thread/tokenUsage/updated` → `account/rateLimits/updated` → `thread/status/changed`（`idle`）→ `turn/completed`。MCP 服务器的启动通知穿插在前半段 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-turn` |

## 错误信息 codexErrorInfo

`turn.error.codexErrorInfo` 的取值，状态 `快照`，出处是合并包里的 `CodexErrorInfo`：

- 字符串：`unauthorized`、`rateLimitExceeded`、`usageLimitExceeded`、`sessionBudgetExceeded`、`modelNotFound`、`serverOverloaded`、`policyCapacityUnavailable`、`flexUnavailable`、`contextWindowExceeded`、`internalServerError`、`badRequest`、`sandboxError`、`threadRollbackFailed`、`cyberPolicy`、`misalignmentPolicyViolation`、`tooManyDenials`、`inferenceHookDenied`、`other`。
- 带 `httpStatusCode` 的对象：`httpConnectionFailed`、`responseStreamConnectionFailed`、`responseStreamDisconnected`、`responseTooManyFailedAttempts`。
- 其他对象：`activeTurnNotSteerable`。

这些值怎么映射到计划里的失败分类（认证失效、限流、模型不可用等）还没有定，写映射要走任务卡。

`modelNotFound` 在请求不存在的模型时出现（`已验证` 2026-11-15，0.169.0，夹具 `codex-bad-model`）：`thread/start` 和 `turn/start` 都照常返回，随后线程状态变成 `systemError`，收到 `error` 通知（`willRetry` 为假），`turn/completed` 的 `turn.status` 是 `failed`。`turn.error.message` 的原文是 `The '<模型名>' model is not supported when using Codex with a ChatGPT account.`。其余各个值在什么情况下出现：`未确认`。

## 服务器发来的请求

服务器会向客户端发请求并等应答，状态 `快照`，出处 `ServerRequest.json`：

`item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`item/permissions/requestApproval`、`item/tool/requestUserInput`、`item/tool/call`、`mcpServer/elicitation/request`、`applyPatchApproval`、`execCommandApproval`、`account/chatgptAuthTokens/refresh`、`attestation/generate`。

- 审批类请求的应答带 `decision`（`快照`，`CommandExecutionRequestApprovalResponse.json`）。
- `approvalPolicy` 的取值是 `untrusted`、`on-request`、`never`，或一个 `granular` 对象（`快照`，合并包里的 `AskForApproval`）。
- 无人值守的会话要明确两件事：用哪个 `approvalPolicy`，以及收到这些请求时怎么应答。按 JSON-RPC 的语义，不应答服务器就会一直等。`approvalPolicy` 为 `never`、沙箱只读时，五个带回合的步骤里服务器没有发来任何请求（`已验证` 2026-11-15，0.169.0）。其他配置下的表现：`未确认`。

## 与会话隔离有关

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| Codex 从工作目录到仓库根的每一级 `.agents/skills`、`$HOME/.agents/skills` 和 `/etc/codex/skills` 发现 skill，并把清单放进上下文 | `文档` | 官方文档 |
| 停用 skill 按路径或名字逐个进行：`~/.codex/config.toml` 的 `[[skills.config]]`，或 `skills/config/write` | `文档` | 官方文档。方法在快照里 |
| `skills/extraRoots/set` 方法存在 | `快照` | `ClientRequest.json`。用途没有查 |
| `thread/start` 响应里的 `instructionSources` 是该线程当前加载的指令文件路径，可用来记录会话实际加载了什么。工作目录里有 `AGENTS.md` 时是它的绝对路径，没有时是空数组 | `已验证` 2026-11-15，0.169.0 | `v2/ThreadStartResponse.json` 的字段说明；夹具 `codex-readonly`、`codex-turn` |
| 工作目录里的 `AGENTS.md` 会被当作指令加载，模型照做。以候选检出为工作目录的审核会话，会执行候选提交里的 `AGENTS.md` | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-readonly`（最终回答里带着 `AGENTS.md` 要求的暗号） |
| 每个线程都会启动用户配置的 MCP 服务器，只读线程也一样。本机有 5 个，约 1 秒内就绪，其中 1 个启动失败 | `已验证` 2026-11-15，0.169.0 | 夹具 `codex-turn` |
| 怎样让一个线程不加载 `AGENTS.md`、不启动 MCP 服务器 | `未确认` | `thread/start` 有 `config` 参数，没有试过 |
| 候选提交里新增的 `.agents/skills/` 会被以该检出为工作目录的会话加载 | `未确认` | 由前两条推出，没有实测 |

## 计划列出的待验证项

2026-11-15 的实机录制覆盖了本机的模型目录返回、只读权限、取消、三种终态、结构化输出和用量通知，结果写在上面各节。

仍然观察不到的两件事：回合实际使用的模型，回合实际生效的档位。按 `AGENTS.md` 的规定，这两个证据字段记“未确认”，不用请求值顶替。`model/rerouted` 没有出现过，它的实际表现也是 `未确认`。
