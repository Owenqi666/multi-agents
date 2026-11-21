# 项目规划 CLI

PM 先确认原话和最终目标。工作台记录 Git 提交，再由显式选定的模型生成阶段计划。每次规划生成独立的计划 ID，按原 ID 可读回。数据使用工作台的 `reviews.sqlite3`，数据目录应在目标仓库和 Git 元数据目录之外。

## 可选的需求咨询

把用户原话保存为 UTF-8 文件 `original.md`。用 `--advisor PROVIDER MODEL EFFORT` 指定一位顾问，或重复该选项指定 Astra 和 Fable 的顺序。每位顾问各运行一个只读回合。第二席使用相同的 Git 提交和快照，读取同一份原话和首席原始草案。已存在的项目可使用 `--project-id PROJECT_ID` 绑定咨询来源。

```sh
uv run python -m multi_agent --data-dir "$DATA" project consult \
  --repo "$REPO" --baseline "$BASELINE" --original-goal original.md \
  --advisor codex gpt-6.2-astra high --advisor claude claude-fable-5-2 high
uv run python -m multi_agent --data-dir "$DATA" project show "$CONSULTATION_ID"
uv run python -m multi_agent --data-dir "$DATA" project show "$CONSULTATION_ID" \
  | jq -j .draft.draft_goal > confirmed.md
```

咨询返回 `kind` 为 `goal_consultation`，并包含 `consultation_id`、`original_goal`、`baseline_commit`、`source_snapshot_id`、`status`、`seats` 和 `draft`。`draft` 包含可编辑的 `draft_goal`、带冻结文本文件路径的 `background`、`assumptions`、`questions` 和 `remaining_disagreements`。每席有自己的 `request`、`source_snapshot_id`、`advice`、`error`、原生 `evidence` 和开始结束时间。未报告的实际模型和档位以 `null` 表示。第二席失败时，首席的 `advice` 仍可读，整轮 `draft` 为空。`consult` 成功退出 0，失败退出 1，已确认中断退出 130，参数错误退出 2。`project show` 只读已有结果。可显式发起新的咨询。

PM 编辑 `confirmed.md`，用同一仓库、基线和原话确认。已有项目继续传 `--project-id PROJECT_ID`。

```sh
uv run python -m multi_agent --data-dir "$DATA" project confirm \
  --repo "$REPO" --baseline "$BASELINE" --original-goal original.md \
  --confirmed-goal confirmed.md --consultation "$CONSULTATION_ID" --adoption edited
```

`--adoption accepted` 表示确认文本与草案完全相同，`edited` 表示有改动。省略时按实际文本判定。确认回执的 `consultation` 保留咨询 ID、采纳决定和快照 ID。未指定项目的咨询可用于初次确认，首次采纳后关联同一项目。关联要求咨询完成，并核对原话、Git 基线及仓库。咨询、确认、编辑和查询各自独立，确认调用保存 PM 决定，无模型回合。

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

## 发布计划修订

从已完成的计划导出结构化 `document`，编辑后显式提交。工作台校验阶段结构、顺序和依赖，并在原确认目标下生成新 `plan_id`。修订回执中 `origin` 为 `pm`，`source_plan_id` 指向原计划，`planner_request` 和 `evidence` 为空。

```sh
uv run python -m multi_agent --data-dir "$DATA" project show "$PLAN_ID" \
  | jq .document > revised-plan.json
uv run python -m multi_agent --data-dir "$DATA" project revise "$PLAN_ID" \
  --document revised-plan.json
uv run python -m multi_agent --data-dir "$DATA" project show "$NEW_PLAN_ID"
```

原计划、阶段运行、候选和测试审核以原 ID 读回。领取后续阶段时传新计划 ID。若上游阶段及其依赖任务书、架构和共用接口均未修改，工作台可在新计划中复用仍合格的已有上游交付，并在依赖回执保留来源 `plan_id`。当 PM 修改确认目标时，先保存独立的目标版本，再以新 `goal_id` 调用 `project plan`。

## 直做金额阶段

```sh
uv run python -m multi_agent --data-dir "$DATA" project stage start "$PLAN_ID" amount \
  --sol gpt-6.2-sol high --card-review claude claude-opus-5-5 high
uv run python -m multi_agent --data-dir "$DATA" project stage list "$PLAN_ID"
uv run python -m multi_agent --data-dir "$DATA" project stage show "$STAGE_RUN_ID"
```

`stage start` 使用 `direct` 模式。Sol 只草拟并实施这一阶段的一张正式任务卡。独立规划席位针对同一张卡的哈希复核。草拟、卡复核和写入各用独立会话。正式任务绑定记录与普通 `task show` 共用，已有单任务流程的默认角色继续沿用。若冻结路径规则拒绝 Sol，回执的 `dispatch_error` 给出规则文件及范围，阶段没有写入尝试。

阶段回执包含 `plan_id`、`goal_id`、`stage_id`、`stage_run_id`、`execution_baseline`、`planning_run_id`、`task_run_id`、`candidate`、规划与实施原回执。重复提交同一计划的同一阶段返回现有记录。尚未绑定实施时，可用 `project stage resume STAGE_RUN_ID` 显式继续已准备并可补跑的规划，沿用冻结配置和原运行标识。若首次准备从未保存，显式使用 `resume STAGE_RUN_ID --prepare`，可随首次准备指定 CLI 路径、超时或宽限期。已绑定实施的失败按现有 `task run --retry` 入口处理。启动或继续阶段在候选校验通过时退出 0，流程失败或派发受阻时退出 1，中断时退出 130。仍可使用 JSON 回执和 `stage show` 读取规划或实施证据。

依赖阶段须有当前候选的可核验测试及审核。启动后续阶段时，工作台记录每个已验证上游的候选、测试尝试和审核。若一个已验证候选已包含其他上游提交，使用该候选作为基线。也可用 `--baseline` 显式指定包含原项目基线和全部上游候选的提交。工作台接受 Git 祖先关系或当前有合格证据的集成。经集成纳入时，阶段记录同时保留集成提交、测试摘要和审核快照。

以回执中的 `task_run_id` 和实现尝试号调用既有测试及审核。两次操作均使用同一个 SQLite 库，运行目录位于该库的同级并且是新目录。

```sh
uv run python -m multi_agent task-test run "$TASK_RUN_ID" \
  --database "$DATA/reviews.sqlite3" --repository "$REPO" \
  --implementation-attempt "$IMPLEMENTATION_ATTEMPT" --directory "$DATA/stage-tests" \
  --command test --declaration-source "PM 确认的阶段测试"
uv run python -m multi_agent task-review run "$TASK_RUN_ID" \
  --database "$DATA/reviews.sqlite3" --repository "$REPO" \
  --implementation-attempt "$IMPLEMENTATION_ATTEMPT" --test-attempt "$TEST_ATTEMPT" \
  --directory "$DATA/stage-review" --seat claude claude-opus-5-5 high --no-auto-rework
```

再次运行 `stage show` 查看 `candidate_verified`、`tests_passed` 和 `review_satisfied`。测试需涵盖正式卡引用并绑定同一候选，最新的一轮代码审核需对应当前实现和测试尝试。审核满足后可用于后续阶段，用户仍控制对项目分支的接受和合并。

## 委派交易汇总阶段

沿用上面的 `PLAN_ID`、`DATA` 和 `REPO`。金额阶段的测试与审核通过后，领取依赖它的 `summary`。Sol 在共同的冻结基线上一次草拟一组任务卡。阶段回执保留 Sol 的运行、原始输出及有效拆分，`leaves` 列出各卡的真实 ID、定义和 `unmet_dependencies`。`stage list` 此时返回 `amount` 和 `summary` 两条领取记录。月报仍可在原计划中查看。

```sh
uv run python -m multi_agent --data-dir "$DATA" project stage start "$PLAN_ID" summary \
  --mode delegate --sol gpt-6.2-sol high --card-review claude claude-opus-5-5 high
uv run python -m multi_agent --data-dir "$DATA" project stage show "$SUMMARY_RUN_ID"
```

从回执中取 `SUMMARY_RUN_ID` 和两张叶卡的 ID。按 `write_scope` 区分收入卡和支出卡。首次领取时，显式选择符合该卡冻结规则的 Terra 或 Sonnet 及档位。复核输入包含实际基线、父拆分运行与清单摘要、所领条目及最终任务卡哈希。每张叶卡有独立复核决定和实施候选。`stage show` 可用 `blocked_reason` 定位失败，无依赖的兄弟卡可继续领取。

首次领取可用 `--card-review PROVIDER MODEL EFFORT` 为该叶卡单独选择复核者。未指定时继承阶段复核配置。叶卡复核者与父拆分草拟者使用不同型号。复核遇到可重试失败时，执行 `project stage leaf SUMMARY_RUN_ID CARD_ID --review-only` 会复用该叶卡的规划运行 ID、执行者与复核请求。重新冻结时会沿用两项模型配置，并保留原规划供查阅。

```sh
uv run python -m multi_agent --data-dir "$DATA" project stage leaf "$SUMMARY_RUN_ID" "$INCOME_CARD" \
  --implementation codex gpt-6.1-terra medium --card-review claude claude-fable-5-2 high --review-only
uv run python -m multi_agent --data-dir "$DATA" project stage leaf "$SUMMARY_RUN_ID" "$EXPENSE_CARD" \
  --implementation codex gpt-6.1-terra medium --review-only
uv run python -m multi_agent --data-dir "$DATA" task run-batch "$INCOME_PLANNING_RUN_ID" "$EXPENSE_PLANNING_RUN_ID" \
  --database "$DATA/reviews.sqlite3" --repository "$REPO" --directory "$DATA/summary-work"
```

对单卡立即实施时省略 `--review-only`。批量实施适用于同基线、无内部依赖且写入范围不重叠的已复核卡。批量命令默认使用各叶卡自己的冻结执行者。有关联的叶卡须先交付精确上游候选。下游基线须包含这些候选，或包含已通过完整测试与独立审核的上游集成候选。多个上游无共同候选时，使用 `--baseline` 指定已验证的集成基线。工作台在启动实施时复查该集成的当期测试和审核。已复核但尚未绑定实施的叶卡遇到上游候选变化，使用 `project stage leaf SUMMARY_RUN_ID CARD_ID --refreeze` 重新冻结并复核，可加 `--review-only`。`prior_planning_run_ids` 保留先前版本。已绑定的叶卡遇到相同变化时需要新的项目计划版本。

两张卡都出现当前有效候选后，`can_integrate` 为 true。用 `project stage integrate` 按阶段领取的全部叶卡创建集成，保存返回的 `integration_id`、`target_ref` 和 `target_baseline`。工作台会在阶段执行基线准备或复用专用本地分支。已有分支正好位于该基线时，可显式传 `--target BRANCH`。集成与接受操作保持当前项目分支原位。

```sh
uv run python -m multi_agent --data-dir "$DATA" project stage integrate "$SUMMARY_RUN_ID"
uv run python -m multi_agent integration test "$INTEGRATION_ID" \
  --database "$DATA/reviews.sqlite3" --repository "$REPO" --directory "$DATA/summary-tests"
uv run python -m multi_agent integration review "$INTEGRATION_ID" \
  --database "$DATA/reviews.sqlite3" --repository "$REPO" --directory "$DATA/summary-review" \
  --seat claude claude-opus-5-5 high --no-auto-rework
uv run python -m multi_agent --data-dir "$DATA" project stage show "$SUMMARY_RUN_ID"
```

`integration` 展示确切成员集合对应的集成 SHA、测试尝试及摘要、审核轮次及快照。只有同一候选的完整测试和最新有效审核通过，阶段才可解锁后继。验收或更新目标分支由用户显式执行。离线可运行 `scripts/verify.sh unit replay`，覆盖真实 Git、SQLite 和假模型 CLI 的流程。

HTTP 客户端可在同一数据目录执行这些操作。[项目 HTTP 服务](project-service.md)
