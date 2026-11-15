# 阶段 1 审核 CLI

阶段 1 提供冻结、单审/双审、报告汇总、补跑和一轮交叉确认。`freeze`、`snapshot`、`show` 不发模型请求；`run`、`retry`、`confirm` 会请求所选真实 CLI，由用户本人在终端运行。自动化开发验证仅使用假 CLI 和已录制夹具。所有命令均不执行目标项目的测试命令。

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


## 单审或双审

以下命令会发真实请求；模型和档位显式选择。环境检查记录版本、登录和可获得的目录，目录缺失记未确认，不自动替换不可用模型。

```bash
uv run python -m multi_agent run SNAPSHOT_ID --seat codex gpt-6.1-sol high
uv run python -m multi_agent run SNAPSHOT_ID \
  --seat codex gpt-6.1-sol high --seat claude claude-opus-5-5 high
```

只选一个席位时只启动一个审核会话；双审必须选择不同模型。每位使用全新目录和相同冻结首轮输入，并行执行。模型不会被提供另一位的首轮报告。最多两个席位，一条命令只为每个选中席位启动一次模型会话；`--timeout`（默认 180 秒）和 `--grace`（默认 2 秒）控制运行和取消收尾。再次重试需要用户明确运行命令。

输出 `round_id` 和业务评估 `assessment.status`：`ready`（可验收）、`needs_rework`（需要返工）、`user_decision`（待用户决定）、`incomplete`（报告或确认未完成）。顶层 `status` 是编排状态，不代表验收。`ready` 仅依据本轮用户提供的完整测试集合与审核报告；工具不替用户接受或合并，也不自动执行返工。

```bash
uv run python -m multi_agent show ROUND_ID --history
uv run python -m multi_agent show
uv run python -m multi_agent retry ROUND_ID
uv run python -m multi_agent retry ROUND_ID --seat reviewer-2
```

补跑沿用快照、模型、档位和提示词版本，只重开失败或尚未启动的席位。成功报告保留，旧尝试迟到只进入历史。更换模型或代码时创建新轮次；代码更新先重新冻结与提供新提交的测试证据。旧轮次仍可查询旧快照。

`show` 包含请求配置、运行时实际报告值、退出码、来源日志和尝试历史；缺失的实际模型/生效档位是 null 并标“未确认”。请求值不会补填实际值。用量事件保留在 `--history` 中，不对多次累计通知再次相加。原始流及未知事件位于数据目录的 `logs/轮次/席位/尝试/`，与快照检出分开。

## 一轮交叉确认

```bash
uv run python -m multi_agent confirm ROUND_ID
```

仅双审首轮报告全部完成后可启动。只为有他人 P0/P1 需要核对的席位创建新只读会话，模型/档位继承原席位，父轮次最多关联一个确认轮次。返回的是子轮次 ID；查看父轮次获取结合确认结果的整体评估。子轮次失败用 `retry CHILD_ROUND_ID`，不新建第二轮确认。

确认来源由运行器绑定，不能由模型自报席位；缺回答显示 `unanswered_confirmations`，不被当成同意。一方未提及的独立发现仍保留；未核对引用或确认分歧会交用户决定。位置归并只是候选重复项，不能证明语义相同或诊断正确。

## 取消、恢复与限制

Ctrl-C 请求取消，等待运行时终态或进程停止，保留已经完成的席位。适配器已观察到终态、但进程仍在收尾时，该席位拒绝晚到的取消；报告继续经过结构、矛盾终态和目录完整性检查后才记为成功。双审只取消仍在运行的席位，补跑只重开失败席位。数据库级文件锁让本地同一数据目录一次只运行一个控制进程，轮内仍并行；运行期间可 `show`。

Codex 成功回合优先读取显式 `final_answer`；没有该标记时兼容最后一个省略或为 null 的 phase 消息，仍要求合法 JSON 和完整报告结构。`commentary`、`partial_answer` 不作为最终报告，也不会回退到更早的合法内容掩盖最后候选的错误。

Claude 预检联合检查 `auth status` 的 JSON 与退出码：`loggedIn=false` 且退出码 1 记为 `auth_expired`，不会启动模型回合。其他异常退出仍失败；版本检查始终要求退出码 0。未登录退出码的依据是 [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)，本次仅用假 CLI 验证该分支。

控制进程意外退出后，未完成尝试可在 `show` 中看到。确认先前 CLI 已停止后，`retry ROUND_ID --recover` 可结束遗留 `running` 控制尝试并重新派发；其旧进程退出码仍未知，不伪造取消证据。遗留 `cancelling` 缺终态时拒绝恢复，可开启新审核轮次并保留旧记录。文件锁释放只能证明控制进程不再持锁，不能证明其孤儿子进程已结束。

Codex 审核与环境预检使用独立的临时 HOME/CODEX_HOME，只继承官方 ChatGPT `auth.json` 的必要认证及刷新元数据。临时认证目录在冻结输入和日志之外，目录权限为 0700、文件为 0600；不会改写用户原配置或登录。缺失/无效登录、非 0.169.0 运行时、无法落实隔离策略都会明确失败。

每次 Codex 会话启用命名权限策略：只开放冻结目录的固定绝对路径与官方 `:minimal` 系统依赖，网络关闭且拒绝审批；改变命令 cwd 不会扩大读范围。自动记忆、指令发现、skills 上下文、MCP/插件入口及相关远端上下文功能被关闭或拒绝。启动前检查有效配置和配置层，并要求 `configRequirements/read` 报告没有 requirements；线程的 `instructionSources` 必须为空。任何不符合预期的配置或 MCP 活动会阻止回合启动或拒绝其结果。

无模型的原生权限 canary 已验证冻结文件可读，外部文件、其他席位报告/DB、答案文件、认证副本、父路径和越界符号链接拒读，改变 cwd 仍保持同一边界。这是该版本原生工具权限证据；后续真实模型上下文验收单独执行，不据此宣称整台计算机隔离。官方 `:minimal` 包含系统目录与设备例外，可信客户端的 `fs/readFile` RPC 也不属于模型工具沙箱。具体版本、来源和证据见 [T-0021 交付说明](deliveries/T-0021.md)。

只有所有自有进程确认停止后才清除临时认证。启动、退出或删除状态未知时，记录 `cleanup_incomplete` 与资源位置，保留最小权限，不伪造已退出/无残留；取消发生在版本预检时也遵守此规则。普通进程组后代会清理，主动脱离会话的外部进程不在此保证内。Claude 继续使用 safe-mode、Read/Grep/Glob，并去掉 API key/token 环境变量。每次审核仍检查冻结目录前后的文件内容与模式。

原始配置响应只保留安全审计摘要。业务报告保留合法的邮箱、路径和代码引用；只遮蔽已注册的认证秘密，避免通用日志脱敏改变引用原文。

命令处理成功返回 0，即使审核发现问题；脚本应读取 JSON 的状态。用户输入错误、执行锁冲突或非法补跑返回 2。没有自动接受、合并、改写项目规则或执行目标代码。
