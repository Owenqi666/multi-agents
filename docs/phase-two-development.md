# 阶段 2 开发路线

用户于 2026-11-16 明确指示：“新开一个阶段2窗口开始按开发顺序继续执行”。本路线从已验收的阶段 1 开始，逐卡开发到单执行者的规划、实现、测试、审核、一次返工闭环。开发 checkout 为 `/Users/owen/Desktop/Multi-agent`，启动 HEAD `f6729e501b7b1db789d3eefdef4c41d0b1646115`。每卡正常独立提交；最终接受/合并仍由用户触发，不自动进入阶段 3。

## 已有能力与实际缺口

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

只有 T28 已建具体卡。下面 S2–S9 是后续拆卡顺序标识，不表示已有接口；实际编号在各次规划时顺序分配。必要时一行拆成多卡，不能用这张路线图直接授权全仓写入。

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

S9 写权限实机验证尚未获对应具体方案的执行确认。先把探测范围、被允许写入的临时目录、外部拒写 canary、命令、预计调用和清理机制写成卡及可审工具，再判断已有授权或请求必要确认。开发协作的 Claude 只读卡片复核已有明确授权，不属于 S9 产品实机验收。未取得 S9 证据时，只能报告离线实现完成，不能宣布阶段 2 已通过。
