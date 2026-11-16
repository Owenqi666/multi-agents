# 运行实例的资源收据

`RuntimeAdapter.ownership_receipt()` 返回该实例在调用时刻的不可变宿主事实，不等待、不发信号、不执行清理。`ImplementationRuntimeAdapter` 继承此接口；独立探测步骤不冒充 RuntimeAdapter 收据。收据绑定随机 `owner_id`，新建 adapter 不能接管上一次运行的证明，也不能从日志、PID 或模型文本重建所有权。

## 四项独立结论

每次 `check_environment()` 和 `submit_turn()` 在产生资源前登记 operation，直到 finally 清理及日志关闭完成才解除 active。工厂创建前登记 acquisition，区分 pending、明确未创建、实际对象已发布、创建结果未知。所有 version、auth、App Server 和模型进程都归同一个实例，不仅记录最后的模型 handle。

- `processes_stopped`：已有 operation，全部 operation/acquisition 已有明确结论；每个实际子进程有退出码、受管进程组已确认消失、watcher 和 readers 已结束，隔离 owner 没有未停止的进程。
- `resources_released`：在进程停止之外，所有 Codex 隔离目录和预检临时 cwd 均已实际移除。
- `logs_sealed`：至少一份日志，每个 operation 预留的日志均实际创建，全部成功关闭且健康，原文件身份、模式、大小及内容 hash 核对通过。新操作在日志创建前失败也不能借用旧日志。
- `outputs_complete`：所有实际进程的 stdout/stderr 都读到了真实 EOF。取消 drain、reader task 已结束或父进程退出均不能替代 EOF。

四项全正只是后续 runner 接受产物的必要条件。它不代表模型成功、不替代结构化产物验证，也不自动接受或合并代码。已知没有创建进程的失败操作可以如实满足部分资源结论，不能据此产生成功模型结果。

## 进程停止与失败

Process 拿到实际 handle 后先登记本地所有权，再发布给 ledger；正常启动和取消期间完成创建使用相同路径。发布失败仍清理实际 handle，无法确认的创建保留 unknown。Codex owner 在写 auth/config 和发日志前发布，真实移除成功才记录 removed。

对受管进程组，TERM 后最多等待一个 grace，KILL 后再等待一个 grace；两次均同时确认实际子进程退出和组不存在。停止失败后的 watcher 等待也最多一个 grace；已确认停止时，允许 Process 原有输出 drain 预算再加一个 grace 完成 reader 收尾，避免与 drain 截止同时到期。默认 runtime grace 为两秒，可注入；这些是等待预算，不保证操作系统调度的精确墙钟上限。拒绝信号或组仍存在时保留所有权。主动脱离受管进程组的后代不在该证明范围。

明确的停止/输出清理未确认使用 `ProcessCleanupIncomplete`，保留已经观察到的模型终态及实际退出事实。普通协议错误、畸形 JSON 和重复终态继续报告失败，不能为了取得完整收据吞掉。收据不会把清理错误改成模型 TIMEOUT。

## 日志封存

Recorder 只累计成功写入并 flush 的脱敏 UTF-8 字节、记录数及 SHA256。序列化、write、flush、close 失败会永久标记不健康。文件还在写时不读取或 hash；关闭后核对原 regular file 的 dev/inode、模式、字节数、hash 及读取前后的稳定元数据。同路径替换、软链、追加、截断、改写或模式变化均不能 seal；已观察到篡改后恢复文件也不能恢复健康。

快照保留当时的值。以后再次真实清理可产生新的资源事实，但不会改写旧快照或使失败日志重新健康。持久化、当前尝试事务门禁和产物采纳由后续 planning runner 负责；T37 仅提供实例内收据，没有执行真实产品权限探测。
