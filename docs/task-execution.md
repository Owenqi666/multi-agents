# Managed 实现尝试的持久化契约

T42 在任务状态与数据库中保存完整的实现准备记录及实际执行事件。创建工作区、检查文件、调用模型、封存执行 manifest 和候选提交属于后续边界；这里保存的可信宿主输入本身不证明真实权限或文件内容已经核验。

## 模式与冻结输入

`TaskAttemptStarted.managed` 显式区分新执行链，默认 False 保持旧接口。模式不由模板名称推断。一个任务运行的 managed 与 legacy 尝试不能混用，避免通过旧入口跳过新的交付证据门槛。旧记录缺少新字段时保持 legacy，不补造事件或工作区身份。

managed 尝试先由 Started 冻结请求，再紧接 TaskExecutionPrepared。准备记录包含完整 WorkspaceRecord、初始工作区 digest、精确 prompt、深冻结 output schema、运行策略、完整派发策略和宿主控制 context 身份。workspace 的控制文件按显式 hex 保存，`None`、空 bytes 和非 UTF-8 bytes 分别往返，不从路径猜测原身份。

TaskExecutionPolicy 固定一个 runtime provider 的 executable 路径/身份/hash、次数预算、timeout 和 grace。TaskDispatchPolicy 另存 task_type、requested override 和完整有序 fallbacks，包括每项的 effort。旧 DispatchDecision 的 selection_history 在选中处结束，不能用它还原后续备选。`claude-code` 到 runtime `claude` 的对应只是请求路由，不是实际模型报告。

同一运行的重试固定这两份策略，并使用新的 workspace/context 身份与 token。尝试次数包含预检失败。重试先要求当前尝试实际停止、最终工作区 digest 已知；初始 digest 不能代替失败后的检查结果。本卡不建立候选或返工来源资格。

## 原子准备

纯 `reserve_task_execution(state, started, prepared)` 在局部不可变值上调用登记过的转移，两步都为 APPLIED 后才返回。失败时原 state 不变。纯 `apply_task` 可以表示单步中间值，但没有 Prepared 的 managed 尝试不能接收运行事件或获得交付资格。

数据库公共 `apply_task` 拒绝单独的 managed Started 和 Prepared。`TaskStore.reserve_task_attempt` 在同一个 BEGIN IMMEDIATE 中读取当前状态、输入和完整历史，检查 CAS、workspace repository、派发决定与准备不变量，再一次写入状态和顺序历史。它复用纯 reserve，不另建一套状态逻辑。

CAS 对象就是 `load_task_history` 返回的有序 `tuple[(TaskEnvelope, Disposition), ...]`，包含 HISTORY_ONLY。Envelope 的 attempt、occurred_at 和完整事件都进入 canonical hash；位置表达顺序，Disposition 使用字符串值。run_id 在事务中选择，不加入 hash；SQL sequence 列不另编码。条数和 hash 同时检查。

store 用完整 TaskDispatchPolicy 调用现有 `resolve_dispatch`，精确比对 Started.decision。known-unavailable 集合仍来自可信调用输入，这一步不宣称验证实际 availability 的来源。后续执行边界负责连接实际预检与失败记录。

## 实际事件与交付资格

managed `TaskAttempt.events` 保存已应用的 Started、Prepared、运行时事件及宿主事件；旧尝试迟到仍只进入完整历史，不能推进当前状态。legacy Diagnostic 继续 HISTORY_ONLY，保持旧状态不变。managed Diagnostic 可保存诊断，但不变成模型终态。

managed Prepared 后也可独立保存真实模型 ProcessExited，即使没有模型终态。它只记录退出码，阶段仍为 implementing，不补模型结果或进程组停止；实际 Reconciled 才关闭宿主阶段。辅助预检进程不能冒充模型退出。legacy 对 implementing 中提前 exit 的原拒绝规则保持。

TaskEnvironmentObserved 记录实际预检信息，未知值保持 null。首次观察位于 Prepared 之后、模型事件/取消/reconciliation 之前；相同 canonical 值重复只进历史，不同值拒绝。它不覆盖 Started.environment，也不从 requested model/effort 补实际值。

TaskEvidenceRejected 可在 managed awaiting_delivery 拒绝无法使用的成功产物，不要求先补齐 exit、stop 或日志。它将宿主阶段转为 failed，同时保留原 Completed/result、实际退出、报告和 reconciliation。之后补全停止核对不会恢复同一次产物的交付资格。

TaskReconciled 分别保存实际 process_stopped、最终 worktree_digest 和执行 manifest 的 log_path/log_sha256。扫描未知使用 None；停止事实不因日志或扫描失败被改写。log 字段成对出现，并指向执行结束后的 execution manifest。执行前 TaskExecutionContext.manifest_sha256 只绑定控制输入，不能替代它。

managed 进入 planning_required 后仍保存当前尝试最新的 Reconciled，包括撤销旧停止事实；阶段保持 planning_required，不能恢复交付或重试。legacy 同一阶段仍保持原来的 HISTORY_ONLY。

managed delivery_ready 必须同时具备 awaiting_delivery、实际成功、实际退出、最新停止为 true、已知且非空的最终 digest、成对非空的执行 manifest receipt 及 Prepared。core 只检查可信字段和状态；后续边界必须实际 inspect manifest 才能提交这些字段或冻结候选。legacy 原门槛保留，不因此获得 managed 资格。

## 兼容与验证

新 managed hash 使用 UTF-8、排序键、紧凑 JSON、`allow_nan=False`，事件带 kind，bytes 使用明确 hex 标签。它不改旧 task/rules/plan hash。codec 只为缺失的新可选字段提供 legacy 默认，旧必需字段缺失仍抛错；旧字段形状保持。

已有 task_attempts/history 表存 JSON，扩展不需要 SQL 表迁移。转移、codec、原子 reserve 和新回放一起验证。旧四个 task 场景保留；legacy reconciliation 继续核对原三字段，新 managed 场景另外核对完整 receipt 与事件序列。离线合成记录不代表实际模型或权限验收。

## 持锁准备边界（T43）

`prepare_task_launch(database_path, task_run_id, planning_run_id, ...)` 在一个持锁作用域中打开实际数据库、核对已绑定的规划来源、创建全新的工作区和宿主 context，再原子保留本次尝试。作用域内的 launch 只能 claim 一次；退出后所有普通复制副本一并失效，复制不能重新取得领取次数。之后的 writer 必须在该作用域内完成调用、停止核对和持久化，不能把 Prepared 取出后另行启动。

首次调用需要明确 runtime policy 与完整 dispatch policy；重试可省略以沿用原值，显式值必须精确一致。执行文件的路径、身份、内容 hash 和可执行位分别核验，capture 不运行它。模型与档位仍是请求值，可用性仍须实际预检报告。当前派发规则固定一个 provider；有序备选中的其他 provider 只留下被规则拒绝的记录。

业务 directory 必须是数据库旁的全新 canonical 目录，并位于所有工作树和 Git 目录之外。内部 workspace 从原始 baseline 的对象字节建立；context 保存 0400 的 prompt、schema 和控制 manifest；logs 只冻结目录身份，后续运行可写日志。模型需要的原始输入直接内联到 prompt，它不读取外部 context。

Git common directory 下的 `multi-agent-task-preparations` 是宿主 registry，目录为 0700、每个 marker 为 0600。这里的 repository 是 Git common directory，source 才是工作树根；registry 不进入源码 digest。锁和 registry bootstrap 之外，任何业务资源创建前先登记 marker。它记录实际已观察到的身份，null 是未知，不能解释为未创建。可更新 marker 的稳定身份进入不可变 context，内容本身不建立循环 hash。

准备会核对同仓库所有 marker 和当前 DB 的任务历史。foreign DB 指针不自动打开；partial、未匹配 reservation、未知停止或未知最终 digest 都阻断。已停止的旧尝试仍需核对实际工作区控制身份与最终 digest；初始 digest 不能替代结束扫描。既有 store 没有公开数据库路径属性，CLI inspector 只用 `PRAGMA database_list` 读取当前连接 main 文件身份，不碰私有业务表或另建事务。

创建失败、CAS 冲突或提交后抛错都不能产生 launch。T43 保留负向 marker 和实际残留及异常路径，不额外清理；T31 自身已有的安全失败清理保持。旧 Prepared 只可 inspect，不能重新派发。context 的控制 hash 也不提供 Completed、ProcessExited、process_stopped 或结束执行 manifest 的资格。

遇到未知残留时，保留数据库、registry、工作区及原异常，停止继续派发，核对原 controller 的资源记录和实际文件身份。锁可取得或 PID 消失不证明进程组已经停止；手工删除 marker 也不能替代核对。证据绑定的人工恢复入口留给 S8，当前不提供绕过或自动收养流程。

## 一次性执行证据（T44）

`bind_task_execution` 在 T43 作用域内把尚未使用的 launch 交给同一个全新 ImplementationRuntimeAdapter。它验证完整准备记录和公开配置，要求实际 ownership receipt 没有 operation 或 resource，再 claim launch。binding 的普通副本共享一次消费状态；数据库归档不能恢复 binding，也不能换一个同路径 adapter 继续封存。

`seal_task_execution` 只读取该 adapter 的实际 result 与 receipt，不运行、取消或清理进程，也不写数据库。首次调用立即消费资格，分别独占写入原 logs 下的 `.seal-attempt.json` 和 `execution.json`；失败残留原地保留，不能取第二份更有利的收据重封。manifest 保存实际终态与退出、环境观察、runtime 报告字段及其来源、所有权收据、派生事实、最终摘要和输出解析结果。未知 model/effort 继续为 null；环境能力列表和请求不能代替生效值。实际非零退出也不改写模型 Completed。

轻检查只核 live lease、数据库和目录身份、当前 Started/Prepared 及公开配置。只有首份实际 receipt 已确认 processes_stopped，才调用完整 context 检查与工作区扫描；否则 final_digest 为 null 且不具备 qualified。停止为 true 后扫描失败仍保留原 receipt，后续 writer 必须独立保存真实停止事实，不能用初始摘要顶替未知结束摘要。

原始日志必须先通过目录范围检查。未 sealed、未关闭、不健康或缺少实际元数据的日志不读取、不补 hash；完整 sealed 日志按 receipt 中的 byte_count 有界读取，核对 regular file、身份、模式、大小和 hash。控制文件还限制为 16 MiB。这个有界保证只针对本模块的核验；既有 Recorder 的 receipt getter 对 closed file 仍有自己的 EOF 检查。

输出只接受最多 1 MiB UTF-8 的严格四字段 JSON，task_hash 必须匹配当前卡。completed 的 required_paths 必须为空；scope_blocked 需要非空说明和具体安全相对路径。路径保留字面量 `[slug]`、`(group)`，拒绝 glob、逃逸、控制字符、Git 元数据及规范化后重复的别名。这是模型报告，不能自动扩写入范围或证明代码、测试结果。

qualified 另需当前尝试仍 awaiting_delivery、实际成功及退出、与 Observed 一致的环境、完整且停止的资源与封存日志、稳定 context/最终工作区及合法输出。scope_blocked 可 qualified，但 delivery_eligible 为 false。接受的取消或 EvidenceRejected 永久撤销资格。核验实际 terminal/exit 使用同 attempt 的完整历史，包括 HISTORY_ONLY，取消后晚到的 Completed 仍如实保存。manifest 绑定首次样本时的历史前缀，允许随后追加无害的迟到结果，同时重新检查实际事件冲突、当前尝试和撤销。

writer 应先持久化实际事件与环境，完成停止核对，再 seal。TaskReconciled 始终写实际 stop 与实际扫描得到的 final_digest；只有 qualified 且没有宿主错误时，才写执行 manifest 的 path/hash。合法 scope_blocked 随后按 report 原样写 TaskScopeBlocked；不合格的成功产物只在 awaiting_delivery 写 TaskEvidenceRejected，其他阶段保存 Diagnostic。T44 提供上述边界，运行与持久化编排由后续 writer 实现。

`inspect_task_execution` 只核正向归档：要求最新 Reconciled 的真实停止、已知最终摘要及精确执行 path/hash，再核 marker、manifest、原始日志、原 context、实际最终工作区及完整历史。负向或缺锚点记录直接拒绝，不扫描其工作区。后续 planning_required 只允许与原 scope_blocked 报告完全一致的 handback；旧工作区、context 和 logs 必须原地保留，任何变化使核验失败。归档不产生 live owner；候选消费者仍须检查当前状态与 delivery_eligible。

## 一次实现运行器（T45）

`execute_task_attempt(database_path, task_run_id, planning_run_id, *, repository, directory, ...)` 只执行一次新的 managed 尝试，返回实际 TaskState。worker 从 T43 准备到调用、停止核对、封存与持久化始终保留同一个 store 和 executor lease。调用前已收到 stop 时读取已有状态返回，不创建新 reservation、工作区、日志或 adapter。它不自动重试、切换备选、重新规划或创建候选。

adapter 的执行文件、工作区、schema、日志目录和运行策略来自 Prepared。绝对 deadline 在 bind 前计算一次，预检与实现共用，清理另有 grace。预检先登记实际 operation，才能响应取消；环境观察照实持久化。None 表示未知并允许继续，明确未登录或已报告的模型/档位集合不支持请求才停止。预检正常返回后还要确认实际 settled 和 processes_stopped，未知时不启动 model。模型会话使用请求的 model/effort、WORKSPACE_WRITE、实际 workspace cwd 及冻结的内联 prompt。

公共运行事件按到达顺序保存，result 只补尚未观察到的实际终态或退出。取消后 exit 与 Completed 的先后可能改变 APPLIED/HISTORY_ONLY 及报告字段，不能人为重排。唯一窄缓冲是尚无实际 exit 的 cleanup_incomplete：待 drain/result 后，有真实 exit 就保存原 Error，没有则保存 Diagnostic，不能据此发明模型终态或物理停止。

外层 caller cancellation 只设置意图，重复取消不会打断 worker 或唯一 event consumer；实际结算后才抛首 CancelledError。accepted callback 先保留原取消 envelope，再同步写数据库。DB 写入异常后仍消费原 stream，原 envelope 和真实重复次数保留；用完整历史的出现次数核对提交后抛错。故障后 accepted cancel 的及时持久化优先，旧 pending 普通报告可能较晚插入，但原时间和队列内部顺序保留。这类宿主问题不获得正向 logpair。

运行器按可能新增资源的 operation 划分 receipt 的有效期。预检已停的样本不能证明随后 turn 已停；进入新 operation 后必须取得新样本。相同 turn 已确认停止后 getter 单纯失败可保留原事实，但观察到 owner 改变或丢失会使该停止资格失效。停止未知不扫描工作区。seal 仍只有一次；封存失败而本次资源确实已停时，独立扫描原工作区获取实际最终摘要，扫描失败保持 None。

正常模型失败、明确预检拒绝或无效输出返回实际负向状态；宿主异常先记录首错误、继续收束和尽力持久化，再抛出原异常。活 consumer 内的写库/轻检查错误通过内部异常包装暂存原对象，不以退出 consumer 代替停止资源。少见的自定义 Exception 也不能让 finally 写入合格证据；日志、扫描或后续清理错误不替代首异常。

无宿主问题且执行证据完整时，先保存真实 Reconciled，再 inspect 正向归档。scope_blocked 按报告原说明与路径写 ScopeBlocked；completed 保持 awaiting_delivery。后续错误用新的 Reconciled(actual_digest, actual_stop, detail, None, None) 清除 logpair，合法阶段再发 EvidenceRejected 或 Diagnostic。写操作是否无错与是否已确认提交分别记录；DB 最终不可写时只能报告结算未持久完成，不能声称已撤销或继续消费产物。
