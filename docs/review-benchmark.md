# 阶段 1 审核基准

当前交付了固定样本、准备工具和导入评分器，**没有运行真实模型基准**，没有得出单审或双审的推荐结论。所有质量判断与测量值均明确标为 `user_supplied`；工具不把位置匹配当作有效发现证明，不自行发模型请求，不执行样本代码或测试。

样本为 B01–B08：六例 development、两例 heldout；其中两例是指定输入域内的无缺陷改写、六例含已知缺陷。部分边界样本用于观察可能的单方发现，这不是已观察到的模型表现。单元测试检查固定 oracle 的行为例子，不构成任意输入上的形式化证明。

源码及需求在 `tests/fixtures/benchmark/visible.json`，评分答案在独立的 `oracle.json`。运行需要本仓库随附的 fixture pack，目前尚未将它打包进分发 wheel。

## 准备输入

输出须在工作台仓库之外，目录必须尚不存在：

```bash
uv run python -m multi_agent --data-dir /external/bench-state \
  benchmark prepare --out /external/bench-inputs
uv run python -m multi_agent --data-dir /external/bench-state \
  benchmark template --manifest /external/bench-inputs/manifest.json \
  > /external/results.json
```

`prepare` 只读取 visible pack，生成八个只含 `case.py` 的 Git 仓库、各自两次确定性提交，并保存冻结快照。输出含 `manifest.json`、`review-prompt.txt`、`cases/` 与 `review-inputs/B01/` 等八个只读审核目录。每个审核目录包含与 CLI 初始输入逐字一致的 `review-input.json`、`diff.patch`、`source/snapshot.json` 和两侧文本树；不会复制 oracle、测试答案、缺陷 ID 或期望发现数量。数据目录可以在输出根的 `data/`，但不能在样本仓库或 `review-inputs/` 内，避免数据库和其他报告进入审核目录。

manifest 固定每个样本的 snapshot ID、可见输入哈希、提示词版本及提示词哈希。四种模式必须共用同一次 prepare 的 manifest。换输出路径重新 prepare 时，仓库身份不同，snapshot ID 会变化；固定提交与 `visible_input_hash` 仍可核对代码、需求、报告 schema 和提示词的一致性。评分器拒绝混用其他快照。

先用 development 调整提示词，冻结提示词后再运行 heldout。评分分别给出开发集、留出集和全体汇总；缺失格子不会作为零分参与计算。

## 用户运行真实审核

从 manifest 取某个 `snapshot_id`，使用同一数据目录。下列命令会产生真实请求，应由用户选择有效模型和档位后自行运行：

```bash
uv run python -m multi_agent --data-dir /external/bench-state \
  run SNAPSHOT_ID --seat codex MODEL_ID EFFORT
uv run python -m multi_agent --data-dir /external/bench-state \
  run SNAPSHOT_ID --seat claude MODEL_ID EFFORT
uv run python -m multi_agent --data-dir /external/bench-state \
  run SNAPSHOT_ID --seat codex CODEX_MODEL CODEX_EFFORT \
  --seat claude CLAUDE_MODEL CLAUDE_EFFORT
uv run python -m multi_agent --data-dir /external/bench-state \
  show ROUND_ID --history > /external/round.json
```

比较首轮独立报告，不把后续交叉确认悄悄混入首轮数据。`official-plugin` 由用户在「Claude Code + 官方 Codex 插件」中，以对应的 `/external/bench-inputs/review-inputs/B01` 等目录为审核工作目录，提供输出根的 `review-prompt.txt` 文本和相同结构化报告要求，再导入结果。不要直接使用 `cases/B01`，那里只有 Git 源码，没有提示词要求的输入布局。插件内部提示词、隐藏上下文、用量口径可能不同；manifest 只约束应用提供的可见输入。工具不会安装或启动该插件，也不声称已完成该比较。

这些快照没有配置测试证据，审核界面会显示证据不足。人工评分不代表候选版本已可验收。

## 导入与人工判断

模板含全部 32 个 `(case_id, mode)` 格子，初始均为 `not_run`，报告为空，时间与用量为 null。允许省略未运行格子；同一格子不能重复，重复实验使用另一份结果文件。

保持模板中的快照、输入和提示词字段不变，填写以下信息：

- `status`：completed、failed 或 not_run。报告缺席时，即使填 completed 也只显示 pending。
- `reports`：原始 `{summary, findings}` 报告。codex-only 的键为 codex；claude-only 为 claude；dual 同时有这两键；official-plugin 为 plugin。可从 show 中当前成功尝试的 result 复制，禁止混入旧尝试或确认轮次。
- `duration_seconds`：实际测得的整次运行墙钟时间，未知为 null。双审不能把并行席位耗时相加。
- `usage`：未知为 null，否则按上述席位名记录 `{reported_model, reported_effort, counts}`。模型/档位未知为 null；counts 只复制历史 Usage 事件或原始日志中的数值字段，不能用请求配置代替。费用字段保留其客户端估算含义，跨提供方不混合求和。

人工判断文件初始内容：

```json
{"schema_version": 1, "source": "user_supplied", "records": []}
```

人工对照 source、报告和独立 oracle，为每个 finding 加入记录。下面仅说明格式，不是模型实测结果：

```json
{
  "case_id": "B01", "mode": "dual", "seat": "codex",
  "snapshot_id": "从评分输出 judgment_bindings 原样复制",
  "report_sha256": "从评分输出 judgment_bindings 原样复制",
  "finding_id": "报告里的原始 ID", "verdict": "known", "issue_id": "D01",
  "reason": "人工核对了报告描述与该已知问题，写明理由。"
}
```

verdict 为 known（本样本 oracle 问题）、additional_valid（额外真实问题）、false_positive（误报）或 unresolved（无法决定）。前三者必须有非空 issue ID；known 使用 oracle ID，后两种已判定分类使用自定 ID，例如 EX01、FP01，不能冒用 oracle ID。只有 unresolved 使用 null。reason 必填。

先用空 judgments 文件评分，输出的 `judgment_bindings` 提供可直接复制的身份字段。`snapshot_id` 和整份归一化报告的 `report_sha256` 必填；快照或报告发生任何变化时，旧判断会被拒绝，复用 finding ID 不能带入旧人工结论。报告哈希使用 UTF-8 canonical JSON（键排序、不转义 Unicode、紧凑分隔符）的 SHA-256，不包含引用核对的附加字段。

同一问题重复出现时，人工给相同 issue ID，评分器据此去重；不同问题即使位置相同也不能共用 ID。有效发现还必须通过冻结原文引用核对。只匹配位置、引用错误、遗漏判断、尚有疑问或报告不齐时，质量指标均为 null/pending。

```bash
uv run python -m multi_agent --data-dir /external/bench-state \
  benchmark score --manifest /external/bench-inputs/manifest.json \
  --results /external/results.json --judgments /external/judgments.json
```

完整格子才输出 valid_discoveries、known_found、additional_valid、missed、false_positives、duplicates。漏报只针对 oracle 已知问题；额外发现不修改其分母。duplicates 是有效发现中去重掉的报告条数，误报按人工 FP ID 去重。未运行不是“发现零个”；未完成分组的汇总也为 null。时间缺一项不求总和；用量逐条保留，缺记录或没有计数不标为完整。

输出保留报告原字段、引用标记、人工判断及理由。`executed_models` 恒为 false，`recommendation` 恒为 null。导入值无法证明模型实际运行过，状态只称 `imported_user_evidence`。真实比较和审核模式选择仍待用户运行并核对这些数据。
