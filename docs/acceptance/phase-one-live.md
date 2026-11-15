# 阶段 1 实机验收：T-0019

2026-11-15，Europe/London（当日 UTC+0）。本轮已形成可复核的实机证据，**阶段 1 尚未整体通过**：基础 CLI、单/双审编排、Claude 单席取消补跑、测试证据门槛与旧快照绑定有成功实测；Codex 两次读取冻结输入以外的用户记忆，后续受影响模式已停止；官方 Codex 插件未安装；人工质量评分未完成。报告提交不代表阶段验收通过。

开工 HEAD 和固定被测产品 SHA 均为 `87045fc6c138566d86d11b46e3b2744caf97b9b8`。未修产品、变更提示词、更新 CLI/plugin、刷新 protocol 或改变永久权限。用户本次明确授权阶段 1 窗口运行真实请求，覆盖此前“由用户终端运行”的本次执行限制；不改变未来任务的授权规则。

证据根目录：`/Users/owen/Desktop/Multi-agent-acceptance/T-0019/`，为私有持久目录。下文相对证据路径均在此根目录下。原始日志、DB、完整报告和命令输出不入库。

## 版本、输入和运行配置

| 项目 | 实测值及边界 |
| --- | --- |
| Codex | `codex-cli 0.169.0`，与本地快照一致；预检 logged_in=true，目录含 `gpt-6.1-sol` 且支持 high。 |
| Claude | `2.1.325 (Claude Code)`，与快照一致；预检 logged_in=true。模型/档位目录不可获得，保持 null。 |
| 请求配置 | Codex `gpt-6.1-sol/high`；Claude `claude-opus-5-5/high`。模型执行 timeout=180 秒、grace=2 秒，外部控制器另有上限。 |
| 运行时报告 | Codex 实际模型和档位均未报告，保持 null。成功 Claude 结果报告 `claude-opus-5-5`，实际档位仍 null。thread/init 回显不作实际配置证据。 |
| 认证与启动 | 工作台及外部执行脚本以程序和 argv 启动子进程，Claude 子进程去掉 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`，未修改用户登录。 |
| 输入 | 一次 `benchmark prepare`，B01–B06 development、B07–B08 heldout；原生模式使用同一 manifest、固定 prompt/schema。没有调提示词或用 heldout 调参。 |

`preflight/environment.json` 保存实际目录与登录检查，`preflight/` 保留原始流。提示词在 20:27:33 UTC 封存后才开始模型回合：

- `benchmark-inputs/manifest.json` SHA-256：`a4da5df0fb2209dfa6e97cfac4733dfbbe75618e904d98054fb01b0dbf1c91f9`。
- `benchmark-inputs/review-prompt.txt` SHA-256：`8af7a9e4bca3d1451e784355c9f5adf9146d00fbd15380bfb00ed0422cc30135`。
- 封存记录：`prompt-seal.json`。客户端初始提示词及输入未提供 oracle、期望发现数或另一席报告。Codex 仍实际自行读取了外部记忆，见下节；不能把提供相同输入等同于完整上下文隔离。

## 已复现的隔离阻塞与停止决定

两次 Codex 的 `commandExecution` 均成功读取 `~/.codex/memories/MEMORY.md`，exitCode=0，非空输出摘要哈希相同。这是**本次已发生的外部上下文读取**，不只是“隔离未知”。精确证据：

| 回合 | 证据文件与 JSONL 行号 |
| --- | --- |
| B01 codex-only：`4142404116d64f9d989fd13201fe6d1b` | `data/logs/4142404116d64f9d989fd13201fe6d1b/reviewer-1/1/codex-7433c055d822486c8f7340057ca1f8eb.jsonl`，第 82 行开始、第 84 行完成；第 20–44 行可见全局 MCP 启动。 |
| B01 dual：`e9de1b18f58c43be858ef52cc9904d1f` | `data/logs/e9de1b18f58c43be858ef52cc9904d1f/reviewer-1/1/codex-17577c73162144e995b732db2345e9df.jsonl`，第 67 行开始、第 69 行完成。 |

达到连续两次系统性问题后，停止 B02–B08 的 codex-only/dual 共 14 格；不修改参数或隔离实现来绕过问题，不继续启动 Codex 交叉确认。已完成输出保留为受限证据。**没有证据证明读取了 oracle 或另一席报告**，不扩大推断。原生事实审计在 `analysis/native-evidence-audit.json`，不包含用户记忆原文，标为 agent_reviewed，未生成用户人工标签。

Claude 实际使用 safe-mode、Read/Grep/Glob 及结构化输出工具。观察到的功能审核读取路径均在审核 cwd 中，文件摘要不变；init 无 MCP，但仍列出内置 skills/plugins，因此也不声称“所有 skills 均被禁用”或证明整个计算机完全隔离。只读配置、文件完整性和外部上下文隔离是不同结论。

## 功能验收矩阵

| 卡片条目 | 状态 | 实测结果与证据 |
| --- | --- | --- |
| 1 固定产品、版本、请求配置 | passed | HEAD/版本一致；`preflight/environment.json`、各 `commands/*/command.json`；本次产品路径未变更。 |
| 2 freeze/snapshot 与两种单审 | passed_with_limitations | 正式 Git freeze/snapshot 见 `functional/evidence-index.json`；B01 两种单审各只有所选席位，报告、引用、终态与 exit0 均保留。Codex 有上述隔离限制。 |
| 3 双审并行、同快照、等待齐全 | passed_with_limitations | B01 dual 两个不同 session，同快照；执行区间重叠 10.857828 秒（包含预检/清理，不称模型推理并发时长）。`commands/B01-dual-inprogress/stdout.txt`、`B01-dual-one-complete/stdout.txt` 的 assessment 均为 incomplete/selected_reports_missing；两份齐全后才 ready_for_assessment，业务评估仍 user_decision。客户端不泄漏对方首轮报告；完整隔离未通过。 |
| 4 取消和补跑 | partial_passed | Claude 单席真实 SIGINT 命中，attempt1=failed/cancelled、真实 exit0；一次 retry 后 attempt2=succeeded/exit0，session 改变，快照/请求配置/原尝试/历史前缀均不变。见 `exports/functional-claude-cancel-summary.json` 和 before/after history。双审保留成功席再只补失败席本次 **not_run**，不能从单席结果推导。 |
| 5 一轮交叉确认 | blocked | B01 dual 两席都有真实 P1，满足触发条件；但 confirm 会再次启动已复现隔离问题的 Codex，故未执行。不是 conditional_not_triggered，也不引用离线结果冒充实机通过。 |
| 6 新快照和真实测试证据 | passed | 相同 clamp 候选两轮都成功、0 findings；无测试时 user_decision/tests=incomplete，有实际通过且匹配候选的 12 项检查时 ready/tests=passed。之后真实 checkout 新候选并重新 freeze，旧两轮完整 show/history 前后逐字相等。 |
| 7 只读、目录与上下文隔离 | partial_passed / isolation_failed | 运行器记录 workspace_unchanged；两轮 Claude 功能审核实际 cwd 分别采样 222/221 次，摘要始终与冻结目录一致。Codex 实际外部记忆读取与全局 MCP 启动使闭合输入隔离不通过；无越界写入证据。 |
| 8 八样本三原生模式 | partial_collected | Claude-only 8/8；codex-only 1/8；dual 1/8。14 格依停止规则 not_run，各格原因在下表和 `matrix-blockers.json`。 |
| 9 官方插件模式 | blocked / not_run | 当前 `claude plugin list --json` 返回 []，普通列表明确没有插件。仅有官方 marketplace 注册，不算插件安装。8 格未运行；未安装、未改配置、未以禁用插件的 safe-mode 假称完成。 |
| 10 运行证据与质量评分 | evidence_passed / quality_pending | 请求和实际值分开；墙钟实测、Usage 保留报告值；空人工 judgments。8 格 pending、2 格工具自动 scored、22 格 not_run；整体人工质量验收未完成。 |
| 11 可复核持久证据 | passed | 外部 DB、JSONL、stdout/stderr、argv、脚本、导出和哈希清单完整保留；仓库只写脱敏文档。 |
| 12 完整 verify | passed_with_known_flakiness | 本轮仅运行一次完整检查，exit0：lint、format、typecheck、543 unit、16 replay 全过。规划方两次失败仍保留，已知测试可靠性问题未解决。 |
| 13 文档范围和交付 | passed | 仅本卡四份文档；文档候选 SHA、最终检查及工作区状态在交付聊天记录。未宣称阶段 1 全部通过。 |

单席取消轮次为 `d796bc254f6c418abe46ce2ceb601a8c`，信号只发给本次拥有的模块控制器 PID，原始取消终态与退出分开保留。未实测的双席取消保留成功报告、旧尝试迟到等时序仅有既有离线证据：`test_runtime_dual_cancel_preserves_completed_seat_and_retries_only_failed_seat`、`test_runtime_completed_before_cancel_preserves_report` 及 `tests/replay/scenarios/phase-one/dual-retry-late-result.json`；不计入本次真实通过项。

双审还具有实际回合重叠证据：上述 dual Codex 原始流第 28/340 行的 emittedAtMs 分别对应 `20:30:00.461Z` 的 turn/started 与 `20:30:22.383Z` 的 turn/completed；同轮 `reviewer-2/1/claude-c6228807a59b478793ce68c824a1c635.jsonl` 第 5–14 行 assistant/tool 活动时间 `20:30:00.955Z–20:30:09.038Z` 位于此区间，表明并非仅预检重叠。此证据仍不测量精确 GPU 推理并发时间。

## 测试证据门槛与新快照

| 观察 | 身份及结果 |
| --- | --- |
| 原候选 | `a422936140f7409ebda0315aa6ac0fc7f8670240`，独立一次性 B05 clamp 仓库，不是用户业务仓库。 |
| 无测试快照 / 轮次 | `06593a31a93aec6ee1ebf4ae6194bb2b183ec72c2268f72bd8652639d913f30f` / `07a27723cd64460fb9f64ccdf0438d5e`；user_decision。 |
| 匹配测试快照 / 轮次 | `cb3528232422437824c73bf3187d6ed1cdafa44258939d7966fb6fa38291bcc7` / `a9dee40172d749c9a642eea828a81c1a`；ready。 |
| 更新候选 / 快照 | `2a933e54b00f106cb4eddcb7ffa5cc7e5af6192f` / `66e6ed20a514f966986c21d9bd847c10e8e11e959181be7756080a0fbc2b397e`；旧完整报告/历史绑定保持不变。 |

外部 `functional/test_clamp.py` 由验收执行者实际运行，每个候选 12 项检查并记录真实 stdout/exit0，argv 绑定完整 commit 和源码 SHA-256。工作台只导入证据，没有自行实现阶段 2 的测试执行。产品字段 `source=user_supplied` 是既有导入格式；本轮测试的实际执行者为代理，不能把该标签解释为用户本人运行或人工质量确认。完整证据在 `functional/evidence-index.json`、`functional/live/summary.json`、`functional/live/*-workspace.json`。

## 八样本四模式

`collected` 仅表示实际首轮报告已采集；`受限` 表示 Codex 外部读取已复现。全部人工质量判断均未完成。

| 样本 | split | codex-only | claude-only | dual | official-plugin |
| --- | --- | --- | --- | --- | --- |
| B01 | development | collected/受限，20.674s，pending | collected，11.418s，pending | collected/受限，23.756s，pending | not_run：未安装 |
| B02 | development | not_run：隔离阻塞 | collected，11.881s，pending | not_run：隔离阻塞 | not_run：未安装 |
| B03 | development | not_run：隔离阻塞 | collected，10.209s，pending | not_run：隔离阻塞 | not_run：未安装 |
| B04 | development | not_run：隔离阻塞 | collected，11.367s，pending | not_run：隔离阻塞 | not_run：未安装 |
| B05 | development | not_run：隔离阻塞 | collected，13.392s，自动 scored | not_run：隔离阻塞 | not_run：未安装 |
| B06 | development | not_run：隔离阻塞 | collected，9.470s，自动 scored | not_run：隔离阻塞 | not_run：未安装 |
| B07 | heldout | not_run：隔离阻塞 | collected，11.595s，pending | not_run：隔离阻塞 | not_run：未安装 |
| B08 | heldout | not_run：隔离阻塞 | collected，12.409s，pending | not_run：隔离阻塞 | not_run：未安装 |

共 10/32 格已采集、11 份首轮席位报告、9 个 finding；14 格停止、8 格插件阻塞。首轮原报告没有被确认或补跑覆盖。Claude 八例合计实测主命令墙钟约 91.741 秒（包含预检和收尾，不含随后 show），不是单纯推理时间，不据此推荐模式。

`analysis/export-001/results.json` 从真实 show/history 与只读 DB 核对导出，`analysis/export-001/provenance.json` 保留来源哈希、请求配置、全部 Usage 序列及计数选择规则：保留首尝试最后一次报告的 counts，不把累计通知相加、不混合两提供方费用。所有已采集格都有报告计数；未知模型/档位仍为 null。

评分器真实输出 `commands/benchmark-score/stdout.txt` 的标签仍是 `imported_user_evidence/source=user_supplied/executed_models=false`，这是导入工具的固定语义，不能用该输出单独证明运行或人工参与。原始 CLI 证据另行证明本轮实际运行。`analysis/export-001/judgments.empty.json` 的 records=[]，没有把代理判断写成用户人工标签。

B05/B06 Claude 空报告被评分器自动标为 scored，六项指标原值均为 0；这些值原样保留，但**不是人工确认没有误报或漏报**。其他 8 个已完成格因缺人工标签仍 pending，所有模式汇总质量指标仍 null/pending，recommendation=null。四模式质量基准未验收，不给优胜模式结论。精确逐格记录见 `analysis/matrix-summary.json`。

## 证据索引、失败与剩余项

外部主要路径：

- `commands/`：每次执行的 argv、cwd、UTC 起止、控制器 PID、实际退出码、墙钟与 stdout/stderr 哈希；`scripts/` 和 `functional/*.py` 保留执行脚本。
- `data/reviews.sqlite3`、`data/logs/`：基准与取消轮次的持久状态、原始真实流；`run-index.json` 连接每格和完整 show/history。
- `functional/data/`、`functional/live/reviews-after-two-calls.sqlite3`：测试门槛/新快照功能轮次与 DB 副本。
- `exports/`：双审 in-progress/一席完成观察、真实取消信号及前后比较；未注入事件、未伪造停止。
- `plugin-inspection/inspection-summary.json`、`plugin-inspection/evidence-index.json`：当前插件列表、版本、只读配置摘要和来源；未安装插件。
- `analysis/native-evidence-audit.json`、`analysis/matrix-summary.json`：明确标为代理事实审计，不是用户人工质量标签。
- `evidence-manifest.json`：最终文件 SHA-256 清单；关键哈希另列交付文档。

规划方基线问题完整保留：`planner-verify-first-failure.txt`、`planner-verify-first-failure-fake-events.jsonl`、`planner-verify.log`、`planner-hook-failure.txt`、`planner-hook-failure-fake-events.jsonl`。同一产品 HEAD 第一次为 542 passed/1 failed，中间一次 543/16 全过，卡片提交 hook 再次失败，卡仍未提交。失败都在 `test_cancellation_during_termination_still_kills_and_reaps`：两份失败日志未等到 fake ready；一次未抛 CancelledError，一次 PID 文件尚未生成。现有证据支持测试 0.3 秒启动同步不足，未显示产品回收失败；这是未解决的测试可靠性问题，不在本卡修复、删测或掩盖。

最终 verify 本轮只运行一次，退出码 0：lint 通过，format 76 个文件通过，typecheck 32 个源文件通过，543 unit（45.67 秒）和 16 replay（0.14 秒）通过。原始 stdout/stderr/argv/退出码位于 `commands/final-verify/`；没有为提交重复运行到变绿，未改检查。

`analysis/completion-evidence.json` 保存最终只读 DB 备份、实际状态计数、逐原始日志的 spawn/exit 数量核对及已记录控制器 PID 查询。本次共 15 个席位尝试：14 成功、1 真实取消；所有记录的运行时启动都有退出记录，已记录控制器均已退出。运行时日志未记录子进程 PID，且无法据此排除主动脱离会话的后代，故不声称完整进程或全局隔离证明。

最终证据清单 `evidence-manifest.json` 包含 389 个文件，SHA-256 为 `ed204a4907f5e26edaf15e30f2219744d07481938efce42ac5ff7037bf4af9c6`。稳定 DB 备份在 `final-db/native.sqlite3`（11 轮/13 尝试）及 `final-db/functional.sqlite3`（2 轮/2 尝试）；关键哈希见交付文档。

剩余产品/验收项为 Codex 外部上下文隔离、受影响 14 格重验、官方插件准备与 8 格基准、真实双席取消保留成功席、交叉确认、人工质量裁定，以及上述离线测试可靠性问题。各项需原窗口审核后另行安排，不能从本轮局部成功推导整体通过。
