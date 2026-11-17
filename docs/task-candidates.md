# 受管任务的固定候选

T46 在现有实现尝试中保存候选提交与执行证据的不可变关联。记录本身是受信宿主输入，不证明 Git 对象已经核验，也不代表测试或审核已经运行。实际归档核验和 Git 冻结由后续边界连接。

## 事件与资格

`TaskCandidateFrozen(delivery, execution_log_path, execution_log_sha256)` 复用 T31 的 `WorkspaceDelivery` 六个字段：baseline、candidate、changed_paths、digest、ref、tree。APPLIED 事件沿现有 `TaskAttempt.events` 保存；`TaskState.current_candidate` 只返回当前尝试的候选。

首次发布要求当前 managed 尝试已具备 `delivery_ready`，Prepared 存在，阶段为 `awaiting_delivery`。原任务、Prepared 和 delivery 的基线一致；delivery digest 与最新 `TaskReconciled.worktree_digest` 相同；执行 manifest 的 path/hash 与最新 Reconciled 完全相同；ref 精确对应 Prepared 的 workspace owner。这里核对来源关联，文件、Git 内容和路径安全检查仍在 CLI 边界完成。

`candidate_ready` 同时依赖现有 delivery_ready 和这些最新来源锚点。候选发布不改变 phase，不启动测试，不授予接受资格。后续每次消费仍须实际核验归档和 Git 候选，不能只看这个派生布尔值。

一个尝试只保留一个候选。相同事件重复为 HISTORY_ONLY，不重复写入 attempt.events；任何 delivery 字段或执行锚点冲突直接抛出 ValueError。当前候选的不可变检查先于首次发布的阶段检查，故终止阶段相同重发仍只保留历史，不恢复资格。候选存在后不能再发 TaskScopeBlocked 扩大范围。

负向 Reconciled 撤销当前资格而保留原候选与模型事实。仍处于 awaiting_delivery 时，后续真实核对恢复相同 digest/执行锚点可以恢复派生资格；TaskEvidenceRejected 已转入 FAILED 时不能恢复。旧尝试的迟到候选无论是否与旧候选冲突、旧尝试是否合格，都只进入完整历史，不修改任何尝试的 APPLIED 事件。

## 原子持久化

`TaskStore.record_task_candidate(run_id, envelope, *, expected_history_length, expected_history_hash)` 是唯一候选持久化入口。普通 `TaskStore.apply_task` 拒绝该事件；`core.transitions.apply_task` 作为纯函数仍接受它，由专用入口调用。

专用入口在同一个 BEGIN IMMEDIATE 事务中读取当前状态和完整历史、核对来源、写状态并追加历史。当前尝试无论首次发布还是相同重发，都先核完整历史长度及 `task_execution_hash(history)`，其中包括 HISTORY_ONLY 记录；CAS 不符抛 ValueError，任何行都不写。旧尝试先按既有路由保存 HISTORY_ONLY，不因旧 CAS 已过时而丢失结果。零号、尚未开始或未来尝试拒绝；未知 run 为 KeyError，错误事件类型为 TypeError。

提交后调用方发生异常时，以完整历史中的原 envelope 判断实际写入事实，不重新冻结或猜测数据库已回滚。Git ref 创建和数据库事务不能跨资源原子提交；后续冻结边界须保留失败现场，不自动收养未绑定 ref。

## 兼容范围

本卡没有新增 phase、SQL 表或列；既有 attempt JSON 和 history 表保存新事件，因此不需要迁移。旧事件/尝试的编码和省略规则保持，旧 FrozenSnapshot、TestEvidence、snapshot_id 和 user_supplied 来源也保持。独立测试执行记录及其版本化来源属于后续 S6 卡。

## 实际冻结与消费核验（T47）

`freeze_task_candidate(store, task_run_id, attempt, lease)` 把 T44 合格实现归档、T31 实际 Git 冻结和 T46 原子记录接在同一次持锁操作中。`inspect_task_candidate` 使用相同参数，只重新核验已经绑定的当前候选。调用方保持同一个实际 store 与 Git common-dir lease，直至本次操作以及需要在锁内进行的后续消费结束。

T45 返回时已经关闭原执行作用域；后续可以重新取得相同仓库的有效 lease，核验已保存的归档。这不会重启或恢复旧运行时 owner。当前接口支持实现阶段的 `awaiting_delivery` 和当前候选；未来测试、审核或返工改变状态关联时，需要对应的边界扩展。

入口先检查当前 managed 尝试、Prepared、实际停止、最终 digest 和执行锚点。缺少这些事实时拒绝，不能先扫描可能仍活跃的工作区。实际归档必须 qualified、delivery_eligible 且报告为 completed；合格的范围交回报告也不能冻结成候选。

冻结前捕获完整历史 H，包含 HISTORY_ONLY。归档核验、Git 冻结和数据库登记使用同一个 H；期间历史变化即拒绝，不改用新历史继续。只有实际 APPLIED 才算登记成功。末次实际 inspection 之后还核历史恰为 H 加本次原 envelope/APPLIED，避免重新进入 inspector 时意外放宽原始比较条件。

Git 核验包括确切 ref、commit、tree、唯一父提交、完整文件内容和原基线变更集；二进制、执行位、symlink 和重命名两侧均计入。实际读取的 commit、递归 tree 和 blob 按 Git 对象 header 加原始 body 重算内容寻址哈希，不能只信 Git 输出中的 OID 文本。路径范围使用原 TaskInput 的冻结卡片与规则。direct packed ref 可核验；symbolic、symlink、垃圾 loose ref 或身份变化拒绝。

Git ref 与 SQLite 事务不能跨资源原子提交。ref 已创建后数据库或末次检查失败时，保留真实 ref、工作区、归档和最初异常；不删除 ref、不重新冻结、不自动收养。数据库提交后抛错时，只回读一次完整历史，按原 envelope 说明是否确认 APPLIED；回读失败保持未知，不宣称回滚或撤销成功。

已有候选只能 inspect。inspect 不写历史、核对记录或文件，也不重封日志。每次消费都重新核原归档、当前候选、Git 内容、数据库历史和身份；不能把首次成功缓存为永久资格。原工作区、context、日志仍须保留。这里沿用合作式宿主锁和现有文件边界，不宣称防御任意敌意宿主并发。
