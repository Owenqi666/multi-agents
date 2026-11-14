---
name: task-card
description: 用任务卡（JSON，按 schemas/task-card.schema.json 校验）规划、交接和领取实现任务。把需求拆成任务、把工作交给另一个模型、收到一张任务卡准备开工、或发现任务做不进写入范围时使用。
---

# task-card

任务卡是规划方和实现方之间唯一的交接物，字段来自 `PROJECT_PLAN.md`「完整工作流」第 1 步。开发这个工作台时就用它：交接不用反复解释，schema 也在阶段 2 之前先被实际使用检验。

## 文件

- 任务卡：`tasks/<id>-<简短英文名>.json`，例如 `tasks/T-0007-snapshot-hash.json`。`id` 是 `T-` 加四位序号，按创建顺序递增。
- Schema：`schemas/task-card.schema.json`。产品代码将来加载的是同一份，所以改 schema 要走任务卡。
- 示例：本 skill 目录下的 `examples/T-0000-example.json`。

## 字段

| 字段 | 怎么写 |
| --- | --- |
| `schema_version` | 固定为 `1` |
| `id`、`goal` | 一张卡一个目标。目标写结果，不写实现步骤 |
| `baseline_commit` | 写卡时的 HEAD，任务从它的代码出发。40 位完整哈希，用 `git rev-parse HEAD` 取 |
| `write_scope` | 允许修改的路径，仓库相对路径或 glob。越小越好 |
| `depends_on` | 必须先集成的任务 id |
| `interface_contracts` | 和其他任务共享的接口。先定契约，再写依赖它的卡 |
| `deliverables` | 交付物：文件、命令、可观察的行为 |
| `acceptance_criteria` | 每条都能被某个命名测试判定，或被审核者对照 diff 判定 |
| `test_refs` | 命名测试，取值只能来自 `scripts/verify.sh --list`。实现类任务至少引用一个 |
| `open_questions` | 未决问题。答案会改变交付内容的，标 `blocks_dispatch: true` |
| `proposed_tests` | 可选。需要新的检查命令时在这里提议，写成程序加参数列表 |

没有内容的数组写 `[]`，不省略字段。省略和"确认过没有"是两回事，卡上要能看出是后者。

验收条件要可判定。"代码质量好""实现健壮"判定不了；"取消后该回合的终态记录为 cancelled，已有事件保留"可以。

## 规划方

1. 对照原始需求写卡。一张卡的写入范围应当小到一次实现会话能做完。
2. 两张卡的写入范围有重叠时，用 `depends_on` 排出先后。互不重叠且没有依赖的卡才能并行。
3. 校验：

   ```bash
   uv run .agents/skills/task-card/scripts/validate_task_cards.py --tests "$(scripts/verify.sh --list)" tasks/*.json
   ```

4. 把卡和原始需求的原文一起交给另一家模型复核：有没有遗漏、路径是否重叠、依赖是否完整、验收条件是否可判定。
5. 有 `blocks_dispatch: true` 的问题时，停下来交用户决定，把答案写进该问题的 `resolution`，然后再派发。
6. `proposed_tests` 只是建议。用户认可之前不执行，也不写进 `scripts/verify.sh`。
7. 卡片依据的代码变了就重定基线：实现方因此把卡交回，或者 `baseline_commit` 被 amend、rebase 改写掉时，对照当前代码复核这张卡，把 `baseline_commit` 更新为当前 HEAD，再派发。

## 实现方

1. 开工前给校验命令加上 `--dispatchable`。它额外检查阻塞问题都有答案、`baseline_commit` 在当前 HEAD 的历史里。不通过就不开始。
2. 在当前 HEAD 上开工，只改 `write_scope` 内的路径。任务卡自己提交在 `baseline_commit` 之后，所以 HEAD 通常比它新。先看 `git diff --name-only <baseline_commit> HEAD`：列出的路径都不在 `write_scope` 内，当前 HEAD 对这张卡就等同于基线；有落在 `write_scope` 内的，说明卡片依据的代码已经变了，停下来交回规划方重定基线。
3. 做到一半发现任务做不进写入范围，就停下来，写明卡在哪里、需要动哪些范围外的路径，交回规划方改卡。自行扩大范围会让并行任务的"互不重叠"前提失效。
4. 完成后跑 `test_refs` 列出的步骤和完整的 `verify`。交付说明写明开工时的 HEAD，逐条对应 `acceptance_criteria`，并列出从开工起实际改动的路径。
