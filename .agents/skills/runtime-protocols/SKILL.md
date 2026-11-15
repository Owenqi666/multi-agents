---
name: runtime-protocols
description: Codex App Server（JSON-RPC）和 Claude Code 无头模式（claude -p）的协议事实、出处和本机验证状态。只要写或改 adapters/，或代码里出现两边 CLI 的选项、方法名、事件名、输出字段（如 turn/start、model/list、--effort、modelUsage），就先读它；升级 codex 或 claude 之后也用它刷新快照。不要凭记忆写协议。
---

# runtime-protocols

两边 CLI 更新很快，记忆里的选项名和字段名很可能属于旧版本。适配器里一个写错的字段名不会报错，只会让证据悄悄缺失。这个 skill 规定协议事实从哪里来、可信到什么程度。

## 协议里的名字要有出处

方法、参数、事件、字段、CLI 选项，写进代码前按下面的顺序查，查到为止：

1. **`protocol/` 快照**：Codex 的 JSON Schema（`protocol/codex/schema/`）和两边的帮助输出。它们由本机安装的版本生成，是最高依据。
2. **`references/` 事实表**：每条都标了出处和状态。
3. **官方文档**：链接在事实表开头。

配置文件的字段另外核对同版本官方 config schema：`protocol/codex/config.schema.json` 是 `rust-v0.169.0` 原始下载，来源、标签和 SHA256 在 `config-schema-source.json`。它不是本机 App Server 导出；结构可按官方源码使用，行为仍须实测。新增配置字段不能只因 `-c` 接受任意键就认定生效。

三处都查不到的名字不写进代码。在交付说明里把它列为"未确认"，由用户决定要不要实机验证。

`protocol/` 不存在，或其中的 `VERSION` 与本机 CLI 版本不一致时，先运行刷新脚本（见下文）再继续。

### 怎么查 Codex 的 schema

- 方法清单在四个文件里：`ClientRequest.json`（客户端请求）、`ClientNotification.json`（客户端通知）、`ServerNotification.json`（服务器通知）、`ServerRequest.json`（服务器发来、需要应答的请求）。
- 单个方法的结构在 `v2/` 下，按驼峰命名：`turn/start` 对应 `v2/TurnStartParams.json` 和 `v2/TurnStartResponse.json`，通知对应 `v2/…Notification.json`。`initialize` 在 `v1/`。
- 两个合并包（`codex_app_server_protocol*.schemas.json`）有几 MB。用 `jq` 按定义名查，不要整份读进上下文。

## 四种状态

| 状态 | 含义 |
| --- | --- |
| `已验证` | 本机实测过，写明日期、CLI 版本和证据 |
| `快照` | 本机版本的 schema 或帮助输出里有。名字和结构可信，行为没有验证 |
| `文档` | 官方文档或 `PROJECT_PLAN.md` 里有，快照里查不到或体现不出来 |
| `未确认` | 没有可靠出处，或出处互相矛盾 |

名字和结构（方法叫什么、有哪些字段）到 `快照` 就可以写进代码。行为（只读沙箱是否真的只读、取消后会收到什么）只有 `已验证` 才能当作事实，其余的在代码和交付说明里按"未确认"对待。

只有实测才能把一条改成 `已验证`：录制的事件流（放在 `tests/fixtures/runtime/<cli>-<版本>/`），或计划里的实测记录。读文档、读 schema 都不算。计划要求"缺少证据的字段标为未确认"，事实表自己也守这条规矩。

## 实机验证

2026-11-15 做过一轮实机验证。录制在 `tests/fixtures/runtime/`，结论写进了两张事实表，标 `已验证` 2026-11-15 的条目都指向具体的夹具文件。

再验证一次用探测工具：`python -m multi_agent.adapters.probe`，用法见 `src/multi_agent/adapters/probe/` 的模块文档。它启动真实的 CLI、发真实的请求，会消耗用户的额度，所以每次都要先得到用户的明确同意，并且由用户在自己的终端里运行。刷新脚本不在此列，它只调用版本、帮助和 schema 导出这类本地命令。

新录制入库之前要检查脱敏，新版本的录制放进新的 `<cli>-<版本>/` 目录。步骤和已知问题见 `tests/fixtures/runtime/README.md`。

## 适配器怎么写

来自 `PROJECT_PLAN.md`「调用层与运行记录」：

- 适配器是系统边界。解析失败、超时、限流、认证失效都在这里归入失败分类，不让畸形数据进入 `core/`。
- 保留原始事件类型。不认识的事件原样记录，不丢弃，也不猜它的含义。
- 终态结果和进程退出分别记录。取消以回合终态或进程停止为依据。
- 请求的模型和档位、运行时报告的值分开存。运行时没报告的字段记"未确认"。
- Codex 的协议类型从 `protocol/codex/schema/` 生成或逐项对照，不手写。

实机验证里和直觉不一样的几件事，细节和证据在事实表里：

- **Claude 的成败看结果行的 `is_error` 和 `terminal_reason`。** API 报错时 `subtype` 仍然是 `success`，取消之后退出码是 0。
- **Codex 的终态看 `turn/completed` 的 `turn.status`。** 模型不存在时，`thread/start` 和 `turn/start` 都照常返回，到回合里才失败。
- **事件流不是完整的操作记录。** Codex 回合里被沙箱拒绝的写入没有 item。只读靠沙箱和工具范围保证，不靠检查事件。
- **回显不是证据。** Codex 的 `thread/start` 和 Claude 的 `system/init` 都只回显请求的模型名。Claude 的实际模型看结果的 `modelUsage`，Codex 没有对应的报告。两边都不报告实际生效的档位。
- **启动 Claude 时去掉环境里的 `ANTHROPIC_API_KEY` 和 `ANTHROPIC_AUTH_TOKEN`。** 否则它不用订阅登录。
- **默认 Codex 会加载工作目录里的 `AGENTS.md`，并启动用户配置的 MCP 服务器。** T21 的审核隔离使用临时 HOME/CODEX_HOME、最小官方 ChatGPT 认证、固定配置和 named permissions；最终路线不设置 `forced_login_method`，另查实际 `account.type=chatgpt`，要求 `configRequirements/read.requirements` 严格为 null。T21 的最终 10 项无模型 canary 保留原绑定。2026-11-15 T22 在最终产品 `adbf4928b057769bc62b7b0a51c5a248e0e236b7` 上重新通过 14 项原生权限 case，并完成 codex-only/dual 各八例及取消、失败席补跑、交叉确认。全部 19 个新 Codex attempt 的 38 个预检/运行隔离 owner 有实际配置、来源、requirements、ChatGPT 登录及清理证据；模型线程顶层 `instructionSources=[]`，原始流未出现 MCP 启动。拒读结论仍来自权限 case，不能由空数组或未观察到读取单独推出。具体范围见事实表「T21 审核隔离」及「T22 修复后真实复验」。旧的宽泛 `read-only` 不能证明外部读取被禁止。

审核隔离还要区分两条边界：模型命令及文件工具的沙箱权限，与宿主加载配置、指令、技能和远端工具的能力。`instructionSources=[]` 只说明列出的文件指令为空，不证明全部上下文为空；`command/exec` 拒读也不证明具有客户端权限的 `fs/readFile` RPC 会拒读。原生系统运行时目录是 `:minimal` 的显式例外，不能把它描述为“只能读冻结目录”。

## 升级 CLI 之后

在仓库根目录运行：

```bash
.agents/skills/runtime-protocols/scripts/refresh.sh
```

然后：

1. 看 `git diff protocol/`。变化的方法、字段和选项，就是需要复查的适配器代码。
2. 按新快照核对事实表：`快照` 条目更新到新版本，受影响的 `已验证` 条目降回 `快照` 或 `文档`，直到重新验证（见「实机验证」）。
3. `protocol/` 的变化和事实表的修改放进同一次提交。

## 参考文件

- `references/codex-app-server.md`：Codex App Server 与 Codex CLI。写 Codex 适配器时读。
- `references/claude-headless.md`：Claude Code 无头模式。写 Claude 适配器或处理会话隔离时读。
