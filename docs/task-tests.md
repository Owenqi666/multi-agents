# 候选测试执行与结果

## 运行与查看

T50 提供 `task-test run/show`，输入为已经核验、冻结到数据库的任务候选。下面的名称必须已经存在于该任务的原始冻结命令注册表中：

```bash
uv run python -m multi_agent task-test run TASK_RUN_ID \
  --database /absolute/data/runs.sqlite3 --implementation-attempt 1 \
  --repository /absolute/repository --directory /absolute/data/test-attempt-1 \
  --command build --command unit \
  --declaration-source '本次明确选择的完整测试集合'

uv run python -m multi_agent task-test show TASK_RUN_ID \
  --database /absolute/data/runs.sqlite3 --implementation-attempt 1
```

`--directory` 必须尚不存在，且与数据库同父目录、位于原工作区和 Git 元数据之外。命令完整 argv 从冻结 registry 取得，不支持临时 shell 字符串或改写参数。需要额外工具时用 `--tool-directory` 指定目录，读取运行时依赖用 `--runtime-read-path`；路径会冻结到本阶段策略。运行器不安装工具或下载依赖。

同一尝试中的所有命令共享独立检出及 `scratch/home`、`scratch/tmp`、`scratch/cache`，因此 build 的产物可以交给下一条 test。检出只包含候选原始 commit 和完整 tree/blob 对象，保留二进制、可执行位和内部 symlink；独立 `.git` 的 shallow 边界停在候选，无父历史、tags、remotes 或 submodules。依赖这些内容的命令会按真实结果失败。

结果包含候选、整套状态、每条命令名称、报告退出码、可确认的映射退出码及 stdout/stderr/wire 日志路径。`verification=verified` 表示当前归档和来源核验通过；失败命令的证据也可以被核验。读取到历史 `succeeded` 不会绕过现有文件核验。缺失或篡改归档显示 `unverified`，未执行的命令显示 `not_run`。

`run` 返回 0 仅表示整套成功且证据核验通过；已结算的失败或不完整返回 1，输入或宿主错误返回 2，用户中断在实际结算后返回 130。`show` 成功读取返回 0，使用只读 SQLite 连接，不创建或迁移数据库，也不恢复旧进程。它会显示保留目录；活动 lease 无法取得时只展示持久状态，核验保持未确认。

显式重跑需要新目录，且前次已经逻辑结束、所需资源实际停止；整套声明重新运行，策略与命令保持冻结。成功尝试不允许重复派发。日志损坏不会抹去独立核实的停止事实，但也不能使测试通过。未确认停止时，测试与实现两条路线都阻止重新派发。

## 执行证据的范围

每条命令使用一个短命 Codex App Server，只做版本、配置审计和 `command/exec`，不建立模型回合。实际观察必须为 `codex-cli 0.169.0` 才执行；请求版本不能代替实际报告。宿主认证位于临时隔离根，命令的 HOME、TMPDIR 和缓存另设在该尝试的 scratch 中，CODEX_HOME 从子命令环境移除。命令权限固定为检出与 scratch 可写、独立 `.git` 只读、明确运行时目录可读、网络关闭。

**当前原生执行与权限行为仍待 S9 的具体 canary 实测。** 离线 fake 会实际运行微型 build/test，但不是 macOS 权限拒读证明。`python -m multi_agent.adapters.test_probe prepare --output-dir PATH` 生成可审计划，不运行 CLI 或读取认证；真实 `run --plan ... --case native-test-boundary` 按 runtime-protocols 的具体授权和用户终端流程执行。

退出报告按固定版本源码解释：0–255（124 除外）可映射为运行时规范化退出码，其中大于等于 128 既可能是普通退出也可能是信号映射，不能猜原因。124、-1 及其它不支持值保留原报告，可确认退出保持未知并记录问题；只有宿主实际 deadline 到期才记录 TIMEOUT。

停止范围 `command-request-and-owned-runtime-v1` 要求匹配最终响应、请求结算，以及所有登记的宿主辅助进程、App Server、自有组、reader/watcher 和 acquisition 结算。它不声称内部命令私有进程组或任意脱离的后代均停止。尚未触及命令请求的预检失败，只要宿主所有资源确实停止，可以负向结算；已尝试发送请求而没有 final 时仍为未知。

日志范围为 `server-emitted-notifications-v1`：保存实际收到的二进制 stdout/stderr、wire、配置观察与范围收据。服务端有自己的输出 drain 限制，因此生产者 stdout/stderr EOF 始终未确认。输出 cap、本地预算或写入/封存故障使证据不合格；不把 App Server 的 EOF 当成命令退出。

T50 的 Prepared 与 Reported 事件保存在现有 JSON 历史中，没有新迁移。Started+Prepared 原子预留绑定原始 task/test 两份历史 checkpoint、候选、独立检出、scratch、策略和日志根。每个命令必须在新鲜作用域中先预留，再消费一次启动许可；冷 DB/manifest 不能重建该许可。一次性归档重新核对实际日志、完整准备记录及原历史前缀。

同候选审核与至多一轮返工将在此执行入口之后接入。旧四字段 `TestEvidence` 和 `user_supplied` 来源保持不变。

## 输入与状态设计沿革

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

T49 提供下述事务与纯状态，T50 接入上面的执行流程。不能从输入或状态记录存在推断测试实际运行过。旧 FrozenSnapshot、四字段 TestEvidence 及 user_supplied 含义保持原样，版本化执行来源在后续审核桥接中处理。

## 原子登记与完整历史

测试阶段以 `(task_run_id, implementation_attempt)` 唯一标识。`TaskTestStore` 的两个专用入口分别预留整个测试尝试和下一条命令；普通观察入口拒绝这两个 Started 事件。首次预留在同一个 `BEGIN IMMEDIATE` 中创建 stage、attempt、current 和 history，不留下空 stage。

每次预留同时核对当前实现和完整候选、原 TaskInput 与命令注册表、原实现历史，以及测试阶段的当前尝试和完整历史。命令预留还核对当前序号。历史包含 HISTORY_ONLY，不能刷新输入的 checkpoint 来绕过变化。重试的新输入只能改变两个实现历史 checkpoint 字段；其它实际字段按 canonical JSON 比较，原始声明、命令顺序和候选执行锚点均保持。

事实观察与执行授权分开：即使候选资格已撤销，当前实际退出或停止仍可记录；合法旧实现、旧测试尝试和旧命令观察只进历史。未来或尚未开始的序号拒绝。CancelRequested 与 AttemptFinished 是整个测试尝试的事件，没有命令序号，不能把缺失字段当作旧 preflight。

## 逻辑结束、实际退出和停止

每个命令按声明顺序分配序号，名称不同但 argv 相同仍分别执行和记录。实际非零退出保留，可在核实停止后继续余下命令，不隐含 fail-fast。下一条命令开始时，当前停止与收据变回未知；上一个命令的停止不能授权新命令的结算。

Finished 只表示不再开始新命令，不填补缺失退出码或停止事实。Finished 后，实际非零退出或 Problem 使尝试 FAILED；全部声明命令都有实际零退出、每条最新收据合格且当前 epoch 收据合格，才能得到 SUCCEEDED。其它情况是 AWAITING_EVIDENCE。未 Finished 时通常保持 RUNNING。

取消一旦接受便锁存。未 Finished，或当前 epoch 停止仍未知/为假时，状态为 CANCELLING；只有 Finished 且实际停止为真才为 CANCELLED。晚到的零退出不会撤销取消，实际退出值仍保留。FAILED 可以伴随停止未知；任何 phase 都不能代替停止收据。

停止保留 `None/False/True`。合格收据要求停止为真、宿主实际核验的 `evidence_qualified=True`，并有完整路径与 SHA 对。路径与 hash 存在本身不证明归档合格。App Server 退出、EOF、终止请求确认或仅自有进程组消失，也不能自动提升成所需范围内所有资源均已停止。

Finished 后同一当前 epoch 的事实仍可到达。成功后的负向收据立即撤销通过资格；后续真正合格的同 epoch 收据可以恢复。失败事实和取消锁存不能靠正向收据复活为成功。命令已经推进后，旧序号的补充收据只进历史，因此要获得整套通过证据，前序命令必须在推进前完成收据核验；否则需在实际结算后显式重试全套。

## 持久化和后续边界

迁移 0005 只新增测试阶段、尝试与历史三表。旧表与旧编码不重写，SQL 不另存 phase；纯 fold 与冷启动加载都调用同一个 `task_test_phase` 从已存事实派生。提交前校验或写入失败整体回滚；提交后的外层异常不能证明未提交，调用方须按原 envelope 回读历史，不能自动重新启动或删除补偿。

`task_test_ready` 仅是当前状态与候选来源门槛。后续执行、归档与审核消费仍须在实际 lease 下核对文件、来源、权限与所需停止范围，再以事务检查当前记录。T49 的离线测试使用合成宿主事实，不构成实机执行、沙箱或完整进程树停止的证据。
