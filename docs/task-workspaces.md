# 实现工作树与候选提交

`cli/workspaces.py` 提供阶段2的宿主文件服务，`core/workspaces.py`保存不可变记录。本模块不启动模型、执行项目测试、推进任务状态或自动合并；调用者负责保存记录、确认实现进程已停止，并在整个实现生命周期保持同一个 lease。

## 生命周期

```python
with executor_lock(repository) as lease:
    record = create_workspace(lease, task_baseline, destination)
    # 后续 runner 在 record.root 中实现，确认停止后调用：
    delivery = freeze_delivery(lease, record, card, frozen_rules)
    remove_workspace(lease, record)
```

锁文件位于规范化 Git common-dir 的 `multi-agent-executor.lock`，采用非阻塞 OS flock。主工作树、linked worktree和不同运行目录共享这把锁。锁文件保留，避免unlink后出现不同inode的两把锁；每次操作核对持锁FD与文件身份。lease过期或锁文件被替换即拒绝。现有双审核器不受此实现锁约束。

目标必须不存在，现存父目录解析后不能位于同仓库任一已登记工作树或Git目录内。记录保存原任务baseline、工作树起点start_commit、root/admin目录dev+inode、`.git`/HEAD/commondir/gitdir/config.worktree原字节与文件身份。省略start_commit时使用原baseline；返工起点必须为原baseline后代。恢复需要持久化的完整record，不根据路径或owner名字猜测所有权。

创建使用`worktree add --detach --no-checkout --no-relative-paths`，宿主从Git blob物化原字节和执行位，再用不带`-u`的read-tree初始化独立index。二进制保真。所有Git调用都是参数数组，并禁用hooks、fsmonitor、稀疏检出、自动维护和签名，沿用snapshot的无系统/全局配置、无替换对象、无惰性拉取和超时控制；不调用checkout filters、git add或外部diff。

## 文件快照与范围

扫描包含全部普通文件及符号链接，包括忽略和未跟踪文件。通过目录FD递归、O_NOFOLLOW打开普通文件后fstat再读取；符号链接只读目标文本。特殊文件（FIFO/socket/device）、目录形式`.git`、不合法UTF-8/path、大小写或Unicode别名碰撞明确拒绝。容量沿用snapshot的20,000文件、8MiB单文件、64MiB总量；额外限制256层目录和80,000扫描条目，防止空目录绕过文件数量限制。

相对symlink按捕获的完整文件图逐段展开，包括路径前缀中的其他symlink，再处理`..`；路径比较使用NFC+casefold。绝对、越界、Git内部目标、循环或超过40次展开拒绝，允许留在树内的悬空相对目标。Gitlinks不支持，不会省略后继续创建。

快照的文件path/mode/content不可变，digest包含各文件内容SHA256。它不是全树同一时刻的原子快照；调用者应先停止执行者。冻结候选只用该次已校验bytes，不再读取工作树正文。即使检查后文件发生变化，发布候选仍对应快照，不能把晚到文件悄悄带入。

最终路径集合始终与原任务baseline比较，覆盖新增、删除、正文、mode变化及rename两侧（按删除和新增处理）。所有最终产物都计入，无缓存排除表：范围内忽略文件也明确进入changed_paths和candidate，范围外则拒绝。空目录不是Git补丁，检查不能证明没有临时越界写入。范围判定复用T29的path_matches，实际文件名不是glob。任何write_scope外或protected_paths内的改动抛DeliveryRejected，保存OUT_OF_SCOPE_PATCH分类及全部拒绝路径。其他文件/Git/所有权错误抛ValueError；底层原因保留cause。

## 固定与清理

候选通过hash-object --no-filters、mktree、commit-tree构造，父提交固定为record.start_commit。宿主作者/提交者为`Multi-agent <multi-agent@localhost>`，时间由Git生成并保存在对象中。用户已有分支、index和源文件不改。卡、冻结规则、仓库和原baseline必须一致；返工不能通过新起点隐藏此前越界改动，也不从候选重读规则。

新增ref为`refs/multi-agent/candidates/<owner-token>`。先拒绝现存loose entry和namespace symlink，再用`update-ref --no-deref <ref> <candidate> <zero-OID>`比较创建，已有普通/packed ref由Git CAS拒绝。**本机实测Git把悬空symbolic ref视为零OID**，因此显式loose entry检查不可省略；它与后续CAS之间不提供对抗外部任意Git元数据写者的原子保障。lease协调本工作台的宿主执行者；`--no-deref`阻止写入转向用户分支。这里没有宣称权限沙箱，后续写权限验收必须另外验证Git元数据保护。

重复冻结同一owner拒绝，不覆盖旧证据；新候选使用新工作树/尝试。清理前再核对root及控制文件身份；未知、被替换或篡改的目录保留并报错。创建中途失败清理已核实的自有工作树；尚未建立所有权时只删除仍为原inode的空目录。清理失败附加exception note，保留原异常和路径。工作树清理后candidate ref仍保留，供测试与审核从同一提交取输入。

本卡验证仅使用临时Git仓库和模拟写入，不是模型写权限验证、项目测试执行器或阶段2端到端验收。

执行者只编辑源文件，不能自行`git commit`、checkout或reset：这些操作会改变冻结的HEAD控制文件，使freeze/remove拒绝。候选提交只由宿主构造；后续runner必须把这个约束传给执行者。出现控制篡改时保留目录供核查，不自动修复HEAD来重新声称所有权。
