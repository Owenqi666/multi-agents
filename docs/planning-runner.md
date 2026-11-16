# 单次规划运行器

`execute_planning_attempt(database_path, run_id, stage, *, repository=None, directory=None, policy=None, stop=None)` 执行一个新 draft 或 review attempt，返回持久化的 PlanningState。它是内部 API，没有自动重试或循环；产品 CLI、实现执行、独立测试和返工在后续卡连接。

首次执行使用 `prepare_planning_attempt` 冻结源、规则、策略和上下文。后续尝试沿用同一运行的策略和源，每次获得新的 context/token 并消耗对应 provider 的次数预算。模型请求取 Started，执行文件、timeout/grace 取 Prepared，schema 从已核验的 `output-schema.json` 读取，cwd 为该上下文，权限为 READ_ONLY。

## 串行控制与停止未知

数据库必须是已有的 canonical 常规文件，与本运行根目录位于同一 canonical 数据父目录。协作锁放在该父目录中，采用无软链独占 flock 和文件身份检查，覆盖扫描、准备、预检、模型执行、清理和最终持久化。

启动前扫描同一数据库中未 bound 的 managed Prepared 尝试。没有 Reconciled 或最新 `process_stopped=false` 都是停止未知，会阻止新 prepare 和 spawn。只能执行当前控制器持锁期间刚准备的 attempt；不能接管既有 Prepared，也不能用新空 adapter、锁释放、PID 查询或磁盘残留补停止事实。

Prepared 后、spawn 前崩溃同样保留未知。日志目录或 adapter 构造失败，若没有实际 operation 也不补 stopped=true。本卡没有解除未知的恢复命令，未来 S8 才提供明确人工核对入口。

已设置 stop 且尚未 reserve 时直接返回原 state，不消费 attempt 或创建 adapter。

## 预检、运行与证据

每个 attempt 使用一个新的 RuntimeAdapter；预检前计算一次 `loop.time() + timeout`，预检和模型共用这一截止时间，清理沿用独立 grace。上下文和可执行文件在预检前、模型前、结束后检查。review 准备之前还必须通过 T40 核验当前 draft 的 execution manifest，legacy synthetic 日志不能取得复核资格。

真实 EnvironmentInfo 先写 EnvironmentObserved。明确未登录或已知目录不支持请求的模型/档位时不启动模型；未知信息保持 null。辅助进程的退出只留在 ownership receipt，不冒充模型 ProcessExited。

模型事件按原值保留，实际模型/档位不从请求补齐。terminal 和 ProcessExited 分别保存；实际成功终态之后的非零退出也保持原值。完整资源结算后调用 T40 seal，只有成功封存且 qualified 时，Reconciled 才保存 manifest 路径/hash。进程停止独立取本实例 receipt.processes_stopped，不与日志或资源释放失败混为一谈。

全正执行证据取得后，宿主才解析当前 Completed.result：草案使用 `parse_planning_draft`，复核使用 `parse_planning_review` 并绑定当前精确卡片。schema 或语义错误产生 ValidationRejected，不改模型 result。证据不完整时，仅 awaiting_validation 允许 EvidenceRejected；其他阶段使用 Diagnostic 和实际 Reconciled，不为完成流程伪造模型终态。

运行器完成当前 draft/review 后，调用者可使用 T40 的 `bind_planning_task(store, planning_run_id, task_run_id, created_at)`。它再次核验精确配对，并通过 expected_plan_hash 在原事务中绑定。运行器不自动派发实现或解决用户问题。

## 取消与写库故障

预检期间优先处理已经完成的结果。pending 预检仅在原截止时间之前、尚未取消且 task 未进入 cancelling 时取消一次；截止时间已到后等待原结算，不追加取消计数将 TIMEOUT 改成 CancelledError。成功环境先保存，再判断认证/catalog；真实失败不会被晚到 stop 改写。

模型事件由独立 consumer 消费，控制器 shield 等待它。反复 caller cancellation 不取消 consumer 或关闭原事件流。adapter.cancel 的同步 on_accepted 先在内存保存接受事实与原 CancelRequested，再写数据库；写库失败不能把接受变成 False，也不能借随后 Completed 取得 validation。取消决定完成后才按原值持久化缓冲的 terminal/exit。

consumer 写库失败时保留首异常和已观察事件，继续 drain 原迭代器直到实际运行 task 结算。`result()` 只补已有真实值，不承担等待或停止证明；不读取私有运行 task，也不声称可以结算任意第三方提前抛出的迭代器。

非终态事件的提交不确定性按完整原 envelope（阶段、尝试、时间和事件）查询历史，并比较失败前已确认写入的次数，避免事务已经提交、返回时才抛错导致同一份实际报告重复写入。从未尝试写入的待存事件逐条保存；真正重复的内容即使遇到相同时间也按观察次数保留。历史读取失败不当作从未提交。

取消回调写库失败后，可有界补写一次同一个原 CancelRequested。补写前先读取当前 attempt 的事件，确认是否已经提交；提交后才抛错的情况不重复记录已应用意图。读取或补写仍失败时，不能将随后 Completed 单独推进为成功。补写不是重新接受取消，也不会解除宿主异常或恢复 validation 资格。

即使原取消意图无法补写，仍独立尽力保存已观察到的 ProcessExited。退出事件本身不能补齐模型终态、进程组停止或 validation 资格；无法写入的原始终态留在实际日志与已观察事实中。

全部清理及可行持久化完成后，有 caller cancellation 时重抛首个 CancelledError，并把首个宿主异常作为 cause；否则重抛首个宿主异常。实际运行时 TIMEOUT 或终态仍按原值保存。若数据库故障使 Reconciled 无法写入，下一次仍按停止未知阻塞，不从文件回填。

已接受取消后实际 Completed 仍可能到达。现有 core 保存该事件的原 result，同时将阶段维持 cancelled；运行器按这个实际阶段处理，不非法追加 EvidenceRejected 或 Validated。

## 证据范围

开发验证使用真实运行的假 CLI、临时 Git 与 synthetic 登录数据；实际子进程生命周期不等于真实模型或权限验证。已有假 CLI 模式、协议快照、真实 fixtures 和检查配置保持。阶段 2 产品模型与权限实机验收仍属 S9。
