# 阶段 2 命令行工作流

工作流是明确的分步操作：需求 → 规划 → 实现及固定候选 → 测试 → 审核 → 至多一次返工 → 新测试和审核。各步返回实际 run、attempt、candidate 或 round ID，下一步使用这些值。最终接受和合并由用户处理。

## 规划

准备权限为 `0700` 的仓库外数据目录。数据库与每个新的业务目录同父目录；业务目录不得已存在，也不得位于任何源工作树或 Git 元数据中。

```bash
uv run python -m multi_agent task plan \
  --database /absolute/data/runs.sqlite3 \
  --repository /absolute/repository --baseline BASELINE \
  --requirements /absolute/requirements.txt \
  --directory /absolute/data/planning-1
```

规则只从指定基线提交的 `.multi-agent/rules.yaml` 读取。原始 UTF-8 需求和规则进入冻结 PlanningInput。Claude 草拟任务卡，Codex 对照原需求复核；失败、待回答问题或需要修改的计划会保留并展示，命令不会循环替用户作决定。

默认请求为 `claude-opus-5-5 high` 草拟和 `gpt-6.1-sol high` 复核。可用 `--draft MODEL EFFORT`、`--plan-review MODEL EFFORT` 修改请求；默认 timeout 180 秒、grace 2 秒、每家规划调用预算 2。`--codex-bin`、`--claude-bin` 选择实际可执行文件。请求模型和档位不是实际报告的证明。

`--declarations FILE` 可提供严格 JSON：顶层必须为 `dependencies` 和 `decisions` 两个数组。dependency 字段为 `task_id/baseline_commit/source/detail`；decision 字段为 `question/resolution/source/detail`；source 必须为 `user_declared`。声明表示用户输入，模型输出不能补成用户决定。

```bash
uv run python -m multi_agent task plan \
  --database /absolute/data/runs.sqlite3 --retry PLAN_RUN_ID
```

重试沿原输入、目录和策略，根据实际阶段续做草拟或复核，不接受新需求或策略参数。已就绪或已绑定的计划不会再次启动。新增答案需建立新规划输入；首次准备没有完成、旧运行停止未确认时，不能自动从目录猜测恢复配置。

## 实现与固定候选

```bash
uv run python -m multi_agent task run PLAN_RUN_ID \
  --database /absolute/data/runs.sqlite3 \
  --repository /absolute/repository --directory /absolute/data/implementation-1
```

首次运行核验实际规划证据并原子绑定任务。执行者按原冻结规则选择，在独立 worktree 中修改原范围；完成后宿主再次核验交付并固定候选。该操作结束于候选，输出中的 `verified` 不表示已测试或审核。

可用 `--implementation PROVIDER MODEL EFFORT` 覆盖初始请求，以重复 `--fallback PROVIDER MODEL EFFORT` 提供有序备选，以 `--task-type NAME` 指定任务类型。实现 provider 使用 `codex` 或 `claude-code`，必须满足规则求交；前端规则仍约束返工。模型选择及原因是请求记录，实际值仍以运行时报告为准。

默认实现总预算 `--max-attempts 2`，包括初始实现、普通失败重试和一次返工，预检失败也占用已预留的尝试。使用一次普通重试后，默认预算可能没有返工余量；后续不会增加或重置预算。

```bash
uv run python -m multi_agent task run PLAN_RUN_ID \
  --database /absolute/data/runs.sqlite3 \
  --repository /absolute/repository --directory /absolute/data/implementation-2 \
  --retry
```

普通重试只处理尚未返工、实际已停止且预算允许的失败或取消尝试，目录必须新建，策略参数不得改动。只有实际 `MODEL_UNAVAILABLE` 事实可用于排除先前请求模型并按原备选顺序选择；认证或超时不会被猜成模型不可用。当前备选仅在原规则允许的同一 provider 内切换模型；其他 provider 记为 `disallowed_by_rules`，运行时 provider 和 program 也保持冻结。命令不会自动反复尝试备选。

## 测试、审核与返工

用返回的 task ID 和 implementation attempt，按 [候选测试说明](task-tests.md) 显式声明完整具名测试集合并执行；随后按 [同候选审核说明](task-reviews.md) 选择一个或两个审核席位。`task-test` 与 `task-review` 的已有输入和退出语义保持。

```bash
uv run python -m multi_agent task rework REVIEW_ROUND_ID \
  --database /absolute/data/runs.sqlite3 --directory /absolute/data/rework-1
```

该命令从当前审核候选建立新 worktree，仍使用原任务、规则和预算。准入、一次限制及交接预算见 [返工说明](task-rework.md)。新候选必须重新执行原完整有序测试集合，再建立新审核；旧报告保留在旧候选上。

## 查看与取消

```bash
uv run python -m multi_agent task show \
  --database /absolute/data/runs.sqlite3 --plan PLAN_RUN_ID --history

uv run python -m multi_agent task show \
  --database /absolute/data/runs.sqlite3 --task TASK_RUN_ID --history
```

show 只读现存数据库，不创建数据库、目录或锁，不迁移或恢复进程。它展示冻结输入、请求与实际报告、预算、当前候选、关联测试与审核及结构化 `next_action`。历史 ready 不会直接成为当前可验收；多个审核轮次各自保留结论，不根据最新行号替用户选择。

Ctrl-C 通过现有 stop Event 交给执行控制器，等待实际结算后返回 130。未知停止和残留准备记录保持未确认；不存在通用的“强制恢复”开关。已完成实现但候选固定中断时，保留实际目录和记录供核查，不自动重跑实现或收养残缺 ref。

新命令退出 0 表示本操作目标完成或 show 成功读取；1 表示正常负向、待用户处理或预算耗尽，附结构化结果；2 表示输入、尺寸、来源/CAS 或宿主错误，在 stderr 保留诊断和 notes；130 表示中断。输入/宿主错误不要求 stdout JSON。可验收不等于已接受或已合并。

本阶段完整流程使用真实模块 CLI 和明确的 fake 外部程序做离线验收。原生写权限与测试沙箱仍须 S9 的具体 canary 验证，不能用离线结果替代。
