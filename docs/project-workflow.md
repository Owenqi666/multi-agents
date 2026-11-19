# 项目规划 CLI

PM 先确认原话和最终目标。工作台记录 Git 提交，再由显式选定的模型生成阶段计划。每次规划生成独立的计划 ID，按原 ID 可读回。数据使用工作台的 `reviews.sqlite3`，数据目录应在目标仓库和 Git 元数据目录之外。

## 从目标到项目计划

准备两个 UTF-8 文件。将需求原话放在 `original.md`，将 PM 最终确认的文本放在 `confirmed.md`。如果直接采纳原话，两个参数可使用同一文件。

```sh
uv run python -m multi_agent --data-dir "$DATA" project confirm \
  --repo "$REPO" --baseline HEAD --original-goal original.md --confirmed-goal confirmed.md
```

回执含 `kind: "confirmed_goal"`、`project_id`、`goal_id`、完整 `baseline_commit`、两个原文、源仓库和确认时间。确认已有项目的新版本时传入 `--project-id PROJECT_ID`，继续指定该项目的仓库、本次基线和两个文件。

```sh
uv run python -m multi_agent --data-dir "$DATA" project plan "$GOAL_ID" \
  --planner codex gpt-6.2-astra high
uv run python -m multi_agent --data-dir "$DATA" project show "$PLAN_ID"
uv run python -m multi_agent --data-dir "$DATA" project show "$PLAN_ID" | jq -r .markdown
uv run python -m multi_agent --data-dir "$DATA" project show "$PROJECT_ID"
uv run python -m multi_agent --data-dir "$DATA" snapshot "$SOURCE_SNAPSHOT_ID"
```

`--planner PROVIDER MODEL EFFORT` 使用 `codex` 或 `claude` 及该运行时支持的具体模型和档位，例如 `claude claude-fable-5-2 high`。用 `--timeout` 指定包含预检的总执行秒数，默认 300。用 `--grace` 指定进程停止的宽限秒数，默认 2。使用非默认运行时路径时传入 `--codex-bin` 或 `--claude-bin`。

只有 `project plan` 启动选定的规划角色。运行在临时只读源码视图中，提供确切的确认版本和指定提交的文本。有用的上下文如规则、脚本和文档可按需读取。快照注记记录省略的二进制文件和 Git 子模块，符号链接按文本呈现。

## 回执与阶段任务书

| 字段 | 值 |
| --- | --- |
| `kind`、`plan_id`、`goal` | `kind` 为 `project_plan`。嵌入规划所依据的完整确认记录。 |
| `status`、`started_at`、`finished_at` | `running`、`succeeded` 或 `failed`。结束时间为空表示还没有持久化终态。 |
| `planner_request` | 显式选择的供应商、模型及档位。 |
| `evidence` | 运行时可用的版本、会话、报告模型、档位、用量、退出码及日志封存信息。空值表示未确认。日志文件保留原生事件，包括收到的改派事件。 |
| `source_snapshot_id`、`prompt_template_version` | 提供给规划者的 Git 快照及提示词版本。 |
| `document`、`markdown` | 成功计划的结构化正文及从同一正文渲染的 Markdown。阶段在 `document.stages` 中按实施顺序排列。 |
| `error` | 失败时的详情、原始类别、可识别的失败分类及重试时间。 |

`project show PLAN_ID` 返回同一计划回执。`project show GOAL_ID` 返回完整确认记录。`project show PROJECT_ID` 返回各确认版本和关联计划列表。查看操作的退出码为 0。规划成功退出 0。规划运行失败时退出 1 并输出持久化的失败回执。无法解析参数、打不开数据库或无法读取指定 Git 基线时退出 2。强制中止进程可能留下未结束的运行，可用项目历史定位。重新执行 `project plan` 会创建一个独立版本。

项目正文有 `title`、`architecture`、`shared_interfaces` 和有序 `stages`。例如账本项目使用一个本地命令分派器，共用签名为 `ledger_amount($cents)`、`ledger_income(@transactions)` 和 `ledger_expense(@transactions)` 的 Perl 接口。

| 阶段 | 目标与实现方向 | 流程与依赖 | 验收与交付 |
| --- | --- | --- | --- |
| `amount` | 通过单独的格式化函数将整数分转换为两位小数。 | 实现 `app/amount.pl`，运行金额示例及阶段检查。无需上游阶段。 | `perl checks/ledger.pl direct` 通过，交付格式化函数。 |
| `summary` | 在同一阶段交付收入和支出汇总。 | 基于已交付的 `amount`，分别实现收入和支出函数，再合并检查。 | `perl checks/ledger.pl delegated` 通过，交付两项汇总功能。 |
| `monthly-report` | 规划未来按月统计，保留待领取状态。 | 记录报表输入和汇总接口。 | 交付后续可实施的月报说明。 |

每个阶段的字段为 `stage_id`、`title`、`goal`、`approach`、`workflow`、`depends_on`、`acceptance_criteria`、`deliverables`。每个依赖引用同版计划中此前已列出的阶段。阶段任务书以 `plan_id` 和 `stage_id` 为定位依据。
