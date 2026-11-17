# 独立测试输入

T48 定义候选上的不可变测试输入。`prepare_task_test_input(store, task_run_id, attempt, lease, *, required_commands, declaration_source)` 准备 `TaskTestInput`，不创建测试检出、不执行命令、不登记测试尝试。

## 明确声明完整集合

`required_commands` 是调用方明确声明的完整测试集合，必须是非空、有序、名称唯一的字符串 tuple。名称只能精确匹配原 TaskInput 中冻结规则的 commands 注册表。不同名称即使 argv 相同，也分别保留。原任务卡的 test_refs 是引用子集；proposed_tests 尚未获准，二者都不会自动扩充或替代本次声明。

这里仅记录“调用方声明的完整集合”，不证明已经发现全部项目测试或覆盖所有验收条件。实际运行时缺少完整集合、新增未批准命令或扩大权限，应由调用方解决，不能从当前工作区、候选规则、README 或 package scripts 猜测。

`declaration_source` 是来源说明原文，类别固定为 `caller_declared`。它不作为路径或 URL 读取，也不作为独立核实过的授权证明。空字符串、纯空白、NUL、无效 Unicode 拒绝；合法文本的前后空白原样保留。

声明预算是仅含 `required_commands` 与 `declaration_source` 的 canonical JSON 的 UTF-8 字节数：使用现有 task_execution_json 的排序和紧凑编码，无末尾换行，最多 1,048,576 bytes。恰好等于限值允许，多字节字符按实际字节计。这个限值不表示整个嵌套任务或候选对象都被限制为 1 MiB。

## 候选与来源冻结

边界先校验参数，再捕获当前 managed 候选状态、原 TaskInput、包含 HISTORY_ONLY 的完整历史 H，以及实际 DB/lease 身份。命令名从原冻结 registry 解析后，调用 T47 的实际 candidate inspector。最后仍检查最初 H、候选和所有身份，没有重新读取新历史来刷新条件。

调用方在准备与后续依赖操作期间持同一个有效 common-dir lease 和实际 store；可以在原实现作用域结束后取得新 lease。该过程只读，不恢复旧运行时 owner，不重新 freeze/seal，不释放调用方的 lease。

输入包含版本、任务运行与实现尝试、task/rules/full TaskInput hash、完整 TaskCandidateFrozen、具名命令顺序、来源原文和原历史 checkpoint。候选的 baseline/tree/ref/digest 从完整事件读取，候选 hash 为派生值。全部实际字段进入新的 input_hash，未知运行时值继续保持未知。

## 后续登记与执行边界

准备好的输入不是可跨时间复用的执行许可。后续独立测试登记仍须实际重新核候选，并在同一个事务里核当前实现、完整候选、原 TaskInput、registry 中的名称和 argv、本次原历史 CAS，以及测试阶段自身的当前尝试与唯一预留约束。

本卡不提供该事务、测试执行器或执行证据。命令退出码、停止、日志完整性、独立检出与隔离资格由后续卡分别记录；不能从输入存在推断测试已通过。旧 FrozenSnapshot、四字段 TestEvidence 及 user_supplied 含义保持原样，版本化执行来源在后续审核桥接中处理。
