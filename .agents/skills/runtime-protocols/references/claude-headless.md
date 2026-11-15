# Claude Code 无头模式

状态的含义见 `SKILL.md`。本表按 2026-11-14 生成的快照核对过（`protocol/claude/help.txt`，Claude Code 2.1.325）。Claude 这边没有 schema 导出，快照只覆盖 CLI 选项，事件和结果字段的依据是官方文档和 2026-11-15 的实机录制。与帮助输出不一致时以帮助输出为准，并更新本表。

2026-11-15 用探测工具（`src/multi_agent/adapters/probe/`）在本机做了实机录制，放在 `tests/fixtures/runtime/claude-2.1.325/`，一个步骤一个文件。下文的“夹具 `claude-turn`”指这个目录里的 `claude-turn.jsonl`。标 `已验证` 2026-11-15 的条目都能在对应的夹具里看到。录制时用的是订阅登录，命令是 `claude -p <提示词> --model claude-opus-5-5 --effort high --output-format stream-json --verbose --safe-mode --tools Read,Grep,Glob --permission-prompts none --no-session-persistence`，子进程的环境里去掉了 `ANTHROPIC_API_KEY`。

## 官方文档

- 无头运行：https://code.claude.com/docs/en/headless
- CLI 参数：https://code.claude.com/docs/en/cli-reference
- 模型与推理档位：https://code.claude.com/docs/en/model-config
- 结果字段（SDK 类型）：https://code.claude.com/docs/en/agent-sdk/typescript
- 环境变量：https://code.claude.com/docs/en/env-vars
- 文档索引：https://code.claude.com/docs/llms.txt

## 本机记录

| 事实 | 状态 | 证据 |
| --- | --- | --- |
| Claude Code 为 2.1.325，从 2.1.174 升级而来 | `已验证` 2026-11-13、2026-11-14 | 计划「本机接入待验证项」；`protocol/claude/VERSION` |
| `claude -p` 短任务分别请求 Sonnet 5.5 `medium` 和 Opus 5.5 `high`，都返回 OK | `已验证` 2026-11-13，2.1.325，仅连通性 | 计划 |
| 上述短任务实际使用的模型和生效的档位 | `未确认` | 计划注明以可观察证据为准，当时没有记录 |
| 去掉环境里的 `ANTHROPIC_API_KEY` 之后用的是订阅登录：`claude auth status` 报 `authMethod` 为 `claude.ai`，`system/init` 报 `apiKeySource` 为 `none` | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-env`、`claude-turn` |
| 环境里有 `ANTHROPIC_API_KEY` 时，`claude auth status` 报 `authMethod` 为 `api_key`、`apiKeySource` 为 `ANTHROPIC_API_KEY`，订阅登录被它顶替 | `已验证` 2026-11-15，2.1.325 | 第一次录制，没有入库 |
| `claude auth status` 输出多行 JSON，退出码 0 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-env` |
| `claude auth status` 不检查登录是否还有效：它报 `loggedIn: true` 之后，紧接着的调用因为登录过期返回 401 | `已验证` 2026-11-15，2.1.325，一次 | 用户终端里的输出没有录制；过期的那次调用见夹具 `claude-auth-expired` |
| 实际使用的模型可以从结果的 `modelUsage` 看到：键是 `claude-opus-5-5`，条目里另有 `canonicalModel` | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-turn` |
| 实际生效的档位观察不到：事件和结果里都没有这个值，`system/init` 只有一个布尔字段 `per_turn_effort_active` | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-turn` |

## 模型与档位

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| `--model <model>` 接受别名或完整的模型名。不存在的模型名在启动时不报错，见「终态与退出码」 | `快照`；完整的模型名 `已验证` 2026-11-15，2.1.325 | `help.txt`；夹具 `claude-turn`、`claude-bad-model` |
| `--effort <level>` 的取值：`low`、`medium`、`high`、`xhigh`、`max` | `快照`；`high` 被接受 `已验证` 2026-11-15，2.1.325 | `help.txt`；夹具 `claude-turn` |
| 模型 ID：`claude-opus-5-5`、`claude-sonnet-5-5` | `claude-opus-5-5` `已验证` 2026-11-15，2.1.325；`claude-sonnet-5-5` `文档` | 计划；夹具 `claude-turn` |
| Opus 5.5 需要 2.1.280 或以上，Sonnet 5.5 需要 2.1.284 或以上 | `文档` | 计划，引自模型配置页 |
| 档位显式传入，不依赖默认值 | `文档` | 计划 |

## 调用与输出

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| `-p/--print` 非交互运行一次 | `已验证` 2026-11-15，2.1.325 | `help.txt`、计划；全部夹具 |
| `--output-format` 的取值：`text`（默认）、`json`（单个结果）、`stream-json`（实时流）。只在 `--print` 下有效 | `快照`；`stream-json` `已验证` 2026-11-15，2.1.325 | `help.txt`；全部夹具 |
| `--input-format` 的取值：`text`（默认）、`stream-json`（实时流式输入）。只在 `--print` 下有效 | `快照` | `help.txt` |
| `--include-partial-messages` 只在 `--print` 且 `--output-format=stream-json` 时有效 | `快照` | `help.txt` |
| `stream-json` 是否必须同时加 `--verbose` | `未确认` | 帮助输出没有说明。官方示例里两者总是一起用，录制时也是一起用的 |
| `--json-schema <schema>` 用 JSON Schema 校验结构化输出，只在 `--print` 下有效。带上它之后工具清单里多出 `StructuredOutput`，模型调用它提交结果。结果的 `structured_output` 是对象，`result` 是同一内容的 JSON 字符串 | `已验证` 2026-11-15，2.1.325 | `help.txt`；夹具 `claude-structured` |
| `--max-budget-usd <amount>` 设定 API 调用的花费上限，只在 `--print` 下有效 | `快照` | `help.txt` |
| `--no-session-persistence` 不把会话存盘，只在 `--print` 下有效 | `快照` | `help.txt`。录制时带着它，有没有存盘没有检查 |
| 结果里的 `modelUsage` 用来核对运行时报告过的模型。取消和 API 报错时它是空对象 | `已验证` 2026-11-15，2.1.325 | 计划；夹具 `claude-turn`、`claude-cancel` |
| 结果里的 `total_cost_usd` 是客户端估算值，展示时按估算标注。成功时有值，取消和 API 报错时是 0 | `文档`；字段的出现 `已验证` 2026-11-15，2.1.325 | 计划；夹具 `claude-turn`、`claude-cancel` |
| 流的最后一条是 `type: "result"`。字段含 `subtype`、`is_error`、`terminal_reason`、`stop_reason`、`api_error_status`、`num_turns`、`result`、`usage`、`modelUsage`、`total_cost_usd`、`permission_denials`、`duration_ms`、`credential_expires_at`（登录到期时间）等 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-turn` |
| 结果行之后进程还要 0.6 到 0.7 秒才退出 | `已验证` 2026-11-15，2.1.325 | 全部夹具 |

## 终态与退出码

| 情况 | 结果行 | 退出码 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| 正常完成 | `subtype: success`、`is_error: false`、`terminal_reason: completed`、`stop_reason: end_turn` | 0 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-turn` |
| 结构化输出 | `subtype: success`、`is_error: false`、`terminal_reason: completed`、`stop_reason: tool_use`、`num_turns: 2`，带 `structured_output` | 0 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-structured` |
| SIGINT 取消 | `subtype: error_during_execution`、`is_error: true`、`terminal_reason: aborted_streaming` | 0 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-cancel` |
| 模型不存在 | `subtype: success`、`is_error: true`、`terminal_reason: api_error`、`api_error_status: 404`、`api_error_type: not_found_error` | 1 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-bad-model` |
| 登录过期 | `subtype: success`、`is_error: true`、`terminal_reason: api_error`、`api_error_status: 401`、`api_error_type: authentication_error`，之前有两条 `api_retry` | 1 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-auth-expired` |

判断成败看 `is_error` 和 `terminal_reason`。`subtype` 在 API 报错时仍然是 `success`，退出码在取消之后是 0，这两个都不能单独用。

## 流事件

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| 流的第一条事件是 `type: "system"`、`subtype: "init"`，报告会话元数据：模型、工具、MCP 服务器、已加载的插件。其中 `model` 只是回显请求的模型名，不存在的模型也照样回显 | `已验证` 2026-11-15，2.1.325 | 官方文档；夹具 `claude-turn`、`claude-bad-model` |
| `init` 事件的字段包括 `mcp_servers`、`slash_commands`、`skills`、`plugins`。可用来把实际加载的 skill 和插件记入配置证据。另有 `tools`、`permissionMode`、`apiKeySource`、`claude_code_version`、`cwd`、`session_id`、`agents`、`capabilities` 等 | `已验证` 2026-11-15，2.1.325 | SDK 类型定义；夹具 `claude-turn` |
| 插件因依赖不满足被降级时，`init` 事件带 `plugin_errors`（2.1.111 起） | `文档` | 更新日志 |
| API 请求因可重试的错误失败时，重试前会发出 `system` / `api_retry` 事件。字段是 `attempt`、`max_retries`、`retry_delay_ms`、`error_status`、`error` | `已验证` 2026-11-15，2.1.325 | 官方文档；夹具 `claude-auth-expired` |
| `api_retry` 的错误类别包含 `authentication_failed`、`rate_limit` 等 | `authentication_failed`（登录过期，`error_status` 为 401）`已验证` 2026-11-15，2.1.325；其余 `未确认` | 夹具 `claude-auth-expired`；其余来自第三方整理，写失败分类的映射前要核对 |
| 录到的事件类型：`system`（`init`、`api_retry`、`thinking_tokens`）、`assistant`、`user`、`rate_limit_event`、`result`。完整字段以夹具为准 | `已验证` 2026-11-15，2.1.325 | 全部夹具 |
| `assistant` 事件的 `message.content` 是内容块：`text`、`tool_use`、`thinking`。事件上另有 `request_id`。被打断的那条带 `aborted: true`。API 报错时是一条合成消息：`message.model` 为 `<synthetic>`，事件上带 `error`、`is_api_error_message`、`api_error_status` | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-readonly`、`claude-cancel`、`claude-bad-model` |
| `user` 事件承载工具结果（`tool_result`）。取消之后有一条文本 `[Request interrupted by user]` | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-readonly`、`claude-cancel` |
| `rate_limit_event` 带 `rate_limit_info`：`status`、`rateLimitType`、`resetsAt` 和各个窗口的 `utilization` | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-turn` |
| 模型名不认识时，标准错误有一行以 `[claude-code:unrecognized_model]` 开头的文本。其余步骤的标准错误没有输出 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-bad-model` |

## 会话隔离

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| 不加隔离选项时，`claude -p` 加载的上下文和交互式会话相同，包括工作目录和 `~/.claude` 里配置的一切 | `文档` | 官方文档 |
| `--safe-mode` 停用全部自定义项：CLAUDE.md、skill、已安装的插件、hooks、MCP 服务器、自定义命令和 agent、输出样式、工作流等。认证、模型选择、内置工具和内置插件、权限照常。等价于 `CLAUDE_CODE_SAFE_MODE=1`。实测工作目录里的 `CLAUDE.md` 没有被加载（它要求的暗号没有出现在回答里），`mcp_servers` 是空的 | `快照`；`CLAUDE.md` 和 MCP 两项 `已验证` 2026-11-15，2.1.325 | `help.txt`；夹具 `claude-readonly` |
| `--safe-mode` 下管理员下发的策略设置仍然生效 | `快照` | `help.txt`。与计划里"受管理的 hooks 仍可能运行"一致 |
| 审核会话用 `--safe-mode --tools Read,Grep,Glob --permission-prompts none` 能达到只读效果：`system/init` 的 `tools` 只有这三个，模型读得到文件，没有能写文件或执行命令的工具，工作目录的内容前后一致，`permission_denials` 是空的 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-readonly` |
| `--tools <tools...>` 限定可用的内置工具：`""` 停用全部，`default` 用默认集合，或列出工具名。可用来划定只读工具范围 | `快照`；列出工具名 `已验证` 2026-11-15，2.1.325 | `help.txt`；夹具 `claude-turn` |
| `--permission-prompts <target>` 指定 `--print` 下谁来应答权限询问：`host`（SDK 宿主或 `--permission-prompt-tool`）或 `none`。`none` 表示没有人应答，凡是需要询问的操作自动拒绝，其余仍由权限模式决定 | `快照` | `help.txt`。录制时用的是 `none`，没有出现需要询问的操作 |
| `--allowedTools`、`--disallowedTools` 选项存在 | `快照` | `help.txt` |
| `--permission-mode` 的取值：`acceptEdits`、`auto`、`bypassPermissions`、`manual`、`dontAsk`、`plan` | `快照` | `help.txt` |
| `--setting-sources <sources>` 只加载列出的设置来源，可选 `user`、`project`、`local` | `快照` | `help.txt` |
| `--disable-slash-commands` 停用全部 skill 和命令 | `快照` | `help.txt` |
| `--bare` 跳过 hooks、LSP、插件同步、自动记忆、钥匙串读取和 CLAUDE.md 自动发现。认证只认 `ANTHROPIC_API_KEY` 或 `apiKeyHelper`，不读 OAuth 和钥匙串。这与"复用已登录"冲突，本项目不用它 | `快照` | `help.txt` |
| 无头模式下，在 prompt 里写 `/skill-name` 可以调用用户可调用的 skill | `文档` | 官方文档 |
| 目录名为 `verify` 的项目或个人 skill 会在提交前被运行（2.1.286 起）。在 `-p` 会话里是否生效 | `未确认` | |
| 环境里的 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` 会让 `claude` 改用 API 凭据，不用订阅登录。工作台启动 Claude 时从子进程的环境里去掉这两个变量（用户 2026-11-15 的决定） | `ANTHROPIC_API_KEY` `已验证` 2026-11-15，2.1.325；`ANTHROPIC_AUTH_TOKEN` `文档` | 官方环境变量文档；第一次录制（没有入库）。夹具开头的 `env_removed` 记录了探测工具去掉的变量，`claude-auth-expired` 除外 |

## 取消

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| 发送 SIGINT 尝试结束当前轮次，宽限期后按需停止进程 | `文档`；SIGINT 能结束当前轮次 `已验证` 2026-11-15，2.1.325 | 计划；夹具 `claude-cancel` |
| 取消以进程停止为依据，已有记录保留 | `文档` | 计划 |
| SIGINT 之后进程会输出终态结果。十几毫秒内依次是：被打断的 `assistant` 消息（`aborted: true`）、一条 `user` 文本 `[Request interrupted by user]`、结果行（见「终态与退出码」）。进程约 0.6 秒后退出，退出码是 0，所以不能靠退出码判断是否被取消 | `已验证` 2026-11-15，2.1.325 | 夹具 `claude-cancel` |

## 计划列出的待验证项

2026-11-15 的实机录制覆盖了只读权限、取消、终态、结构化输出、用量和模型报告，结果写在上面各节。

仍然观察不到的一件事：回合实际生效的档位。按 `AGENTS.md` 的规定，这个证据字段记“未确认”，不用请求值顶替。限流时的事件和结果没有录到，也是 `未确认`。
