# 实现任务状态与持久化（T30）

本卡提供实现阶段的状态底座，不启动进程或自动恢复。后续卡再连接工作树、交付校验、测试、审核与返工；`awaiting_delivery` 既不是候选接受，也不是用户验收成功。

## 值对象和来源

`TaskInput(card, frozen_rules, requirements, created_at)` 保存完整任务卡、冻结规则的仓库/基线/blob/原始字节及解析值、原始需求和创建时间。raw rules_hash 只标识规则字节，不能单独标识跨仓库或跨基线的一次运行。

`TaskState` 绑定 task_id/task_hash/rules_hash/baseline_commit；`TaskAttempt` 保存 immutable request、规范化运行记录 `AttemptState` 与最近一次 reconciliation。request 含 `DispatchDecision`、worktree、prompt_template_version、started_at 和 environment。环境仅由边界提供实际检查记录，拿不到的字段保持未知，不把请求模型或 effort 复制为实际报告值。

`TaskAttemptStarted` 的 decision 两个 hash 必须与任务绑定一致。实际模型/effort 仍仅由已有规范化 SessionStarted/Completed 提供，并保留来源；消息、用量、终态和退出的原始事件留在历史。宿主恢复只能改变 TaskPhase 和 reconciliation，不伪造运行时 Error、失败分类、实际配置或退出码。

## 事件与集中转移表

纯函数为 `core.transitions.apply_task(state, TaskEnvelope(attempt,event,occurred_at))`。合法事件集中登记在该文件的 `_TASK_TRANSITIONS`；未登记的转移直接抛 ValueError，不在存储或调用方绕过。

| 当前 phase | 事件/门槛 | 结果 |
| --- | --- | --- |
| ready | TaskAttemptStarted，编号1，decision hash相符 | implementing，创建首尝试 |
| implementing | SessionStarted/Message/Usage | 保留观察；运行中 |
| implementing | Completed | awaiting_delivery，保存成功终态；尚未取得退出证据时 delivery_ready=false |
| implementing | Error（真正的边界认可终态） | failed；取消分类则 cancelled |
| implementing | CancelRequested（适配器已接受的取消意图） | cancelling；不声称停止 |
| cancelling | Completed/Error 或真实 ProcessExited | cancelled，仍分别保留终态/退出；取消结果不成为交付成功 |
| implementing/cancelling | TaskDiagnostic | HISTORY_ONLY，原始诊断保留，phase不推进 |
| implementing | TaskReconciled(process_stopped=true) | phase=failed；runtime原状态、未知字段及exit原样保留，同次核对可许可重试 |
| cancelling | TaskReconciled(process_stopped=true) | phase=cancelled；无退出码就仍为未知 |
| 实现中、取消中、failed、cancelled、awaiting_delivery | TaskReconciled | 以最近核对为准；false撤销旧true的重试/交付准备许可，不能沿用旧停止事实 |
| failed/cancelled | TaskAttemptStarted，下一编号，最近核对确认停止且已检查工作区 | implementing，新尝试；保留旧尝试和历史 |
| awaiting_delivery | 首个真实 ProcessExited | 保存原始退出码；成功终态且存在退出证据才 delivery_ready=true |
| awaiting_delivery | TaskScopeBlocked，须delivery_ready | planning_required，保留原因和required_paths，同卡不能继续启动 |
| awaiting_delivery（managed） | TaskReworkStarted，当前候选可核验、原来源精确绑定、尚无返工、原总预算允许 | implementing，新attempt从当前候选开始；与Prepared同事务预留并消费唯一返工轮次 |
| 终结后 | 重复终态/迟到消息或重复取消 | HISTORY_ONLY |
| 已有exit_code后 | 第二个 ProcessExited | HISTORY_ONLY，不能把第一次退出码洗掉 |
| planning_required | 重复范围阻塞、运行时观察、核对 | HISTORY_ONLY；新的任务卡需另行规划/建立运行 |

取消中重复 CancelRequested 只留历史。当前编号重复 start 是错误，不能把并发重复启动当幂等成功；未来尚未开始编号同样是错误。**attempt < current 的所有事件先路由为 HISTORY_ONLY**，包括旧 start、核对、范围阻塞和退出；不会更新旧聚合记录或当前状态。

T52 的返工请求是独立的 `TaskReworkStarted` 子类，旧 Started 编码不变。已有返工后，普通 Started 在 failed/cancelled 等阶段也被拒绝，不能从原 baseline 绕过一次限制。预留即消费，失败或取消不归还。纯 reducer 对旧控制请求仍先作 HISTORY_ONLY；存储中的 managed Started 和 TaskReworkStarted 控制请求须经当前原子 pair 预留，旧控制请求拒绝不写。legacy Started 入口保持原行为，旧运行时观察仍正常留历史。返工后的 Prepared 要求 start_commit 为已绑定旧候选，task_baseline 和原 policy 保持；没有新 phase、SQL 列或迁移。完整来源与准入见 [返工说明](task-rework.md)。

## 终态、退出和停止未确认

模型回合终态与服务进程退出是两个事实。当前 RuntimeAdapter 在 Codex 回合成功后可通过 close_finished/terminate 关闭服务进程，因此实际退出码可能是信号负码。任务状态保存该原始数字，不因非零码重新判定模型成败。后续测试执行器对项目测试命令的 exit 0 门槛是另一项规则。

Completed 之后没有实际 ProcessExited，或最近核对仍不能确认停止，任务继续等待交付证据。TaskReconciled 确认停止不能补造未知退出码，也不能把 awaiting_delivery 悄悄改为自动重试。

CLI/adapters 边界须识别“没有模型终态且停止未确认”的诊断，例如 `adapter.cleanup_incomplete`。它不能作为真正终态 Error 喂入状态机；可以用 `TaskDiagnostic(Error(...))` 保存，只有历史，无状态推进。既有 review runner 已在终态输入前过滤此情况；后续实现 runner 必须同样在边界处理。core 信任已经分类的事件，不再次读取原始 CLI 字段作防御性分类。

运行中直接收到未经边界归一的退出、没有终态也没有取消/恢复依据，不在合法转移表内；调用方应先按运行时合同生成真实失败或停止证据。

## SQLite 与恢复

新增迁移 `0003_tasks.sql`，旧迁移不变：

- task_runs：每次运行的不可变完整输入、当前阶段/尝试指针和范围阻塞。
- task_attempts：每个尝试的请求、运行记录、最近核对。
- task_history：顺序事件、时间戳和 applied/history_only。

`TaskStore` 继承 ReviewStore 以复用连接和迁移；不改旧审核的状态语义。接口：create_task、load_task、load_task_input、apply_task、load_task_history、list_recoverable_tasks。重复 run_id 的创建拒绝，不能覆盖旧输入。

apply_task 在同一 `BEGIN IMMEDIATE` 事务中读取当前尝试、执行纯转移、写状态/尝试并追加历史；失败全部回滚。连接继承 `isolation_level=None` 和5秒锁等待，同一run的两个启动竞争者中只有一个成功，另一个等锁后读到新状态并因非法重复start抛 ValueError。跨run全局单执行者锁由后续runner实现，不把本卡的同run事务误称全局调度。

跨多表读取使用一致读事务。codec 显式构造已校验数据，缺失字段/非法枚举直接抛；依据 AGENTS 的边界规则，不在 store 重跑 JSON Schema/YAML，不静默修补或用默认值填证据。本卡不承诺抵御用户直接篡改数据库。

重启只读库和展示，不启动模型。list_recoverable_tasks 列出本卡全部七种 phase 的待处理run（包括ready、等待交付和等待规划）；它是工作清单，不能作为“允许重试”的标志。实际重派仍要当前尝试核对和状态表允许；后续工作树边界负责产生可追溯的检查事实。

## 回放

`tests/replay/scenarios/tasks/` 固定包含 retry-late、cancel-diagnostic-exit、restart-reconcile、cancel-terminal-exit。每个场景分别执行两次纯 apply_task 折叠和两次 SQLite 回放，比较每一步完整状态、原始事件时间戳与精确历史 disposition。已有四个阶段0行为及其余回放保持原样。
