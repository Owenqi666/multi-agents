# 阶段 2 开发路线

用户于 2026-11-16 明确指示：“新开一个阶段2窗口开始按开发顺序继续执行”。本路线从已验收的阶段 1 开始，逐卡开发到单执行者的规划、实现、测试、审核、一次返工闭环。开发 checkout 为 `/Users/owen/Desktop/Multi-agent`，启动 HEAD `f6729e501b7b1db789d3eefdef4c41d0b1646115`。每卡正常独立提交；最终接受/合并仍由用户触发，不自动进入阶段 3。

## 当前交付顺序（2026-11-17）

用户调整为：“收尾 T49 后，优先打通‘候选提交→执行测试→展示结果’，再接审核和一轮返工。保留现有检查，减少碎片化拆卡和过度防御，后续按实际可用功能和整体完成百分比汇报进度。”

T49 已完成测试尝试、命名命令和实际结果的事务记录。T50 已将独立检出、受限命令执行、日志证据和 `task-test run/show` 作为一个完整离线功能接通。T51 新增 `task-review run/show/retry/confirm`，接通同候选实际测试、单/双审、失败席补跑、交叉确认与当前资格展示。T52 补齐 `task plan/run/show/rework`，从需求开始，经规划、实现、固定候选、测试、审核和一次原范围返工，再对新候选执行原完整测试与新审核；实际模块 CLI 全链和 50 项新 unit、7 项新 replay 已通过。完整检查和正常提交回执见各交付记录。仍然只有一个产品实现执行者，不进入阶段 3，不自动接受或合并。

整体进度按可用操作和最终验收估算。T49 完成时约 60%（55–65%）；T50 跑通固定候选 → build → test → 重开 show 后约 70%；T51 同候选审核接通后约 80%；T52 完整分步 CLI 流程、一次返工、新证据隔离和明确重试/取消通过离线验证后约 90%。T54 通过最后零模型 canary 后曾估为 95%、仅待接收；主窗口核对项目书发现自动衔接仍缺失，修正为约 90%。T55 已按用户指令用一张功能卡补齐审核后的自动一次返工、原完整复测与原配置复审，新增 26 项回归，完整检查 3348 unit、68 replay 通过；默认正常审核入口的真实单次模块 CLI 回归已证明自动衔接。交付见 [T55](deliveries/T-0055.md)，最终接收仍交主窗口验收，不自动接受、合并或进入阶段 3。

四项固定 S9 检查保持各自证据范围：前三项绑定 T52，最后一项以 T54 源码哈希和提交回执绑定；T55 不重跑模型或把旧报告改称新提交重跑。开发功能、离线检查和原生权限证据分别报告，固定 canary 不推广为任意路径或完整模型任务的保证。现有 lint、格式、类型、unit、replay 与提交 hook 全部保留。

## 启动时已有能力与缺口（历史）

| 能力 | 已有事实 | 本阶段缺口 |
| --- | --- | --- |
| 任务卡 | `schemas/task-card.schema.json`、`.agents/skills/task-card/scripts/validate_task_cards.py` 是开发期校验 | 产品尚无 loader/任务图/规则模块；开发脚本固定检查本仓库 HEAD，不能直接当产品入口 |
| 冻结输入 | `cli/snapshot.py` 从 Git 对象生成不可变审核输入；`core/snapshot.py:FrozenSnapshot` 有内容 hash | `.multi-agent/rules.yaml` 未读取，未冻结到任务运行 |
| 调用 | `adapters/base.py:Permission.WORKSPACE_WRITE` 只是枚举；`RuntimeAdapter.start_session` 明确拒绝非 READ_ONLY | 编码会话和对应写权限验证均未实现 |
| 状态与存储 | `core/state.py`、`ReviewStore` 和迁移 0001/0002 保存审核 rounds/seats/attempts/history/snapshots/round_inputs/executions | 无产品任务状态、规则哈希、实现尝试、工作树、候选及测试执行关联表；审核 attempts 不能冒充任务尝试 |
| 工作区 | 审核器物化冻结文件、核对内容 | 没有单执行者实现 worktree、完整交付路径判定 |
| 测试 | `core/snapshot.py:TestEvidence` 是输入的 commit/command/exit_code/log 记录 | 没有真实测试执行器、独立测试检出及来源证明 |
| 审核 | 单审/双审、失败席补跑、一轮交叉确认及旧结果隔离已验收 | 无实现候选到测试/审核的编排；交叉确认不等于自动返工 |
| 失败 | `core/events.py` 与迁移 0001 已声明 out_of_scope_patch/tests_failed/integration_conflict | 缺少实现/测试阶段产出这些失败的逻辑；新增状态或分类仍先走 state-change |

阶段 1 705 unit/16 replay 是启动基准。历史封存、B05 正负零合同解释分歧、`agent_reviewed` 来源、实际模型/effort 的未知和官方插件差异保留，不重写、不重跑以替代阶段 2 证据。

## 依赖顺序与卡片边界

以下表格保留阶段 2 启动时的依赖规划。实际交付已推进到 T52；从 T50 起按上面的用户操作顺序合并完整功能卡。S2–S9 是能力标识，不单独授权全仓写入，也不要求再按输入、状态、上下文、检出碎片化拆卡。

| 顺序 | 最小交付 | 前置与接口先行点 | 命名验证规划 |
| --- | --- | --- | --- |
| T28 | 任务卡、基线规则读取和不可变契约 | 唯一 schema、严格 YAML、命名 argv、规则原文/blob/hash、任务图 | `test_rules_are_read_from_baseline_not_worktree`、`test_product_test_refs_use_rules_commands_not_verify_steps` |
| S2 | 派发限制求交与角色选择 | 用 T28 冻结值；给出可用 provider/model 集合、选择及原因、冲突；任务类型作为显式参数 | `test_future_frontend_paths_require_claude`、`test_conflicting_rules_require_split`、`test_override_and_fallback_obey_rules` |
| S3 | 任务阶段、当前尝试、持久化与恢复 | 先定状态转移表/迁移/回放；绑定卡/规则 hash、请求和报告字段，保留终态及退出 | `test_late_implementation_result_is_history_only`、`test_restart_requires_attempt_and_workspace_reconciliation`；replay |
| S4 | 单执行者 worktree 和实际交付检查 | 固定基线，记录拥有的目录；候选固定前收集所有路径，拒绝越界/受保护改动 | `test_delivery_includes_untracked_and_rename_sides`、`test_protected_paths_rejected`、`test_single_executor_serializes` |
| S5a | 写模式适配器离线实现 | runtime-protocols；核对本机 schema/help；单独写卡定义权限和可审核探测工具，不能直接放开现有只读路径 | 假 CLI `test_implementation_permission_contract`、`test_terminal_and_exit_are_independent` |
| S5b | Claude 起草、GPT 复核、规则驱动实现编排 | 任务范围不可完成时交回规划；模型覆盖/有序备选仍须满足规则；同次重试固定规则 | `test_claude_draft_gpt_review_precedes_dispatch`、`test_scope_block_returns_to_planning`、`test_retry_keeps_frozen_rules` |
| S6 | 独立候选测试检出、来源及同提交审核 | T28 命名命令，S3 存储、S4 固定候选；先完成测试隔离契约，再连接既有审核器 | `test_tests_and_review_share_candidate`、`test_test_credentials_are_inaccessible`、`test_failed_tests_are_traceable` |
| S7 | 至多一轮返工、新候选及重新验证 | 已核实且原范围内的问题；从当前候选新建 worktree/尝试；冻结原规则不读取候选修改 | `test_frontend_rework_uses_claude`、`test_second_rework_is_refused`、`test_new_candidate_requires_new_evidence` |
| S8 | CLI 端到端、取消/恢复/状态显示 | 模块入口、临时 Git 仓库和假 CLI 全闭环；单/双审沿用原实现，界面开发不在此卡范围 | `test_phase_two_end_to_end`、`test_incomplete_or_disputed_review_requires_user` |
| S9 | 实机写权限与最终验收 | 先交可审核具体方案/工具，再按用户授权执行；未通过不能宣称阶段 2 整体验收 | Codex workspace 写沙箱、Claude 写工具集、worktree 外写拒绝、受保护路径拒收；分别保存终态/退出/清理 |

S4 与 S6 的基础能力可先用假执行器验证，但实际开发仍逐卡按依赖串行。现有两个审核席位继续并行；多执行者并行、补丁汇集、冲突集成、自动接受/合并不在阶段 2。

## 需求追溯表

| 计划子句或用户要求 | 交付归属 | 可判定验证 |
| --- | --- | --- |
| 工作流1：Claude 起草、GPT 对原需求复核遗漏/范围/依赖/验收 | S5b | `test_claude_draft_gpt_review_precedes_dispatch` |
| 工作流1：JSON Schema、待决问题、命名测试 | T28 / S5b | `test_unresolved_blocking_question_prevents_dispatch`、`test_unknown_dependency_and_dependency_cycle_are_rejected` |
| 工作流1：新增命令先由用户采纳 | T28 / S6 | `test_proposed_tests_do_not_become_approved_commands`；执行器只取冻结 commands |
| 工作流1：范围不足交回规划 | S3 / S5b | `test_scope_block_returns_to_planning`，保存原因与所需路径，旧卡不继续写 |
| 工作流2：项目规则/依赖约束派发；共享接口先定 | T28 / S2 | 规则/任务图单测；依赖实现后重新核对基线，接口先提交后发卡 |
| 阶段2：单执行者 worktree 实现 | S4 / S5a / S5b | `test_single_executor_serializes`、实现会话 cwd/权限传输假 CLI 检查 |
| 规则：基线读取/hash，派发重试返工固定 | T28 / S5b / S7 | `test_rules_are_read_from_baseline_not_worktree`、`test_retry_keeps_frozen_rules` |
| 规则：路径与任务类型限制交集、冲突拆卡 | S2 | `test_future_frontend_paths_require_claude`、`test_conflicting_rules_require_split`；不把 glob 当字面路径或仅枚举现有文件 |
| 模型：预设、覆盖、有序备选、实际证据 | S2 / S5b | `test_override_and_fallback_obey_rules`；请求值与报告值分开，未知不填请求值 |
| 阶段2：前端实现/返工均派 Claude，GPT 可审核 | S2 / S7 / S8 | `test_frontend_rework_uses_claude`、前端候选 GPT 单审和双审集成测试 |
| 阶段2：拒绝越界、受保护路径 | S4 | `test_protected_paths_rejected`、覆盖新增/未跟踪/删除/重命名两侧和路径别名 |
| 规则：已验证的写权限 | S5a / S9 | 离线合同检查及独立实机探测；只读历史结论不能替代 |
| 工作流3单执行者部分：固定候选、完整配置测试、日志 | S4 / S6 | `test_tests_and_review_share_candidate`，日志仓库外，argv/exit/hash 可追溯 |
| 规则：独立测试检出、限制权限/凭据 | S6 / S9 | 权限限制与环境白名单，拒读凭据 canary；清空环境本身不宣称文件凭据不可读 |
| 阶段2：测试与审核同提交、来源不同于用户输入证据 | S6 / S8 | `test_tests_and_review_share_candidate`，新增来源与执行记录进入 hash，不冒充 `user_supplied` |
| 工作流5：至多一轮，已核实/原范围，证据充分单方问题处置 | S7 | `test_second_rework_is_refused`，已核实阻塞、未知范围和分歧分别判定 |
| 工作流5：返工从当前候选建工作树，新尝试/新测试/新快照 | S7 / S8 | `test_new_candidate_requires_new_evidence`，旧审核只关联旧候选 |
| 持久化：当前尝试事务核对；重启前检查上次结果 | S3 / S8 | `test_late_implementation_result_is_history_only`、恢复回放 |
| 失败/取消：分类、日志、终态与进程退出分别保存 | S3 / S5a / S6 / S8 | state-change 配套表/回放；取消与晚到事件测试 |
| 最终接受/合并由用户触发 | S8 / S9 | CLI 不自动合并；阶段3接受时目标基线核对另行实施 |

## 开发与实机证据

每卡先按 task-card 规划并交另一提供方复核，意见写入交付文档、原始输出在仓库外。GPT 子代理可读源码或做辅助评审，但不得冒称跨提供方。实现前 `--dispatchable`，检查 baseline 至开工 HEAD 是否碰到写入范围；需要时先重定基线。新增命令与真正影响交付的未决问题按原计划交用户，不把常规技术选型当审批。

T27 先提交 T28 草案，T28 草案基线是启动 HEAD、依赖 T27。由于它自己的任务卡路径在 write_scope 内，T27 提交后必须将 T28 重定到该实际提交，再次校验后领取，不预测尚不存在的提交 hash。

开发使用假 CLI 和已有夹具。每卡交付开工 HEAD、候选 SHA（通过仓库外 completion 回执绑定，避免自引用）、逐项验收、原有测试改动及原因、实际路径、完整 verify 五步结果。不得删除/削弱旧检查。日志保存到 `/Users/owen/Desktop/Multi-agent-acceptance/phase-two/`，不修改已有封存。

S9 已按用户对具体四项方案的授权直接执行，用户明确要求助手运行而无需手动终端。T52 提交上的 Codex 原生 13 项操作、Codex 模型正向写入和 Claude 文件工具正负向请求均经实际日志复核；两次模型调用没有重试。测试权限 case 在启动前因探测器 256 MiB 限制拒绝了当前 280,704,736 字节的固定 Codex 二进制，未运行测试命令，原失败完整保留。

用户随后明确要求修复大小限制并在检查后直接复验最后零模型 case。T53 将探测器和测试运行时中的 256 MiB 上限对齐到产品入口既有的 512 MiB，保留原测试与安全门禁；完整五步检查通过，3301 项 unit、68 项 replay 通过。实际复验已越过原大小门禁并启动固定 Codex 0.169.0，但测试适配器误拒绝合法的 `configWarning` 与 `remoteControl/status/changed`（disabled）通知，在测试命令发出前失败。实际模型调用和 command/exec 均为 0，测试权限 canary 未执行；进程退出与隔离目录清理已确认。原大小失败和新协议失败分开封存，详见 T53 交付和仓库外回执。前三项仍保留原提交及原始报告，不伪称在修复提交上重新调用模型。开发协作的 Claude 只读卡片复核已有明确授权，与零模型权限 canary 分开记录。整体仍约 90%，下一阻塞为测试运行时的异步通知兼容性；未取得实际测试权限证据前，不能宣布阶段 2 整体验收完成。

用户继续明确授权修复合法异步通知处理，并要求保留原失败、完整检查后由助手直接零模型复验。T54 仅兼容固定 schema 定义的配置警告和远控 disabled 状态通知，在预检、执行及清理共用校验；原有严格门禁保留。基线 7 项有效回归失败已记录，修复后 88 项相关测试通过，完整五步为 3322 项 unit、68 项 replay 通过。新目录实机报告 `passed=true`：实际两条 command/exec 均退出 0、9 项固定检查全部通过、共享构建和缓存验证成功；实际两种通知正常接收，模型调用 0，受管进程退出和隔离清理已确认。原 S9 与 T53 共 108 个证据文件保留，不重复前两次模型 case。最终提交绑定和独立复核见 T54 交付及仓库外回执，任意后代停止和生产者 EOF 继续未确认，最终接受/合并仍由用户触发。
