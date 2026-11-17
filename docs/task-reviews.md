# 同候选审核

`task-review` 将当前候选、原任务约定和实际执行的具名测试冻结成一个审核输入。单审、双审、失败席补跑及交叉确认都使用同一份输入。先完成 `task-test run`，再执行：

```bash
uv run python -m multi_agent task-review run TASK_RUN_ID \
  --database /absolute/data/runs.sqlite3 \
  --implementation-attempt 1 --test-attempt 1 \
  --repository /absolute/repository --directory /absolute/data/review-1 \
  --seat codex MODEL EFFORT --seat claude OTHER_MODEL EFFORT

uv run python -m multi_agent task-review show ROUND_ID \
  --database /absolute/data/runs.sqlite3 --history

uv run python -m multi_agent task-review retry ROUND_ID \
  --database /absolute/data/runs.sqlite3

uv run python -m multi_agent task-review confirm ROUND_ID \
  --database /absolute/data/runs.sqlite3
```

选择一个或两个不同模型，provider 为 `codex` 或 `claude`，模型和 effort 均需显式提供。审核目录必须尚不存在，与数据库同父目录，并位于所有实现工作树和 Git 元数据之外。默认 timeout 为 180 秒、grace 为 2 秒；可用 `--codex-bin`、`--claude-bin` 选择可执行文件。

`retry` 仅补跑失败或尚未开始的席位，保留原配置和输入；可用重复的 `--seat KEY` 指定席位。中断后遗留 RUNNING 状态沿既有规则要求显式 `--recover`，不能把恢复视为旧进程已停止。停止仍未确认的运行保持阻塞。成功席位不可重复运行。

双审存在 P0/P1 问题时可执行一次交叉确认，各席位核对对方的问题。单审不支持 `confirm`。已有确认轮次的失败席使用其 round ID 执行 `retry`。交叉确认不是实现返工，也不自动接受或合并候选。

## 输入与证据

审核输入版本为 `executed_task_review` v2，包含原始完整 TaskInput、冻结规则、原 baseline 到准确候选的 diff/files、完整测试准备记录、逐命令实际 manifest，以及 stdout/stderr 原始 bytes。名称不同而 argv 相同的命令仍各自保留序号和结果。每个审核者看到相同的完整输入 hash、只读源文件和只读二进制日志；给模型的 JSON 通过相对路径引用这些日志，不依赖宿主可变日志路径。

完整 canonical UTF-8 输入预算为 128 MiB，包含 bytes 的 hex 编码膨胀；超过预算明确拒绝，不截断。冻结前后的候选、数据库和原历史检查点必须一致；绑定 snapshot、round 和 round input 在同一个事务中完成。创建提交后发生宿主异常会保留实际记录和目录，不自动重新启动审核。

只有完整结束、每条命令实际执行且停止范围已确认、所有归档合格的套件可进入此入口。正常非零退出可进入审核，评估保留 `needs_rework`；缺命令、未知停止、取消、超时或不合格日志不能当作完整执行证据。测试执行证明沿用 T50 的范围：服务端实际发送的输出、命令请求和自有运行时资源的停止。任意后代停止和生产者输出 EOF 仍未确认。

模型、effort、用量及改派信息只记运行时实际报告；请求值不补齐未知值。旧 `FrozenSnapshot`、四字段 `TestEvidence`、v1 prompt 和 `user_supplied` 语义保持原样。旧执行入口拒绝 v2 输入；已有旧审核仍按原流程工作。

## 当前资格和历史报告

`show` 以只读方式打开现存数据库，不创建数据库、目录、锁文件或迁移。它保留历史报告，并显示 task、implementation、test 坐标、candidate、`source_current`、`verification` 和 `current_eligible`。只有重新核实当前候选、原历史检查点和实际归档后，历史结论才可成为当前结论；新尝试、负向来源事实或归档变化会撤销当前资格。审核输入本身保持原 bytes。

创建时 task/test 完整历史必须精确匹配。之后 test 历史只允许原前缀未改且新增项全部为 HISTORY_ONLY；原实现历史检查点不刷新。活动执行者持有共享 lease 时，`show` 仍可展示数据库历史，但当前核验保持未确认。旧通用 `show` 对 v2 也只展示历史，不能提升当前资格。

`run/retry/confirm` 返回 0 仅表示当前来源已核验且整体 assessment 为 ready，1 表示负向、分歧或不完整，2 表示输入或宿主错误，用户中断在结算后返回 130。确认轮次的整体 assessment 来自父审核。`show` 成功读取返回 0，其退出码本身不表示候选可验收。

离线功能检查使用临时 Git、SQLite、实际小型测试命令和 fake reviewers。原生写权限与测试沙箱仍需要 S9 的具体 canary 验收；本入口不增加真实探测授权。
