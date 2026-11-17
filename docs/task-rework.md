# 一次原范围返工

返工沿同一个 task run、新 implementation attempt，从被审核的当前候选创建工作树。原任务 baseline、完整 TaskInput、规划 provenance、规则和执行策略保持。修订后的候选以旧候选为 parent，交付范围仍按原 baseline 的累计改动核验。

## 准入与一次限制

来源须实际核验通过，所选审核报告齐全、无实质分歧，并存在引用已核对、明确位于原允许范围的 P0/P1 问题。所有拟处理阻塞问题都必须符合原 write scope 和 protected_paths。单方有充分引用的阻塞意见可以处理；引用 verified 证明源行匹配，不把诊断变成宿主证实的事实。

仅测试非零而没有明确可定位的原范围问题、未核引用、范围不明或存在实质分歧时，返回待用户判断，不自动启动写入。已知原预算耗尽返回 `budget_exhausted`；这些政策拒绝以退出 1 和结构化结果展示。

专用预留事务成功即消耗唯一返工轮次。返工也占原 max_attempts 预算；失败或取消后不能通过普通 retry 再写。预留之前发生的范围、身份、预算、交接尺寸等拒绝不消耗轮次。预留提交后宿主报错仍保留实际消费，不自动删除补偿或再次启动。

## 来源与原子预留

`TaskReworkSource` 冻结原 task/implementation/test/review 坐标、完整 TaskInput 的 hash、candidate commit/hash、原 task/test 完整历史检查点，以及审核/确认 checkpoint 和原 assessment。已有 v2 大型快照只引用 outer ID，不重复写入事件。

审核 checkpoint 同时绑定完整 RoundState、round_inputs 的 snapshot/parent 关联、metadata、execution 和完整有序 history。确认轮不存在也是明确的绑定值；后来新增确认轮会使原 CAS 失效。

`TaskReviewStore.reserve_task_rework_execution` 在同一个 `BEGIN IMMEDIATE` 中核所有当前来源、关联和历史，再原子写 `TaskReworkStarted` 与 `TaskExecutionPrepared`。普通事件入口不能代替这个控制事务。新事件使用既有 JSON 历史和尝试表，没有新 TaskPhase 或 SQL 迁移。

executor lease → 原 review execution lock 的顺序保持至实际控制器结算。运行器复用同一受保护的 worker、取消、退出、清理与封存逻辑。冷检查仅重建已经绑定的不可变交接，不在新尝试开始后要求旧候选仍是 current，也不刷新原授权检查点。

## 内联交接与测试

`rework-managed-v1` 内联包含原完整任务、准确 diff、带原来源的意见，以及相关失败命令的完整名字、argv、manifest 和 stdout/stderr bytes。二进制数据沿 hex 编码保留，模型不能根据其中的宿主路径读取外部记录。原实现 prompt 字节与原生权限配置保持。

最终实际 prompt 的 UTF-8 编码与实际序列化 context manifest 各自最多 8 MiB，包含 hex 和 JSON 转义开销；超限在预留前拒绝，不截断或摘要替代。这个上限不保证操作系统可以发送所有较小输入：现 Claude 通过 argv 接收 prompt，实际启动仍可能报 E2BIG。真实预留后启动失败按原运行器结算并保留消费记录。

修订后测试使用原审核输入的完整、有序 NamedCommand。比较的是冻结规则中的名称、executable 字面值和 args，包含顺序及同 argv 的不同名称；不比较新工作树里的绝对路径或可执行文件身份。CLI 提前校验，测试预留事务再次核对。新候选/实现/测试坐标和历史必须更新，declaration_source 可以记录本次真实声明。

旧测试与审核不能作为新候选通过的证据。返工命令只固定新候选，必须显式再执行完整测试与新审核。没有自动接受、合并或第二轮返工。
