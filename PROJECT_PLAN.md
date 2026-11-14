# Multi-agent 本地开发工作台

> 2026 年 11 月 14 日 · 修订版计划

## 目标

做一个在 Mac 上运行的个人开发工作台，界面类似 Codex。一次任务可以由 Claude 和 GPT 共同制定计划，再交给指定模型分工实现，最后在同一个界面查看改动、测试和独立审核意见。用户可以为每个角色选择模型和推理强度，并查看实际使用的配置。

工作台由前端和本地调用层组成。调用层复用已登录的 Codex 和 Claude Code，负责跨模型派发、工作区管理、事件转发和运行记录。各自的运行时负责模型请求、会话和工具执行，并可在会话内部按需创建原生 subagent。首版以本机单用户、单仓库为范围，云端托管和团队协作留到后续评估。

自建部分聚焦在同版本盲审与整体评估、按仓库规则派发任务，以及把测试和审核证据绑定到具体提交。已有的「Claude Code + 官方 Codex 插件」作为现成工作流的比较基线。 [OpenAI Codex 插件](https://github.com/openai/codex-plugin-cc#codex-integration)

本地编排核心用 Python 和 SQLite，先提供可脚本调用的 CLI，跑通审核闭环后再加本地网页。桌面打包、可视化规则编辑和原生 subagent 的详细进度留到后续。最终接受或合并由用户触发。

## 模型分工与配置

下表是首版建议预设。Opus 和 GPT 规划会话共同制定任务，Sonnet 和 Terra 按项目规则领取实现任务。最终审核可选单模型或双模型，审核模型和推理强度由用户单独选择。

| 角色 | 建议默认模型 | 建议推理强度 | 职责 |
| --- | --- | --- | --- |
| 规划与 Claude 侧审核 | Claude Opus 5.5 `claude-opus-5-5` | `high` | 起草任务卡和验收条件，参与最终独立审核。 |
| GPT 侧计划复核与审核 | GPT‑6.1 Sol `gpt-6.1-sol` | `high` | 检查任务边界、依赖和遗漏，独立审核集成 diff。 |
| 常规实现 | Claude Sonnet 5.5 `claude-sonnet-5-5` | `medium` | 实现功能、编写相关测试并报告阻塞。 |
| 范围明确的实现 | GPT‑6.1 Terra `gpt-6.1-terra` | `medium`（以客户端支持为准） | 执行范围明确、验收条件清楚的小改动。 |

- **角色预设。** GPT 侧复核与审核可以显式切换到 `gpt-6-astra`。范围明确的实现保留 Terra 为默认，用同一组小型实现任务另行比较 Terra 和 GPT‑6 Luna `gpt-6-luna`，按实际通过的测试、改动范围、耗时和用量评估。
- **覆盖与备选。** 启动任务时可覆盖模型和档位，实现模型须符合项目派发规则。用户可为角色启用有序备选链，工作台在派发前解析并记录本次选择及原因。用户显式选定的审核模型不可用时提示重新选择。双审补跑沿用原席位的配置，更换模型则发起新审核轮次。
- **模型与档位。** Codex 通过 `model/list` 获取可见模型、支持档位和默认档位。`includeHidden: true` 可用于诊断，选择器默认展示可见项。Codex 官方文档在 `turn/start.params` 中给出了 `model`、`effort` 和 `sandboxPolicy` 的示例。Claude Code 通过 `--model` 和 `--effort` 选择配置，档位显式传入。Opus 5.5 和 Sonnet 5.5 的文档档位是 `low`、`medium`、`high`、`xhigh`、`max`。
- **配置证据。** 运行前检查客户端版本、登录状态及能获取的模型目录。记录请求的具体模型 ID 和档位、运行时报告的值和改派事件，缺少证据的字段标为未确认。预设调整用于后续运行，目录查询、连通性和实际配置分别记录。

参考 [Codex 模型目录](https://learn.chatgpt.com/docs/app-server#models)、[Codex 回合参数](https://learn.chatgpt.com/docs/app-server#turns)、[OpenAI 模型选择](https://learn.chatgpt.com/docs/model-selection) 和 [Claude Code 模型与推理档位](https://code.claude.com/docs/en/model-config#adjust-effort-level)。

## 完整工作流

1. **制定任务。** 用户输入需求和目标仓库。Opus 起草任务卡，Sol 或 Astra 对照原始需求复核遗漏、路径重叠、依赖和验收条件。用 JSON Schema 校验任务 ID、目标、基线提交、写入范围、依赖、接口契约、交付物、可验证的验收条件、命名测试引用和待确认问题。模型提出的新增测试命令作为建议，影响交付的未决问题在派发相关任务前交用户决定。执行者发现计划无法在原范围内完成时，记录原因并交回规划会话。
2. **分工实现。** 本地调用层按项目规则和任务依赖派发任务。有独立边界、写入范围互不重叠的任务可在各自 worktree 并行。需要共享接口时先确定契约，将契约改动放入本轮集成分支，并用该提交作为依赖任务的基线。涉及相同文件或有依赖的改动按顺序执行。
3. **集成并测试。** 按依赖顺序将通过交付校验的补丁收拢到集成工作区，中间按需执行受影响的检查。先固定最终候选提交，再从该提交执行项目配置的完整验收测试。记录基线与候选提交、diff、测试命令、退出码和日志，并在审核输入中突出测试文件及配置的改动。集成冲突保留在对应任务，测试失败可把同一候选提交和失败日志交给符合项目规则的执行者修复，或由用户决定后续处理。
4. **审核与整体评估。** 用户选择单审或双审。工作台将已固定的代码与测试证据组成审核快照。双审并行运行两个隔离会话，双方提交后再做整体评估。
5. **返工与验收。** 自动返工默认最多一轮，只处理已核对证据且处于原任务范围内的明确问题。相关测试失败或两位审核者确认可帮助定位问题。证据充分的单方阻塞意见也需处置，尚有分歧或范围不明时交用户判断。返工默认从当前集成提交建 worktree 并开启新尝试，传入原任务卡、相关 diff 和意见。修订后固定新提交并重新测试和审核。用户触发接受或合并时，核对审核对应的候选提交和目标分支基线。目标分支推进后，基于新基线生成候选版本并重新验证。

## 审核模式与版本冻结

| 模式 | 模型选择 | 运行方式 |
| --- | --- | --- |
| 单模型审核 | 自选一个可用模型和推理档位。 | 一个只读会话审核冻结版本，完成后展示意见和可获得的测试证据。 |
| 双模型审核 | 自选两个不同的可用模型，分别设置推理档位。建议预设为 Opus 5.5 与 GPT‑6.1 Sol，可将 Sol 换成 Astra。 | 两个独立的只读会话并行审核。各自提交完整结果后，工作台再做整体评估。 |

- **冻结审核输入。** 阶段 1 以本地仓库加基线、待审两个提交为正式入口。以仓库标识、两个提交、固定 diff、原始需求、验收条件和已有测试证据生成快照哈希。粘贴 diff 可用于快速试跑，缺少仓库或测试证据时明确标注，仅生成审核报告。审核轮次另行固定模型、档位和提示词模板版本，每位审核者的尝试单独编号。
- **隔离审核会话。** 每位审核者用全新的只读会话访问同一份冻结输入，按需读取固定提交的只读检出。两份首轮报告都提交后才交换意见。结构化报告记录严重度、基线或待审侧、文件和行号、原文、影响及依据。删除的代码可以引用基线侧。工作台对照冻结版本检查引用，无法核对的位置标为「待核实」。
- **整体评估。** 两份报告齐全后，工作台按文件、位置与问题类别归并候选重复项，保留原始意见和各自证据。单方发现默认保留，另一方未提到的问题标为「未确认」。仅对可能阻塞验收的问题做至多一轮交叉确认，记录同意、不同意或不确定及其理由。第三模型做语义去重或仲裁留到后续。
- **验收状态。** 汇总报告显示「可验收」「需要返工」或「待用户决定」。可验收要求对应提交的配置测试通过、所选审核报告齐全、阻塞项已处置。已核对且可修复的阻塞项进入返工，未解决的实质分歧或证据不足的问题交用户判断。
- **补跑与重新审核。** 一个会话失败、取消或超时，双审显示未完成，保留已成功的报告。用同一快照和席位配置启动新尝试，仅当前尝试的终态能推进审核。代码修订后生成新快照并重新测试和审核，旧结果保留在历史中。

## 项目规则文件

工作台从用户选定的基线提交读取 `.multi-agent/rules.yaml`，记录规则哈希。本次运行的派发、重试、返工和交付校验都使用这份规则。工作台从规则中解析命名测试命令，任务卡引用测试名称。示例中的命令和路径可按项目调整。

```yaml
version: 1
commands:
  frontend-unit:
    executable: pnpm
    args: ["--filter", frontend, test]
protected_paths:
  - ".multi-agent/**"
  - ".claude/**"
  - ".mcp.json"
implementation:
  - paths:
      - "frontend/**"
    provider: claude-code
    default_model: claude-sonnet-5-5
    default_effort: medium
```

- **派发前校验。** 对每条写入路径应用命中的规则，未命中时使用项目默认值。任务类型可附加限制。可用模型须同时满足各路径和任务类型的限制。同一路径的规则重叠时，兼容限制合并，冲突则提示调整规则。前后端任务要求不同提供方时拆卡，再按接口依赖派发。
- **执行与交付校验。** 实现会话在分配的 worktree 中运行，使用已验证的运行时权限控制写入。进入集成前，工作台独立检查实际变更路径，覆盖新增、未跟踪、删除以及重命名前后的路径，拒收越界或触及受保护路径的补丁。受保护配置由用户明确采纳后才用于新运行。
- **测试命令。** 工作台执行冻结规则中用户认可的命令，以程序和参数的形式启动。项目需要补充命令时先由用户选定。测试可能执行候选代码和项目脚本，应在独立测试检出中运行，并限制测试进程的权限和可访问凭据。
- **审核模型单独选择。** 前端实现规则只约束写代码和返工，单审可选 GPT 或 Claude，双审可由两边独立复核 Claude 生成的前端改动。

## 调用层与运行记录

- **本地接入。** Codex 优先使用 App Server 的 stdio JSON-RPC，Claude 优先使用 `claude -p` 的 JSON 或流式输出。继续用官方 CLI 的登录。项目可沿用 Paper-to-Alpha 的子进程管理、结构化输出校验和调用记录，为编码会话单独设置读写权限和事件处理。 [Paper-to-Alpha 模型工作流](https://github.com/Owenqi666/quant-ai-agent/blob/5c53bb63c76710bab4576ce2b02390e7ca4fc4c3/docs/model-workflow.md) · [Claude Code 自动化](https://code.claude.com/docs/en/headless)
- **统一接口与事件。** 先定义环境检查、启动会话、提交一轮、读取事件、取消和取结果。适配器统一输出会话开始、消息、用量、完成和错误，工具及文件事件按阶段接入。保留原始事件类型，分别记录终态结果与进程退出。阶段 0 用合成事件验证编排逻辑。
- **会话与权限。** 为规划、实现、审核建立可单独控制的会话。审核采用已验证的只读配置并隔离上下文和日志。Codex 文档给出的只读配置为 `turn/start.params.sandboxPolicy.type` 设为 `readOnly`。Claude Code 的 `--safe-mode` 保留官方登录并关闭常规记忆、hooks 和 MCP，受管理的 hooks 仍可能运行。将该模式和只读工具范围列为后续实机验证项，检出前后的 Git 状态用于辅助检查。原生 subagent 可用于会话内局部任务，跨厂商交接由工作台管理。 [Codex 沙箱](https://learn.chatgpt.com/docs/app-server#sandbox-read-access-readonlyaccess) · [Claude CLI 参数](https://code.claude.com/docs/en/cli-reference)
- **持久化与重试。** SQLite 记录任务、阶段、尝试、会话 ID、时间、CLI 版本、模型、提示词模板版本、规则哈希、结果和失败原因。审核记录关联快照、审核轮次、席位和尝试。终态写入时检查该尝试是否仍为当前尝试，晚到结果只进入历史。重启后展示可恢复的阶段，重新派发前检查上次尝试和工作区结果。
- **失败与取消。** 区分认证失效、限流、模型不可用、超时、取消、越界补丁、测试失败和集成冲突。限流记录可重试时间（如有）。Codex 使用 `turn/interrupt` 并等待对应回合终态，Claude 使用 SIGINT 尝试结束当前轮次，宽限期后按需停止进程。取消以回合终态或进程停止为依据，保留已有记录。 [Codex 取消](https://learn.chatgpt.com/docs/app-server#interrupt-a-turn) · [Claude 非交互运行](https://code.claude.com/docs/en/headless)
- **用量与证据。** 设置每家 CLI 的并发、时长和调用次数上限，展示运行时可获取的用量。Codex 文档列出 `model/rerouted` 和 `thread/tokenUsage/updated`，分别记录可观察到的改派和用量。Claude 可从结果中的 `modelUsage` 核对报告过的模型，`total_cost_usd` 按客户端估算显示。后续实测验证这些事件在本机的表现。日志保存在仓库外并与运行关联，测试结果、模型说明及审核状态各有可追溯的来源。 [Codex 事件](https://learn.chatgpt.com/docs/app-server#events) · [Claude 模型报告](https://code.claude.com/docs/en/model-config) · [Claude 用量字段](https://code.claude.com/docs/en/agent-sdk/typescript)
- **界面。** 主视图显示原始需求、任务卡、各阶段状态与模型配置。点开实现任务可查看其 worktree、diff 和测试输出。点开审核意见可定位到对应 diff，并将反馈和代码上下文放入返工的交接包。

## 开源参考

| 项目 | 借鉴内容 |
| --- | --- |
| Orchevia · Apache-2.0 | 本地 CLI 统一接入、节点级模型和权限、测试退出码、超时、审批和有限重试。首版可借流程节点定义和运行视图。 [工作流定义](https://github.com/iazaran/orchevia/blob/main/docs/workflow-schema.md) |
| Codex Paired Superpowers · MIT | 模型按阶段配置，审核结论关联具体提交。其多审核席位和双作者模式存在组合限制，完整四角色流程需要在本项目里验证。 [模型配置与限制](https://github.com/mkritter3/codex-paired-superpowers/blob/main/README.md#L350-L393) |
| Duo · MIT | 先独立回答，再逐条讨论，保留异议。内置 Pair 是一位执行者和一位审核者，适合对照首个闭环。 [讨论实现](https://github.com/Audatic07/duo/blob/main/src/protocols/debate.ts) · [Pair 实现](https://github.com/Audatic07/duo/blob/main/src/protocols/pair.ts#L135-L140) |
| Paradev · MIT | 每任务一个 worktree，审核意见定位到 diff 并回传给 agent。 [审核与反馈](https://github.com/ParadevAI/Paradev#review-and-revise) |
| Orca · MIT | 实验性编排中以运行、任务和尝试区分状态，完成消息关联当前尝试，避免旧结果覆盖重试结果。 [编排文档](https://github.com/stablyai/orca/blob/main/docs/site/content/docs/cli/orchestration.mdx) |
| GitHub Spec Kit · MIT | 需求、计划和任务作为独立产物。任务模板标出依赖、并行条件和可独立验证的验收点。 [任务模板](https://github.com/github/spec-kit/blob/main/templates/tasks-template.md) |
| OpenAI Codex plugin for Claude Code · Apache-2.0 | 对照官方插件如何通过本机 Codex App Server 和现有登录实现只读审核、后台委派、状态和取消。 [Codex 集成](https://github.com/openai/codex-plugin-cc#codex-integration) |

## 实施顺序与验收

| 阶段 | 交付内容 | 验收条件 |
| --- | --- | --- |
| 0. 编排核心与接入验证 | 先定义最小数据模型、状态机与两个适配器的统一接口，用合成事件做离线回放。实机验证和事件流录制列为暂缓。 | 离线验证两个席位并行、同一快照补跑、旧尝试迟到及取消。实机联调恢复后，先验证阶段 1 所选审核会话的模型目录、档位、只读权限、终态和可观察到的实际模型。 |
| 1. 可选单审或双审 | 先提供 CLI。本地仓库加两个提交为正式入口，粘贴 diff 用于快速试跑。提供冻结快照、结构化报告、引用核对、双审汇总和小型审核基准。 | 单审只启动所选会话。双审对同一快照并行盲审，两份报告齐全才整体评估。补跑只重开失败会话，旧结果只进入历史。代码更新后，旧报告仍关联旧快照。无测试证据的输入显示对应状态。 |
| 2. 规划到实现 | 加入任务卡、项目规则、worktree、单执行者实现和真实测试。 | Claude 起草、GPT 复核，执行者按冻结规则派发。验证前端实现及返工交给 Claude，越界和更改受保护路径的补丁被拒收，GPT 仍可审核前端。测试与审核对应同一提交，并覆盖一轮自动返工及修订后的重新验证。 |
| 3. 并行实现与集成 | 执行者按规则并行处理独立任务，按依赖集成，集成版本按所选模式审核。 | 在独立 worktree 产出补丁，最终候选提交固定后跑完整测试。接受时核对候选与目标基线，目标分支推进时重新验证。超时、取消和重试可追溯。 |

阶段 1 先用 6–10 个固定样本做审核基准，覆盖已知缺陷、无缺陷和可能由单方发现的问题。相同输入分别用两种单审、双审和「Claude Code + 官方 Codex 插件」跑，记录去重后的有效发现、漏报、误报、耗时及可获得的用量。保留部分样本用于独立验证提示词修改。结果用于调整推荐的审核模式和后续投入，用户可以继续选择单审或双审。

## 本机接入待验证项

2026 年 11 月 13 日记录了两边 CLI 的升级、登录和短任务连通性测试。下表列出请求的模型和档位及当时的回包。运行时实际使用的模型和生效档位以可观察证据为准。

| CLI | 版本 | 短任务请求的模型与档位 | 验证方式 |
| --- | --- | --- | --- |
| Claude Code | **2.1.325** | Sonnet 5.5 `medium`、Opus 5.5 `high` | `claude -p` 返回 OK |
| Codex CLI | **0.169.0** | GPT‑6.1 Sol `high`、GPT‑6.1 Terra `medium`、GPT‑6 Astra `high`、GPT‑6 Luna `medium` | `codex exec`（只读沙箱）返回 OK |

- Claude Code **2.1.325** 满足两个模型的版本要求。Opus 5.5 需 **2.1.280 或以上**，Sonnet 5.5 需 **2.1.284 或以上**。 [Claude Code 版本要求](https://code.claude.com/docs/en/model-config#model-aliases)
- Codex 升级前使用 0.145.0，模型目录里没有 GPT‑6.x。调用 Sol 和 Terra 返回「ChatGPT 账号不支持该模型」，Astra 返回「需要更新版本的 Codex」。升级到 0.169.0 后，上表的短任务均收到回包，`codex debug models` 列出 `gpt-6-astra`、`gpt-6-luna`、`gpt-6-sol`、`gpt-6.1-sol`、`gpt-6.1-terra`。本机 Codex 由官方安装脚本安装，升级命令为 `curl -fsSL https://chatgpt.com/codex/install.sh | sh`。
- 官方文档明确列出 GPT‑6.1 Terra 需要 Codex CLI **0.159.0 或以上**，本次记录的 **0.169.0** 满足该要求。 [Codex CLI 模型要求](https://learn.chatgpt.com/docs/models#choose-a-model)
- Luna `medium` 的一次短任务记录了 9,024 token。后续基准单独记录每次用量及统计口径。
- **暂缓实机验证。** 官方文档给出了 `model/list`、`turn/start.params.effort`、`sandboxPolicy` 和相关事件。待验证的是本机的目录返回、配置生效、只读权限、取消、用量及模型报告。App Server 检查和两边实际事件流录制继续暂缓。四模型流程尚待实现和联调。 [Codex 模型目录](https://learn.chatgpt.com/docs/app-server#models) · [Codex 回合参数](https://learn.chatgpt.com/docs/app-server#turns)

## 已完成任务

### 2026 年 11 月 13 日

- **规划审阅。** 完成对本规划的审阅，结论和建议见 [Multi-agent 工作台规划审阅](https://claude.ai/artifact/FiEuhpf3oMxbnJHCpRNUFS)。
- **CLI 升级和短任务测试。** Claude Code 从 2.1.174 升到 **2.1.325**，Codex CLI 从 0.145.0 升到 **0.169.0**。短任务的请求参数和回包记录在「本机接入待验证项」。

### 2026 年 11 月 14 日

- **更新项目计划。** 将采纳的审阅建议和六项决定并入正文，修正 Codex 参数及 Terra 最低版本要求。App Server 实机检查和两边事件流录制继续暂缓。
