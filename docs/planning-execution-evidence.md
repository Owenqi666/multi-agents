# 规划执行证据

T40 将一次规划执行的日志、资源事实和实际运行事件封存到仓库外，并提供核验后绑定任务的边界。完整执行、取消和预检控制器由后续 T41 接入；本模块不启动 CLI、不等待进程、不发送信号，也不修改规划状态。

## 日志与封存

`prepare_planning_logs(store, run_id, stage, attempt)` 核对已冻结的 Prepared/context，在该运行的 `logs/` 下独占创建 `<stage>-<attempt>-<context-token>/`，权限为 700，返回不可变目录身份。已有路径和软链均拒绝采用。创建目录不证明模型从未启动或进程已停止，也不授权恢复既有 Prepared。

调用者先完成 RuntimeAdapter 的资源结算，按实际值保存 terminal 和 ProcessExited，再调用 `seal_planning_execution`。它直接向本次 adapter 实例取 ownership receipt 和 `result(turn)`，不接受调用者填写停止成功的布尔值。EnvironmentInfo 与已存 EnvironmentObserved 精确对应；未知字段保留 null，请求的 model/effort 不替代实际报告。

封存先以独占方式创建 `.seal-attempt.json`，保存绑定和实际 receipt，关闭并设为 400；再独占创建 `execution.json`。主 manifest 绑定 marker 的身份和 hash，包含自身实际文件身份、run/input、Started/Prepared、source/context/owner、stage/attempt、日志目录、环境、资源事实、原始日志索引和实际 terminal/exit。两份文件使用 canonical planning JSON 加换行。manifest 上限为 16 MiB，超限明确失败；原始日志以流式方式核验，不受这个索引上限限制。

只有下面条件同时成立，manifest 才有资格支持宿主验证：

- 本实例恰有一次 environment 和一次 turn operation，已全部结算，无 pending/unknown acquisition。
- processes_stopped、resources_released、logs_sealed、outputs_complete 分别为 true。
- 有实际模型 handle，持久化的 Completed 和 ProcessExited 与实例 result 精确一致。
- 环境观察一致，冻结上下文、可执行文件和日志目录身份未变，全部原始日志仍有原身份、模式、大小和 hash。

各项事实分别保存。日志失败或资源未释放不会把实际进程停止改成未知；反之，退出码为 0 或成功文本也不能补齐停止、EOF 或日志证据。不合格的 forensic manifest 可以保存，但不能授予 validation 资格。

adapter 必须配置为本次 owned 日志目录。helper 在显式打开任何原始日志前检查其属于该目录；adapter 自己的 receipt 查询也可能核验它所持有的文件。本边界依赖受信任的 RuntimeAdapter 实现，不提供对任意恶意 Python 对象的 I/O 隔离。

## 只读核验与绑定

`inspect_planning_execution` 只沿该尝试已经持久化的 Reconciled.log_path/hash 定位 `execution.json`。它核对 canonical JSON、版本和结构、400 模式、原文件身份/大小/hash、marker、全部绑定与实际事件，再重新计算 receipt 条件并核对原始日志及冻结上下文。只声明 `qualified=true` 不能通过。Reconciled 的停止事实必须与 receipt 一致且为 true。旧 synthetic 日志、未 managed 的尝试和不完整 receipt 不取得资格。

核验是对封存证据的检查，不是接管已消失控制器的进程所有权。绑定通过 `bind_planning_task(store, planning_run_id, task_run_id, created_at)`，严格选择当前 draft 和 review；复核请求必须指向当前 draft attempt 与 task hash。两份 manifest 核验通过、core 可绑定且用户问题已解决之后，helper 将所检验的 plan hash 交给 store。

`PlanningStore.create_planned_task(..., expected_plan_hash=...)` 在原 `BEGIN IMMEDIATE` 事务中重建当前 provenance 并比较，再写 task、provenance、bound 状态及历史。文件核验后如果规划来源改变，整个绑定回滚。旧省略参数的可信底层调用保持兼容，不能把它描述为经过本模块的文件核验。plan hash 覆盖所选请求、事件、reconciliation 及两份 manifest 路径/hash。

这一比较不禁止具有主机写权限的人之后修改文件；再次 inspect 仍会检查原 hash。

## 失败与调用顺序

正常顺序为实际 Completed → 实际 ProcessExited → seal → Reconciled → 宿主 Validated。仅成功封存且合格时，调用者才将 manifest 路径/hash 写入 Reconciled。seal 不合格或失败时，调用者保留实例实际停止事实，日志字段保持 None，再按阶段提交 EvidenceRejected 或 Diagnostic，不能生成另一份模型终态。T40 自身不写这些事件；测试中的事件驱动明确标为 synthetic。

已有 marker 禁止同一 attempt 重封。写入、关闭、chmod、读取等失败留下的残留不删除、不覆盖，也不通过新文件名绕过；当前尝试已 EvidenceRejected 时同样拒绝。磁盘或数据库错误继续抛出，不修改已存运行事实。若故障发生在 marker 创建前，或磁盘无法保留任何文件，helper 不能宣称跨进程记住这次失败，后续控制器必须保存拒绝/诊断及实际停止核对。

Prepared 后、spawn 前崩溃也属于停止未知。锁已释放、PID 不存在或新建空 adapter 都不能补 true；本模块不提供自动恢复。未来 S8 将提供明确人工核对入口，在此之前未知尝试保持阻塞。

## 验证范围

离线测试使用临时 Git、明确标记的 synthetic adapter/receipt 和实际本地临时进程，覆盖精确绑定、资源不足、文件篡改、独占封存、文件故障与原子绑定。它们不构成真实模型或阶段 2 权限验证。没有新状态、事件、FailureCategory 或 SQL 迁移，旧回放和可信底层 API 保留。
