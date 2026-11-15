# 阶段 1 审核 CLI

当前先提供冻结输入入口。审核运行、报告汇总与补跑在后续任务卡接入；这里的冻结命令不会发模型请求，也不会执行提交里的代码或测试。

从仓库两个提交创建快照：

```bash
uv run python -m multi_agent freeze --repo /path/to/repo \
  --baseline BASE_COMMIT --candidate CANDIDATE_COMMIT \
  --requirements /path/to/requirements.txt \
  --acceptance '明确、可核验的验收条件'
```

输出 JSON 中的 `snapshot_id` 可用于读取完整输入：

```bash
uv run python -m multi_agent snapshot SNAPSHOT_ID
```

数据默认保存在 `~/.local/share/multi-agent/`，可在子命令前指定 `--data-dir /external/path`。它必须位于目标仓库之外。输入使用提交对象，工作区未提交的修改不参与审核。

`--tests /path/to/evidence.json` 导入用户提供的测试证据，格式为：

```json
{
  "required_commands": [["scripts/verify.sh"]],
  "results": [{
    "commit": "候选提交的完整哈希",
    "command": ["scripts/verify.sh"],
    "exit_code": 0,
    "log": "原始测试日志"
  }]
}
```

`required_commands` 声明本轮必须通过的完整测试集合，内部冻结字段为 `required_tests`。本阶段只保存用户提供的证据及来源，不宣称运行过这些测试。缺少测试集合或对应提交的完整通过记录时，不得判为可验收。

快速试跑可以用 `freeze --diff /path/to/patch.diff` 替代仓库与两个提交；仍需需求和验收条件，不能携带测试证据。此类输入只能生成报告，引用位置待核实。

快照包括双方文本文件、完整对象 ID、模式、diff、需求、验收和测试证据。binary 与 gitlink 只保留元数据；symlink 在审核目录变成普通文本。暂不展开子模块或运行构建，过大输入会拒绝而不截断。
