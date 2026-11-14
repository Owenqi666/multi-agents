# Claude Code 无头模式

状态的含义见 `SKILL.md`。本表按 2026-11-14 生成的快照核对过（`protocol/claude/help.txt`，Claude Code 2.1.325）。Claude 这边没有 schema 导出，快照只覆盖 CLI 选项，事件和结果字段仍然只有文档依据。与帮助输出不一致时以帮助输出为准，并更新本表。

## 官方文档

- 无头运行：https://code.claude.com/docs/en/headless
- CLI 参数：https://code.claude.com/docs/en/cli-reference
- 模型与推理档位：https://code.claude.com/docs/en/model-config
- 结果字段（SDK 类型）：https://code.claude.com/docs/en/agent-sdk/typescript
- 文档索引：https://code.claude.com/docs/llms.txt

## 本机记录

| 事实 | 状态 | 证据 |
| --- | --- | --- |
| Claude Code 为 2.1.325，从 2.1.174 升级而来 | `已验证` 2026-11-13、2026-11-14 | 计划「本机接入待验证项」；`protocol/claude/VERSION` |
| `claude -p` 短任务分别请求 Sonnet 5.5 `medium` 和 Opus 5.5 `high`，都返回 OK | `已验证` 2026-11-13，2.1.325，仅连通性 | 计划 |
| 上述短任务实际使用的模型和生效的档位 | `未确认` | 计划注明以可观察证据为准，当时没有记录 |

## 模型与档位

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| `--model <model>` 接受别名或完整的模型名 | `快照` | `help.txt` |
| `--effort <level>` 的取值：`low`、`medium`、`high`、`xhigh`、`max` | `快照` | `help.txt` |
| 模型 ID：`claude-opus-5-5`、`claude-sonnet-5-5` | `文档` | 计划 |
| Opus 5.5 需要 2.1.280 或以上，Sonnet 5.5 需要 2.1.284 或以上 | `文档` | 计划，引自模型配置页 |
| 档位显式传入，不依赖默认值 | `文档` | 计划 |

## 调用与输出

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| `-p/--print` 非交互运行一次 | `快照`；连通性 `已验证` 2026-11-13 | `help.txt`、计划 |
| `--output-format` 的取值：`text`（默认）、`json`（单个结果）、`stream-json`（实时流）。只在 `--print` 下有效 | `快照` | `help.txt` |
| `--input-format` 的取值：`text`（默认）、`stream-json`（实时流式输入）。只在 `--print` 下有效 | `快照` | `help.txt` |
| `--include-partial-messages` 只在 `--print` 且 `--output-format=stream-json` 时有效 | `快照` | `help.txt` |
| `stream-json` 是否必须同时加 `--verbose` | `未确认` | 帮助输出没有说明。官方示例里两者总是一起用 |
| `--json-schema <schema>` 用 JSON Schema 校验结构化输出，只在 `--print` 下有效 | `快照` | `help.txt` |
| `--max-budget-usd <amount>` 设定 API 调用的花费上限，只在 `--print` 下有效 | `快照` | `help.txt` |
| `--no-session-persistence` 不把会话存盘，只在 `--print` 下有效 | `快照` | `help.txt` |
| 结果里的 `modelUsage` 用来核对运行时报告过的模型 | `文档` | 计划 |
| 结果里的 `total_cost_usd` 是客户端估算值，展示时按估算标注 | `文档` | 计划 |

## 流事件

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| 流的第一条事件是 `type: "system"`、`subtype: "init"`，报告会话元数据：模型、工具、MCP 服务器、已加载的插件 | `文档` | 官方文档 |
| `init` 事件的字段包括 `mcp_servers`、`slash_commands`、`skills`、`plugins`。可用来把实际加载的 skill 和插件记入配置证据 | `文档` | SDK 类型定义 |
| 插件因依赖不满足被降级时，`init` 事件带 `plugin_errors`（2.1.111 起） | `文档` | 更新日志 |
| API 请求因可重试的错误失败时，重试前会发出 `system` / `api_retry` 事件 | `文档` | 官方文档 |
| `api_retry` 的错误类别包含 `authentication_failed`、`rate_limit` 等 | `未确认` | 第三方整理。写失败分类的映射前要核对 |
| 各事件的完整字段 | `未确认` | 等事件流录制恢复后补 |

## 会话隔离

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| 不加隔离选项时，`claude -p` 加载的上下文和交互式会话相同，包括工作目录和 `~/.claude` 里配置的一切 | `文档` | 官方文档 |
| `--safe-mode` 停用全部自定义项：CLAUDE.md、skill、已安装的插件、hooks、MCP 服务器、自定义命令和 agent、输出样式、工作流等。认证、模型选择、内置工具和内置插件、权限照常。等价于 `CLAUDE_CODE_SAFE_MODE=1` | `快照` | `help.txt` |
| `--safe-mode` 下管理员下发的策略设置仍然生效 | `快照` | `help.txt`。与计划里"受管理的 hooks 仍可能运行"一致 |
| 审核会话用 `--safe-mode` 加只读工具范围，能否达到只读效果 | `未确认` | 计划列为实机验证项 |
| `--tools <tools...>` 限定可用的内置工具：`""` 停用全部，`default` 用默认集合，或列出工具名。可用来划定只读工具范围 | `快照` | `help.txt` |
| `--allowedTools`、`--disallowedTools` 选项存在 | `快照` | `help.txt` |
| `--permission-mode` 的取值：`acceptEdits`、`auto`、`bypassPermissions`、`manual`、`dontAsk`、`plan` | `快照` | `help.txt` |
| `--setting-sources <sources>` 只加载列出的设置来源，可选 `user`、`project`、`local` | `快照` | `help.txt` |
| `--disable-slash-commands` 停用全部 skill 和命令 | `快照` | `help.txt` |
| `--bare` 跳过 hooks、LSP、插件同步、自动记忆、钥匙串读取和 CLAUDE.md 自动发现。认证只认 `ANTHROPIC_API_KEY` 或 `apiKeyHelper`，不读 OAuth 和钥匙串。这与"复用已登录"冲突，本项目不用它 | `快照` | `help.txt` |
| 无头模式下，在 prompt 里写 `/skill-name` 可以调用用户可调用的 skill | `文档` | 官方文档 |
| 目录名为 `verify` 的项目或个人 skill 会在提交前被运行（2.1.286 起）。在 `-p` 会话里是否生效 | `未确认` | |

## 取消

| 事实 | 状态 | 出处 |
| --- | --- | --- |
| 发送 SIGINT 尝试结束当前轮次，宽限期后按需停止进程 | `文档` | 计划 |
| 取消以进程停止为依据，已有记录保留 | `文档` | 计划 |
| SIGINT 之后进程是否输出终态结果、退出码是多少 | `未确认` | |

## 计划列出的待验证项

配置是否生效、只读权限、取消、用量和模型报告。事件流录制暂缓。
