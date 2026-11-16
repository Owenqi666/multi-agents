# 规划上下文与启动占位

T36 将“这次规划会读什么、按什么预算运行”保存到规划尝试，供下一步只读 runner 使用。本卡只提供内部 Python API，不接用户 CLI 命令，也不启动预检、模型、测试或实现工作树。原始需求、规则、模型请求、依赖和用户决定继续来自 T34 的 PlanningInput。

## 持久记录

`PlanningExecutionPrepared` 是新的宿主事件，记录以下四项：

- `source_snapshot_id`：数据库中内容寻址的固定基线源码。
- `policy`：两家可执行文件的规范绝对路径、device/inode、SHA256、各自最大尝试次数，以及 timeout/grace。
- `run_owner`：外部运行根目录的路径、身份、随机 token 和不可变 owner manifest hash。
- `attempt_context`：本次独占目录的路径、身份、新 token、完整内容与模式 digest，以及 manifest hash。

策略中的 timeout 是后续 runner 预检与模型共用的预算；grace 用于清理。T36 冻结并保存这些值，尚未执行计时。预检失败也占一次 provider attempt；重启不重置次数。没有模型实际报告时，Started.environment 仍为 None。实际环境观察和运行时停止收据由后续卡接入，不能把请求或目录锁状态填成运行证据。

可执行文件 hash 绑定该文件本身，不覆盖解释器、动态库或其所有依赖；后续仍需运行时版本与配置审计。

数据放入已有 attempts payload、events 和 history，不修改旧 PlanningInput、PlanningAttempt 或 PlanningAttemptStarted 的字段。没有 SQL 结构变更或迁移。旧绑定来源按原编码继续计算同一 plan_hash；新 Prepared 随新尝试进入来源 hash。

## 转移与事务

Prepared 首次只能紧接当前新 Started，且没有任何运行事件或预检观察。后续阶段和重试必须保持首个 Prepared 的 source、policy 和 run owner；上下文路径、身份和 token 必须全新。缺少完整 Prepared 链的 legacy 尝试不能被新执行入口自动接管。相同事件的重复只进历史，不给调用者再次启动许可；同一当前尝试的不同准备记录拒绝。旧阶段/旧尝试的晚到事件沿用历史隔离。

`PlanningStore.reserve_planning_attempt(run_id, started, prepared)` 在一个 BEGIN IMMEDIATE 事务里核对源码快照、现有状态与次数，依次应用 Started 和 Prepared，再写状态与两条历史。两条都必须 APPLIED。任何一步失败全部回滚；双连接竞争只能有一个获得当前尝试。源码快照先保存，事务失败留下未引用快照无害。

占位提交后才允许后续 runner 调预检。若控制器此时崩溃，数据库仍有未确认停止的尝试；重启不能当它从未运行而自动重发。最新 reconciliation 缺失或 `process_stopped=false` 都不满足重新派发。测试用已有合成终态、真实形状的 ProcessExited 和 PlanningReconciled 建立“已停止”前提；不靠 PID 不存在或锁可获取作推断。

## 文件边界

首次准备从实际 source worktree 核对 Git common identity 与冻结规则 blob，源码始终使用已经解析的 rules.baseline_commit 全 OID，作为 snapshot 的 baseline 和 candidate。HEAD 或 dirty 文件之后改变不影响输入。后续阶段和重试只从数据库读取相同 snapshot，不重新读取活动工作树或规则。

run root 仅首次新建，位于 source worktree、common Git 和 linked-worktree admin 目录之外；不能自动采用首次占位前遗留的未绑定目录。后续按首个持久 owner 核对并复用 root，每次新建独占 context。owner manifest 绑定 run/input/source/policy，attempt manifest 另绑定 stage、attempt 和上下文身份。日志保留在模型 cwd 之外。

源码视图复用既有 materialize：没有 Git 管理目录和可执行位，软链是惰性文本；二进制和 gitlink 以遗漏清单说明，不伪装为完整测试环境。宿主额外写 planning-input.json、output-schema.json，复核时写精确的 task-card.json。内容、模式和目录身份在占位与后续检查时核对，符号链接和替换拒绝。chmod 和 digest 是完整性检查，不是沙箱实测结论。

准备失败的残留目录保留，不能覆盖后继续调用；未知停止也不自动删除 cwd 或 owner。本卡不实现进程重附着、取消、自动恢复或清理。后续 runner 必须在实际调用前再核对上下文与可执行文件。

## 提示词

Claude draft 使用 task-card schema，Codex review 使用 REVIEW_SCHEMA。两者的 versioned prompt 都指向原始需求、冻结规则、依赖、用户决定与固定源码，并说明遗漏内容。review 绑定当前 draft attempt 和 task_hash；模型提出的问题或 resolution 不冒充用户答案。

仓库文件中的指令作为被分析的数据。规划不执行项目代码、测试或 Git，也不将 proposed_tests 当已获认可的命令。源文件是否可见、提示词是否禁止执行与运行时权限是否实际生效是不同证据；本卡只验证前两项，后续运行器沿用独立 READ_ONLY 会话。

本卡不自动生成 needs_revision 的修订反馈文件。后续 runner 接入修订时，需要明确绑定前次复核反馈，仍与用户决定区分，并继续记录本次精确提示词。运行器还需实际环境观察、全部受管资源的停止证明和已关闭日志的收据；Completed 加父进程退出码不能替代这些证明。
