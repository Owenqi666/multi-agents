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
