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
| 隔离审核线程的配置与权限方案 | `文档`；共同产品路径无模型命令 `已验证` 2026-11-15，完整审核回合尚未验证 | 见下节 T21；不能由 `thread/start.config`、空 MCP overlay 或 cwd 单独推出隔离成立 |
| 候选提交里新增的 `.agents/skills/` 会被以该检出为工作目录的会话加载 | `未确认` | 由前两条推出，没有实测 |

## T21 审核隔离（0.169.0）

### 来源与证据范围

`protocol/codex/config.schema.json` 是[官方 `rust-v0.169.0` config schema](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/config.schema.json)的原始文件，没有人工改动。SHA256 为 `f515f420927460250181097af8dc72b80a6be4f01f4324a0efef816e46cc9c86`，大小 271771 字节；下载信息见旁边的 `config-schema-source.json`。下述源码结论状态为 `文档`，不能冒充本机模型实测。

2026-11-15 的初步原生无模型 canary 使用产品 `policy_config` 生成配置，并对 `command/exec` 省略 `sandboxPolicy`。证据在本机 `/Users/owen/Desktop/Multi-agent-acceptance/T-0021/native-policy-canary/raw.jsonl`：10 个读取 case 均符合预期，冻结输入可读，外部文件、另一席报告、数据库、答案、认证、符号链接目标、父目录及改 cwd 后的外部文件拒读；改 cwd 后仍能读取绝对路径冻结输入。**这只验证当次原生命令读取路径。** 原始配置摘要仍记 `policy_matches:false`，不能宣称完整配置审计通过；后续配置增补、最终产品集成及真实模型审核回合需要各自证据。未调用 `turn/start`。

同日 `native-policy-final/` 的共同产品路径 canary 记录了配置 gate 通过、10 项读取符合预期及实际退出清理，作为开发证据保留；此时尚未覆盖后补的独立 requirements gate。初步全等失败已由 schema/source 对照解释为 serde 补 null/默认值，旧记录不改写。

严格 requirements 路线的阶段证据在 `/Users/owen/Desktop/Multi-agent-acceptance/T-0021/native-policy-strict-requirements/{summary.json,raw.jsonl}`（`已验证` 2026-11-15，0.169.0）：删除 `forced_login_method`，最小认证材料仍只允许官方 ChatGPT 登录；调用共同产品的 `CodexIsolation.create/spawn/terminate/cleanup` 和 `RuntimeAdapter._collect/_spawn/_audit_codex`，以 `app-server --strict-config` 启动，实际通过 `requirements` 必须存在且为 null 的 gate。10 个读取 case 全部 `matches=true`，版本进程与 App Server 实际退出 0，随后记录 `isolation_removed`，模型请求为 0。此前 `native-policy-requirements/raw.jsonl` 保留了有 forced-login 投影时被严格 gate 拒绝并清理的记录。

**最终验收记录**是 `/Users/owen/Desktop/Multi-agent-acceptance/T-0021/native-policy-accepted/{raw.jsonl,summary.json,metadata.json}`，脚本为上级目录的 `native-policy-accepted.py`，记录时间为 2026-11-15 22:00 UTC。它通过完整字段存在性核对，`isolation_verified` 实际保存 `effective_config`（含官方序列化 null）、真实来源 `["user","system"]` 和 `requirements:null`；`account/read` 实际报告 `type:"chatgpt"`。10/10 case 符合预期，两个进程退出码均为 0，资源清理确认，模型请求为 0。metadata 绑定当次 `runtime.py`、`codex_isolation.py`、`probe/recording.py` 的 SHA256；验收对 32 个输出文件进行实际认证串扫描，未发现泄漏。旧记录保持原貌，不把新日志字段倒填进早期证据。

这些证据覆盖当次共同实现的配置/requirements 审计、原生命令读取和资源生命周期，不扩张到客户端 `fs/readFile`，不证明所有模型工具或完整模型上下文已隔离。真实审核回合仍待 T22。

### Named permissions 与实际执行路径

最小结构是 `default_permissions="multi-agent-frozen-review"`，对应 `[permissions.multi-agent-frozen-review.filesystem]` 的 `":minimal"="read"` 和冻结目录规范化绝对路径 `="read"`，以及 `[permissions.multi-agent-frozen-review.network] enabled=false`。不继承 `:read-only`，不增加 `:root`、临时目录、认证、日志或其他席位根；不使用相对 cwd 授权。实现从空 restricted filesystem 编译这些条目；未知特殊路径可能被忽略并警告，因此必须核对准确键名及有效配置。[权限编译源码](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/config/permissions.rs#L459)

`command/exec` 未指定权限覆盖时取 `self.config.permissions.effective_permission_profile()`，随后传入 `build_exec_request`；指定旧 `sandboxPolicy` 则走另一条覆盖分支。审核线程及回合也必须避免旧 `thread/start.sandbox`、`turn/start.sandboxPolicy` 把 named profile 改回宽泛只读。官方源码中的实验性 `permissionProfile`、`activePermissionProfile` 没有出现在本仓库本机导出结构中，不能据源码擅自加入协议请求或要求响应必有。[命令执行处理器](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/request_processors/command_exec_processor.rs#L250)

macOS `:minimal` 允许系统程序、框架、库与若干系统配置目录读取，包括 `/etc`、`/private/etc`、`/Library/Preferences`、`/var/db`、`/opt/homebrew/lib` 和 `/usr/local/lib`；还有终端、描述符和 `/dev/null` 等必要写入例外。因此它是“受限用户文件读取加系统运行时例外”，不是全文件系统零外读或绝对只写禁止。[原始 Seatbelt 平台规则](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/sandboxing/src/seatbelt_read_only_platform_defaults.sbpl)

模型 `view_image` 把当前环境的 sandbox context 传给 metadata/read；受限文件系统读取走 sandboxed helper，同样使用权限 profile，并额外放行 helper 可执行文件及平台运行时，不能建立沙箱时返回错误。`tool_search` 搜索已注册工具目录，本身不是文件读取工具；它不会约束搜出的 MCP 或远端工具。客户端 `fs/readFile` 则显式使用 `sandbox=None`，具有 App Server 客户端权限的人仍可调用它；产品不能把这个客户端 RPC 暴露为模型读文件后声称沿用了模型沙箱。[模型图片读取](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/tools/handlers/view_image.rs#L150)、[文件读取分派](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/exec-server/src/local_file_system.rs#L93)、[文件系统 helper](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/exec-server/src/fs_sandbox.rs#L112)、[工具搜索](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/tools/handlers/tool_search.rs#L75)、[客户端文件 RPC](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/request_processors/fs_processor.rs#L64)

### 宿主上下文与配置审计

- 临时 HOME/CODEX_HOME 只承接必要的官方订阅认证；模型可读根不包含它们。`project_doc_max_bytes=0` 会跳过项目指令，却不抑制 Codex home 的 `AGENTS.md` / `AGENTS.override.md`，所以需要干净 home 和指令来源审计。[项目加载](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/agents_md.rs#L58)、[全局加载](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/codex-home/src/instructions/mod.rs#L40)
- `skills.include_instructions=false`、`skills.bundled.enabled=false`、`cloud.skills.enabled=false`，并关闭 `features.skill_search`。后者是 shadow selection 开关；前两项不能代替文件权限。`skip_host_skill_discovery` 仅在没有 contributor 要求 host discovery 时才生效，当前 App Server 注册的 HostSkillProvider 会要求发现，不能当作全局禁用开关。[技能配置接线](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/extensions.rs#L114)、[host catalog 条件](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/ext/skills/src/preparation.rs#L85)、[发现需求](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/ext/skills/src/extension.rs#L304)
- 同时关闭 memories 的 `use_memories`、`generate_memories`、`dedicated_tools`，以及相应 features；关闭 plugins、remote_plugin、apps/connectors、hooks、shell snapshots、外部 agent memory import、remote control、子代理和非审核工具。`plugins._default.enabled=false` 可能被具体插件设置覆盖；空 `mcp_servers={}` overlay 也不删除低优先级层，须审计最终集合为空。`orchestrator.mcp.enabled=false` 只控制 orchestrator MCP。`plugin_hooks` 是旧 no-op，`memory_tool` 是 memories 别名，不能算独立保证。[官方配置 schema](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/config.schema.json)、[features 定义](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/features/src/lib.rs)
- 显式关闭 `features.token_budget` 和 `features.context_management`，防止模型默认或 experimental context 开启远端 notes/history；这些后端调用不受本地文件系统沙箱约束。`image_generation` 也有独立工具 gate，审核不需要时关闭，不能由 `network.enabled=false` 推导宿主所有远端工具都被禁用。[notes/history gate](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/ext/history-notes/src/extension.rs#L56)、[模型默认与 experimental context](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/session/token_budget.rs#L22)、[图像工具 gate](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/tools/spec_plan.rs#L821)

`config/read` 要指定冻结 cwd 并请求 `includeLayers=true`。服务端先合并配置，再应用 exact requirements，返回有效 config、origins 和 layers（不返回 packaged-defaults 层）。审计不能只比较 profile 名称：要核对完整关键配置、实际文件授权根、禁用开关及外部工具集合，确认唯一非空用户层是本次临时配置、没有意外 profile/session flags、非空外来系统/MDM/企业配置或未知层；已知空层如实记录。有效值可能经过默认值补全，原始 JSON 不相等不一定表示权限不同，但未经解释也不能算通过。[config/read 实现](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/config_manager_service.rs#L136)、[层来源及优先级](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/config/src/config_layer_source.rs)

`permissions` 和 `default_permissions` 经 API `Config.additional` 的 serde flatten 保留在有效 config 顶层。序列化会补 `description/extends/workspace_roots=null`、`filesystem.glob_scan_max_depth=null` 和未设置的 network 字段；只能按已知结构接受这些 null，不能忽略任意额外非空授权。memories、agents、orchestrator、shell environment 也会补 Option 字段的 null。固定非空默认值来自官方 `config/defaults.toml`（如五类 include 指令/context 开关为 true、`history.persistence="save-all"`、`project_root_markers=[".git"]`），features 还会由 config/read 补选定实验功能的实际值。未知原始 TOML 键可能在解析成 ConfigToml 时被忽略，因此还必须检查原始 layers，不能只查有效 config。[API flatten](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server-protocol/src/protocol/v2/config.rs#L282)、[权限序列化结构](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/config/src/permissions_toml.rs#L113)、[固定默认值](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/config/defaults.toml)、[实验功能回填](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/request_processors/config_processor.rs#L117)

`instructionSources` 只枚举有路径的 user/thread/project 指令；它不列出所有配置字符串、base/developer instructions、插件说明或远端上下文。必须另行拒绝不受控的 `instructions`、`developer_instructions`、`model_instructions_file`、外部 provider/工具等配置，不得把空数组当完整隔离证明。配置或来源无法确认时，在模型请求前失败。[来源枚举](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/agents_md.rs#L486)

还有独立 requirements 边界：`config/read` 的 exact projection 没有覆盖全部 requirements，尤其 `additional_developer_instructions` 会作为独立 managed developer 消息进入上下文，既不必出现在有效 `developer_instructions`，也不属于文件型 `instructionSources`。必须另查本机快照已有的 `configRequirements/read`；`ConfigRequirementsReadResponse.requirements` 的 schema 说明，无 requirements 配置时为 null。不能仅凭自有用户层与空系统 config 层断言没有 MDM/requirements 上下文。[有限投影](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/config/src/config_requirements.rs#L1821)、[独立开发者消息](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/core/src/context/world_state/managed_developer_instructions.rs#L24)

该 schema 的 null 说明还需结合 0.169.0 源码：`forced_login_method="chatgpt"` 本身就会把 AuthManager 的有效方法限制为 `["chatgpt"]`。API 映射发现有效方法不是默认 `["api","chatgpt"]` 时，即使没有 requirements 文件，也会生成 requirements 对象，并填 `allowedLoginMethods`、将其他未配置字段序列化为 null。这解释了开发时自身配置为何被严格 null gate 拒绝，**不是最终允许 requirements 对象的例外**。[登录策略交集](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/config/src/auth_policy.rs#L11)、[API 对象生成](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/request_processors/config_processor.rs#L467)

API 只投影部分 `ConfigRequirementsToml` 字段；隐藏的非空 requirements 也可能产生“登录列表加其他字段全 null”的同形对象。最终方案因此移除自身 `forced_login_method`，拒绝任何 requirements 对象（包括空对象）及缺字段，只接受实际 null。官方服务先判断完整 requirements 是否为空，再做 API 投影；这避免把可见字段为空误当成不存在隐藏要求。订阅约束由最小认证材料加环境检查/实际运行各自的 `account/read` 类型检查保证，不使用 internal token RPC；模型请求前必须观察到 `account.type="chatgpt"`。[完整 requirements 的空判断](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/app-server/src/config_manager_service.rs#L214)、[requirements 完整结构](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/config/src/config_requirements.rs#L1131)

## 计划列出的待验证项

2026-11-15 的实机录制覆盖了本机的模型目录返回、只读权限、取消、三种终态、结构化输出和用量通知，结果写在上面各节。

仍然观察不到的两件事：回合实际使用的模型，回合实际生效的档位。按 `AGENTS.md` 的规定，这两个证据字段记“未确认”，不用请求值顶替。`model/rerouted` 没有出现过，它的实际表现也是 `未确认`。
