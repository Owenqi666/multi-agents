# 阶段 1：Claude Code + 官方 Codex 插件对照

本文记录 T-0025 的真实执行、输入绑定与转换证据，不判定 finding 的正确性或比较模式优劣。八例均完成唯一 `attempt-001` 首轮，执行结果为 8/8 success；八份结构有效的转换报告已封包，一次完整 verify 已通过。文档提交 SHA 与本轮总证据封存由最终交付回报绑定。

本轮经用户授权，以会话级 `--plugin-dir` 加载官方插件；不永久安装、不修改用户设置、不升级 CLI、不修改产品或官方插件源码。质量评审交由独立窗口按最新授权执行 `agent_reviewed`，替代此前必须真人填写的安排；本执行窗口不读取 T24、oracle 或旧质量结论，也不自行填写质量标签。

## 固定身份与证据入口

| 项目 | 绑定 |
| --- | --- |
| 任务卡 | [T-0025](/Users/owen/Desktop/Multi-agent/tasks/T-0025-official-plugin-comparison.json) |
| 任务开工 HEAD | `2d77ab161157297a0a73461b4e64b4ca563a0b24` |
| 固定产品 SHA | `adbf4928b057769bc62b7b0a51c5a248e0e236b7` |
| 八例执行时文档 HEAD | `4328ed51314aef88094568f2c9fe4aa5fdaec780`；与固定产品 SHA 分开记录 |
| 官方来源 | `https://github.com/openai/codex-plugin-cc`，提交 `db52e28f4d9ded852ab3942cea316258ae4ef346`，插件 `1.0.6` |
| 实际通道 | `official-plugin-task` |
| 本机版本依据 | Claude Code `2.1.325`；Codex `0.169.0`；源码、快照与实测证据分别记载 |
| 外部证据根 | `/Users/owen/Desktop/Multi-agent-acceptance/T-0025/`，下文简称 `ROOT` |

任务开始的校验、产品 SHA 和零模型请求状态见 [start.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/start.json)。[官方下载记录](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/official-source-fetch.json)保存 clone/checkout 的 argv 和退出码；[源码审计](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/plugin-source-audit.json)绑定 63 个官方跟踪文件的 SHA256、20 项行号结论与三份本机协议快照。源码结论本身不是运行成功证据。

## 实际调用链与模型证据

实际链路是：外层 Claude → 唯一固定 MCP 工具 `mcp__t25_gate__official_codex_task({})` → 原版 `codex-companion.mjs task` → 原版 broker/AppServer 客户端 → 本机 Codex。外层不独立审核或添加 finding。MCP gate 固定程序、目录、prompt、模型和参数，不接受模型提供的任意命令、路径或 `--write`。

```text
[node, <official>/plugins/codex/scripts/codex-companion.mjs,
 task, --cwd, <owned frozen workspace>, --fresh,
 --model, gpt-6.1-sol, --effort, high,
 --prompt-file, <sealed full prompt>, --json]
```

`--write` 未传入；官方 task 没有 `--read-only` 选项。task 默认前台等待。实际 `thread/start` 请求包含 `approvalPolicy=never`、`sandbox=read-only`、`ephemeral=false`，随后 `turn/start` 发送文本输入。这里没有调用 `review/start`，也没有调用官方 rescue 的 Sonnet 子代理；不能将本轮称为 native review 或原 rescue Agent 路线。

外部 `codex_recorder.py` 只在原版插件启动 Codex 的 argv/stdio 处记录并转发字节，不生成请求、不改写 RPC、不替代官方插件客户端。证据副本仅遮盖已注册凭据字面值；传输内容保持原样。各例 `harness-binding.json` 绑定实际脚本版本，`codex-processes/*/events.jsonl` 保留 send/recv、spawn、exit 和 drain 结果。

| 层 | 请求 | 可报告的事实 | 仍未确认 |
| --- | --- | --- | --- |
| 外层 Claude | `claude-opus-5-5` / `high` | 八例 `modelUsage` 报告 `claude-opus-5-5`，保留各例用量与 `costUSD` | 实际生效 effort |
| 内层 Codex | `gpt-6.1-sol` / `high` | 保留各例累计 token usage；`thread/start` 回显请求模型 | 实际模型、实际 effort、实际费用 |

Codex 的累计用量取同一 turn 最后一次累计 total，不累加多条累计通知。外层 helper 等待包含内层执行，不能把两个墙钟相加当总时长；外层 Claude 的费用字段也不是整个插件链路的总费用。保留真正的零值，缺失项为未确认。

## 冻结输入、提示词与输出结构

本轮沿用 T19/T22 的八个冻结 snapshot，没有重新 prepare。每例从封存的 T19 `review-inputs/Bxx` 复制五份文件至新的私有 owner：`review-input.json`、`diff.patch`、`source/snapshot.json` 及两侧 `case.py`。目录内容与字节哈希在执行前后核对；manifest、审计映射、其他报告和 oracle 不进入模型工作目录。

[冻结输入审计](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/frozen-inputs.json)核对八例 40 个文件及两份 manifest/prompt，共 44 项授权输入文件，均匹配原封存。另有独立[旧证据保全回执](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/old-evidence-preservation.json)：T19 为 389/389、T22 为 364/364，文件大小、SHA256、读取期间稳定性及 manifest 前后 hash 全部匹配。保全过程仅将旧文件字节用于哈希，没有解释报告内容、读取 T24/oracle 或写入旧根目录。

| 内容 | SHA256 |
| --- | --- |
| 原共享 `review-v1` prompt | `8af7a9e4bca3d1451e784355c9f5adf9146d00fbd15380bfb00ed0422cc30135` |
| 实际内层 prompt（共享文本 + 固定插件说明 + JSON schema） | `b3eb8489b993e1594425e9ce980fda842ad7a32225fc210e0b90770d7fdbf500` |
| 原 manifest | `a4da5df0fb2209dfa6e97cfac4733dfbbe75618e904d98054fb01b0dbf1c91f9` |

完整 prompt 在发送前按官方 helper 的 `.trim()` 行为处理，实际 turn 输入与完整 prompt hash 分开核验；原共享 hash 没有被冒用为实际输入 hash。schema 在可见 prompt 中提供，真实 RPC 的 `outputSchema=null`；`--json` 只是 helper 的输出包装。事后结构校验与原生 RPC outputSchema 约束不同，因此不能直接宣称本轮与原生模式字节及协议完全等价，也不能用旧 prompt hash 强行导入原 benchmark 格式。

## 外层工具限制与内层权限差异

[外层 gate 检查](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/outer-gate-negative-tests.json)共 25 项通过，包含 `--write`、任意命令、额外参数、外部 cwd、修改模型/prompt、未知工具等拒绝负例，也包含重复调用不再 spawn、prompt 改变拒绝、官方 helper 固定、符号链接/越界封存拒绝和超时未知状态检查。检查在正式模型请求前执行，不能将其中的模拟 helper 记为真实模型调用。

外层使用 `--tools ""`、唯一 `--strict-mcp-config`、受控 `--allowedTools`、`--restricted`、显式临时 settings 与 `--plugin-dir`；没有使用会禁用插件的 `--safe-mode`。B01 实际 init 列出的工具只有固定 MCP gate，实际仅调用一次且参数为 `{}`；init 同时报告 `codex@inline`、版本 `1.0.6` 与固定官方目录。这证明插件实际加载和路由发生，不只依据启动 argv 推断。

[标准只读 canary](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/native-readonly-canary/summary.json)没有发模型请求，实测三项：

| 合成边界 | 实测 |
| --- | --- |
| 读取冻结目录 | 允许，exit 0 |
| 读取外部合成哨兵 | 允许，exit 0；作为官方 baseline 差异保留 |
| 写入冻结目录 | 拒绝，exit 1、Operation not permitted |

官方标准 `readOnly` 的读取范围宽于 T21/T22 named permissions；本轮不继承 T22 的 14 项隔离通过结论。B01 线程报告 `sandbox.type=readOnly`、`networkAccess=false`、`activePermissionProfile=null`、`instructionSources=[]`。空 instructionSources 只覆盖所报告的文件指令列表，不证明所有上下文为空；事件流也不是完整文件访问清单，不能把没有记录到访问解释成没有读取。

Claude 实际仍列出 builtin plugins 和 skills；`--restricted` 与临时设置不证明 managed policy 或所有隐式上下文均被排除。外层受控 MCP 与内层标准只读是不同边界。已观测到的命令、来源和限制逐例保留，未知项不填成隔离成功。

## 认证、生命周期与先封存后清理

每例使用独立临时 `HOME`、`CODEX_HOME`、`CLAUDE_PLUGIN_DATA` 和 fresh session；清除继承的认证覆盖、broker endpoint、旧 session/transcript 等变量。Codex 只复制必要官方登录字段，Claude 从既有 Keychain 只读取得并仅复制 `claudeAiOauth` 到本例私有 fallback 文件，权限 0600、owner 0700；凭据值不写入 argv 或报告。不复制用户记忆、项目状态或整份全局配置，不改原 Keychain 或永久用户设置。

空临时 HOME 的首次 [认证预检](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/claude-auth-preflight.json)实际为 exit 1、`loggedIn=false`、`authMethod=none`，模型请求为零，临时目录已删除。只复制账号 metadata 也不足以认证。之后 [最小官方凭据副本预检](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/claude-auth-minimal-copy.json)报告 exit 0、`loggedIn=true`、`authMethod=claude.ai`；此失败与修复过程均保留，没有覆盖为首次成功。

原版插件会 detached broker，并在 SessionEnd 删除 job/log。正式 harness 在 helper 返回、MCP 工具回复前保存原 job/log、broker metadata/log，再由父控制器在手动 teardown 前补抓一份。先验证捕获回执及 owned 进程身份，再调用原版 SessionEnd；退出未知时保留私有目录和原始日志。真实 terminal、helper exit、AppServer exit、wrapper drain 和 owned PID 消失分别取证，不能互相替代。进程清理证据覆盖已记录并核对身份的进程；它不能排除未被观察到的逸出后代。

[生命周期检查](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/harness-lifecycle-check.json)包含 9 项，最终均通过：未知退出保留 owner、封存先于 teardown、五项越界/符号链接拒绝，以及下游 write/flush 断开后继续 drain 和记录。此前 [initial/001 失败](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/harness-lifecycle-check-001.json)包括测试替身缺少 PIPE 和 plugin-data 根符号链接漏洞；[002](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/harness-lifecycle-check-002.json)因检查期间脚本 hash 改变仍记失败，稳定重跑 [003](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/harness-lifecycle-check-003.json)才记通过。失败记录和脚本版本均保留，修复仅在外部 harness，不修改产品、仓库测试或官方插件。

早期 canary 的 secret 注册曾过宽；[模型前修订记录](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/harness-pre-model-changes.json)说明后续只注册凭据 token，保留原 canary recorder 的 hash，正式 benchmark 未使用该早期版本。生命周期合成回执绑定的是当时脚本版本；B01 又绑定真实运行版本并独立复核生命周期，不能将旧脚本的通过结果无条件移给后续版本。

## 八例唯一首轮矩阵

各例唯一首轮均为 `attempt-001`。条目数仅统计模型原文 finding，不代表有效发现；B06 的 0 仅表示实际返回空 findings，不代表已证明无缺陷。

| Case | 执行状态 | 原始 finding 条目数 | 外层墙钟 / helper 墙钟（秒） | 来源 |
| --- | --- | --- | --- | --- |
| B01 | success | 1 | 23.19 / 15.44 | [completion](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/cases/B01/attempt-001/completion.json) |
| B02 | success | 1 | 23.41 / 15.26 | [completion](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/cases/B02/attempt-001/completion.json) |
| B03 | success | 1 | 24.29 / 15.55 | [completion](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/cases/B03/attempt-001/completion.json) |
| B04 | success | 1 | 25.11 / 15.56 | [completion](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/cases/B04/attempt-001/completion.json) |
| B05 | success | 1 | 40.15 / 31.39 | [completion](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/cases/B05/attempt-001/completion.json) |
| B06 | success | 0 | 21.40 / 14.80 | [completion](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/cases/B06/attempt-001/completion.json) |
| B07 | success | 1 | 22.45 / 15.27 | [completion](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/cases/B07/attempt-001/completion.json) |
| B08 | success | 1 | 25.00 / 16.61 | [completion](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/cases/B08/attempt-001/completion.json) |

共八个内层 Codex turn、八个外层 Claude 会话。每例 recorder 记录五个 Codex 进程，包含无模型的 version/help 检查与一个 AppServer；总计 40 个进程记录不等于 40 次模型审核。各例输入前后相同、helper exit 0、capture complete，已记录子进程退出且 owner 删除。原始流、时间与请求绑定位于各例目录，完整索引见[最终运行审计](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/runtime-audit-final/runtime-audit.json)。

原独立审计对 B01/B02 判 pass，对 B03–B08 的保守命令解析判 `manual_review_required`，整体原文件仍为 `review_required`，没有改成全绿。根代理随后逐条核对真实命令和 cwd：`rg | head`、negative glob、`od`、`sed -n l`、分号及 `&&` 仅用于列举或读取冻结文件。[补充命令复核](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/runtime-audit-final/recorded-command-review.json)绑定原审计 hash，关闭这六例的解析疑点；合并运行结论为八例通过。这只覆盖已记录命令的语义，不是完整访问审计或 finding 质量判断。

### B01 冒烟的可定位证据

B01 成功冒烟就是正式首轮，未另跑替换。其 snapshot 为 `cf06ffc3449e37fc7c102b675e8d38da5176ceea379ce7e71ee3464b96a1a3fe`，Codex thread 为 `01a1e19c-ba7a-76d3-b3bb-e6afbebd2d0f`，turn 为 `01a1e19c-bc3d-72e1-be06-82106895a9ce`。[独立运行审计](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/B01-runtime-audit.json)定位到真实 `turn/completed.status=completed`，并核对 helper 原始输出与 final_answer 完全一致。外层结果 `is_error=false`、`terminal_reason=completed`，独立进程 exit 0。

B01 的 gate 封存在 `2026-11-15T23:48:42.290372Z`，早于外层完成 `23:48:46.624818Z`；捕获的六个文件 hash 均匹配。五次 Codex 子进程记录包括可用性检查和实际 AppServer，均有 exit/drain，不代表五次模型审核。独立复核时记录的 25 个 PID 均已消失，owner 已删除，冻结输入前后和原源输入一致。外层墙钟约 23.19 秒，helper 墙钟约 15.44 秒；二者重叠。

B02–B08 均使用新 owner 和同一固定通道，未通过补跑替换首轮。协议约束仍是：一旦发生模型请求，该次始终保留为首轮成功或失败；诊断重试不能替换首轮，连续两次系统性失败时停止受影响运行。格式失败也必须按失败保留，不能记为零发现。

## 转换交接与验收边界

交接包已生成于 `ROOT/handoff/plugin-001/`：[index.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/handoff/plugin-001/index.json)逐例包含 `case_id`、`snapshot_id`、真实状态、通道、共享/实际/外层 prompt hash、原始输出 hash、规范化 report hash及来源路径。每例保留 `inner-raw-output.txt`、原 report 的字节副本 `original-report.json`、转换记录 `report.json`、冻结输入与运行证据。

新收集记录的 `source=collection_runtime`，不把执行记录冒作质量裁定。转换按原文顺序分配稳定 finding ID，并绑定原始文件 hash 与行号/字节区间；只转换结构，不增加原文没有的 finding，不改写实质结论。report hash 使用现有 UTF-8 canonical JSON 口径，不含引用核对的附加字段。八份唯一首轮报告均通过转换结构校验，质量判断数为 0。

[manifest.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/handoff/plugin-001/manifest.json)列出 264 个 payload 文件；加 manifest 自身共 265 个文件。其 SHA256 为 `534415e6b61a406dc01bf6e0161992228011e9f43e69154a5261da76f46ca40d`。[封包回执](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/preparation/handoff-summary.json)记录所有 payload 大小/hash 已核验、253 个复制来源未改变、原 case 输出未修改，并保留每例 raw/converted 双 hash。该包已交原窗口转送独立评审；本执行窗口没有向评审提供质量标签。

报告 schema 或 quote/reference 校验通过，只说明结构与引用绑定可核对，不是有效发现、漏报或误报的质量裁定。本轮执行与转换证据交给原窗口，再由独立 `agent_reviewed` 流程评审；本文件不预填质量标签、不选优胜模式。

| 任务卡验收项 | 当前结果 |
| --- | --- |
| 1. 官方来源、加载与 helper 路由 | 八例真实加载、固定 helper 与内层请求已核验 |
| 2. 八例冻结绑定、prompt 差异、首轮身份 | 44 项输入准备审计通过；八例唯一首轮、双 prompt hash 和前后输入绑定保留 |
| 3. 工具控制、只读与边界 | 25 项 gate 检查、3 项无模型 canary、独立审计及六例命令补充复核；不继承 T22 隔离 |
| 4. 八例状态、原始输出、报告值与转换 | 8/8 success，八份转换结构有效；原文、报告值、稳定 finding 映射及双 hash 已封包 |
| 5. 先封存与 owned 资源清理 | 9 项生命周期检查；八例 capture complete、退出/drain 与 owner 删除有记录 |
| 6. 交接及旧证据不变 | T19 389/389、T22 364/364 保全通过；plugin-001 共 265 个文件已交原窗口；本轮总封存由交付回报绑定 |
| 7. 完整 verify、独立文档提交与清单 | 一次完整 verify 五步通过；文档提交与最终清单由交付回报绑定 |

## 最终检查与封存绑定

根代理在协调好的独占检查时段执行了一次 `scripts/verify.sh`，没有失败后反复重试取绿。[command.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/checks/final-verify/command.json)记录 UTC `2026-11-15T23:59:22.848558Z` 至 `2026-11-16T00:00:22.827479Z`、墙钟约 59.97 秒、exit 0；[stdout](/Users/owen/Desktop/Multi-agent-acceptance/T-0025/checks/final-verify/stdout.txt)记录 lint 通过、87 个文件格式通过、33 个源文件 typecheck 通过、702 个 unit 通过（59.45 秒）及 16 个 replay 通过（0.13 秒），stderr 为空。

该结果只证明这次完整检查通过。T25 未修改产品或仓库测试；此前规划窗口观察到的 0.4 秒启动超时问题由 T26 另卡处理，本轮不能宣称已修复该问题。

最终文档 SHA、证据 manifest 文件数及其自身 hash 由交付回报绑定。这里不预填尚未生成的封存值，也不据本次验收自行切换项目阶段。
