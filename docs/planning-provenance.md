# 单卡规划来源与派发门禁

T34 建立规划的不可变输入、角色尝试、宿主校验和持久化关联，不启动模型或创建工作树。T35 将连接只读规划调用，后续实现 runner 才使用这些门禁；旧 `TaskStore.create_task` 本身没有规划来源，不能被描述为已经过 Claude/GPT 规划。

## 输入与角色

`PlanningInput` 冻结原始需求、基线 `FrozenProjectRules`、两角色请求、依赖声明和用户决定。draft 运行时 provider 固定为 `claude`，review 为 `codex`；此处不是项目规则中的 `claude-code`。模型 ID 是显式请求，不靠字符串前缀证明实际模型。实际模型/档位来自规范化运行事件，缺失保留未知；报告与请求不同也原样保留。

每个规划运行产出一张可执行卡。`DependencyReceipt` 绑定 task ID、本次具体基线、`source=user_declared` 及说明，只代表用户声明，不宣称工作台已验证依赖完成。后续依赖任务要在具体的新基线上重新规划与复核，不借本模块实现多任务自动集成。

`UserDecision` 保存问题、答案、用户来源声明及说明。模型填写 `OpenQuestion.resolution` 不能替代它。输入创建后不修改；新增或更改用户答案另建规划运行，产生新 input hash。同一运行的新 draft 只能在原输入下修订草案。未解决问题可保留供展示和复核，但阻止派发；答案导致卡片变化时必须重新复核。

边界 `cli/planning_inputs.py` 负责类型、UTF-8、严格 JSON、任务卡 schema、基线、命名测试、依赖与用户答案对应关系。建议的新测试只保存在卡里，不加入冻结 commands。core/store 信任已校验的输入，另检查状态、身份和来源关联不变量，不吞异常。

## 尝试与状态

draft/review 分别从尝试 1 递增。每次开始冻结请求、精确 prompt、模板版本、环境和时间；运行事件保留会话、实际报告、用量、结果和退出。`PlanningReconciled` 是宿主停止核对，并保存日志位置/hash，不是模型终态。

| 当前阶段 | 允许推进的证据 |
| --- | --- |
| pending | 开始 Claude draft。 |
| drafting / reviewing | 当前尝试运行事件；成功终态进入 awaiting_validation；错误进入 failed/cancelled；请求取消进入 cancelling。 |
| cancelling | 取消终态或真实进程退出可关闭；诊断不代替终态。 |
| awaiting_validation | 成功终态、真实退出、最新停止核对和日志 receipt 全部具备后，才接收相应宿主验证结果。 |
| awaiting_review | 已验证 draft 可开始 Codex review，必须绑定当前 draft attempt 和 task hash。 |
| revision_required | 复核要求修订或卡有未决阻塞问题；完整停止后可重新起草，旧卡及复核立刻失去当前资格。 |
| failed / cancelled | 最新停止核对为 true 后可重试；复核重试仍绑定当前有效 draft。 |
| ready | 当前完整复核为 ready、无阻塞意见、用户问题已解决，可原子绑定任务；未绑定前也可重新起草使旧批准失效。 |
| reconciliation_required | 已 ready 的所选证据后来出现停止未知；完整核对恢复前禁止绑定。 |
| bound | 任务与来源已封存；后来事件只进历史，变更另起运行。 |

转移唯一登记处为 `core/transitions.py:apply_planning`。成功后的信号退出仍保存真实值，不反转模型成功；exit 0 不能补成缺失的成功终态。宿主确认已停止而没有模型终态时，只关闭宿主阶段，不虚构 Error、退出码或实际模型。

宿主验证的产物必须对应当前尝试实际 `Completed.result`：草案经 TaskCard 的可选字段规范化后相同，复核全部字段精确相同。另一份合法卡片或把 needs_revision 改成 ready 都不能绑定此尝试。边界先对实际 result 做 schema 校验，core 负责已校验产物的来源一致性；它不把畸形运行输出修成有效卡片。

旧尝试、已替换阶段的结果只进历史；未开始的未来尝试和非法当前转移直接抛错。同一已验证产物的重复只进历史，失败后首次声称产物已验证则拒绝。当前所选 draft 的停止核对是明确例外：review 开始后仍可更新它，并撤销派发资格。已替换的 draft 核对不能污染新的尝试。bound 后全部规划事件仅保留历史。

## 哈希与来源

新规划 hash 统一使用 UTF-8 JSON，`ensure_ascii=False`、`sort_keys=True`、紧凑逗号/冒号分隔、`allow_nan=False`。数组顺序保留，文本不归一化。不可变容器按字段递归转换为 JSON；运行事件包含 kind，避免不同事件丢失类型。

- input hash 覆盖完整冻结输入。
- task hash 沿用 `TaskCard.task_hash`，包括所有 open_questions/resolution；draft artifact hash 就是 task hash。
- rules hash 沿用原始 UTF-8 规则文本字节，不重新格式化 YAML。
- review artifact hash 覆盖 task_hash、decision、reason、blocking_issues。
- plan hash 覆盖输入、选定 draft/review 请求与精确 prompt、实际事件和用量、核对与日志 receipt、卡片及复核产物。

`PlanningProvenance` 是选定来源的不可变值。绑定动作自身以及绑定后的历史不进入被封存 attempt.events，因此绑定前后 plan hash 相同。未选中的旧尝试仍在规划历史中，但不能借旧复核为新卡放行。

## SQLite 与任务关联

迁移 `0004_planning.sql` 增加规划 runs、attempts、history 及 task_planning_provenance；原三份迁移不改。迁移加载器按编号自动发现 SQL，无额外注册常量。T30 的任务和审核数据保持兼容。

`PlanningStore.create_planned_task(task_run_id, planning_run_id, created_at)` 在一个 `BEGIN IMMEDIATE` 中重新核对当前来源门禁、生成 TaskInput、绑定唯一 provenance、写 bound 状态和历史。调用者不能另传卡片、规则或需求替换已审核输入。一个规划只绑定一个任务，两个连接竞争不能各成功一次。公共 `apply_planning` 不允许单独提交 `PlanningTaskBound`，避免脱离这一事务写出虚假的关联。

旧 `TaskStore.create_task` 保持原接口，只创建无规划 provenance 的底层记录。T34 提供来源查询和新绑定门禁，不声称后续 runner 已接入。恢复接口列出未绑定规划及历史；它不自动重发请求，重试仍必须经过停止核对。

## 离线验证

命名回放为 planning-happy、planning-revision-late、planning-missing-exit、planning-cancel-diagnostic、planning-reconcile-revoked。相同序列在纯状态机与 SQLite 中重复，检查最终状态和完整历史 disposition。双连接竞争另由单元测试验证；边界、hash 稳定性、未知报告值和旧数据库迁移也有独立测试。模型和工作树编排不在本卡内。
