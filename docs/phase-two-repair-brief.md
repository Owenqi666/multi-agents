# 阶段 2 统一修复任务书

执行卡为 [T-0057](../tasks/T-0057-phase-two-repair.json)。盘点基线为 `70a819962727eab728ccdfd3434f31e0dd45f60d`。阶段 2 窗口执行，主窗口复核证据，最终接受或合并由用户决定。

## 剩余工作

| 编号 | 状态 | 工作项 | 关闭条件 |
| --- | --- | --- | --- |
| R1 | 已复现，待修 | Codex 规划复核将 `blocking_issues.uniqueItems` 发给严格结构化输出接口。T56 的真实 `turn/start` 因此收到 HTTP 400 `invalid_json_schema`，没有产生规划决定。 | 真实规划复核接受请求并产出可校验的决定。宿主仍拒绝重复阻塞项、错误任务哈希和不一致的决定。 |
| A1 | 待实机验收 | 在打印预览样例上走通规划、首次实施、初测，以及一次默认代码审核调用内的返工、复测和复审。 | 能沿同一任务从已核实的初审阻塞追到新候选、原完整测试及新的审核轮次，并给出最终质量结论。 |

盘点得到 **1 个已确认缺陷、1 项端到端验收**。T53 的可执行文件大小限制、T54 的合法异步通知、T55 的自动编排及两处验收修复、T56 的 Claude 草拟方言错误均已修复。四项 S9 权限验证沿用各自原始提交的结论。初始实施、初测、初审、返工、复测和复审在 T56 实机中均未到达；它们是 A1 的检查点。当前样例的 Claude 实施与返工共用一份输出定义，Codex 代码初审与复审也共用一份，这两份定义均不含引发 R1 的字段。[阶段路线](phase-two-development.md) · [T55 交付](deliveries/T-0055.md) · [T56 实机报告](</Users/owen/Desktop/Multi-agent-acceptance/phase-two/T56-native-70a819962727/REPORT.md>)

## 修复与回归

1. 在实际 Codex 规划复核请求的发送边界，从冻结输入生成运行时支持的结构化输出描述。保留规范 `REVIEW_SCHEMA`、冻结文件、宿主 `parse_planning_review` 的全部校验，以及 T56 已实测的 Claude 草拟行为。
2. 经规划入口用假 CLI 对照实际出站参数。以命名测试复现原拒绝，证明修复后可接受合法决定、拒绝重复阻塞项等非法返回，记录正反例及原错误。沿实际角色检查其他发送点，按证据修正。运行仓库完整 `scripts/verify.sh` 并正常提交。
3. 本任务书持续覆盖同一功能链内新发现的产品阻塞。发现后在下方台账追加首次失败原件、原因、修复、回归及复验证据。由阶段 2 窗口连续执行；需要调整写入范围时主窗口按实际路径修订同一卡。可恢复的环境或设置问题在既有产品预算内恢复。对稳定复现的同一错误，先修复再复验。

## 实机验收

从样例提交 `695cfcdea1f50395312f0421aad9966dda2f46fb` 创建隔离副本，核对原始需求、规则、受保护的 build/test、席位配置及实现总预算 2。新运行使用独立证据目录，记录产品提交与工作区状态。沿正常产品入口使用真实运行时；合理的规划修订或可恢复重试遵循原有规则并留存记录。

| 检查点 | 所需证据 |
| --- | --- |
| 规划与首次实施 | Claude 草拟卡通过原校验，Codex 规划复核及宿主业务校验完成。记下初始候选、实施尝试和实际结果。 |
| 原完整测试 | 在初始候选上按原声明顺序实际执行 build、test；保留名称、程序、参数、退出码和失败断言。 |
| 初审与一次返工 | 一次默认 `task-review run` 看到真实的范围内阻塞。记录引用核实、唯一返工预留和执行尝试，以及新候选和直接父提交。 |
| 新候选复测与复审 | 核实新候选按原完整顺序、argv 和策略实际重跑 build、test；新审核绑定本次复测、继承原席位配置。记录二审结论、最终退出码，以及有无第二次返工。 |

分别报告流程是否执行到复审、修后测试结果与复审质量结论。若模型未给出合格阻塞，A1 记为未验证，不为抽取指定结论重复审核。若二审仍有阻塞，标明流程已走到复审以及质量判定未通过。每次产品修复提交前完整运行 verify；最终产品提交上从样例基线重新实机验收。可将最终实测结果写入仓库外的固定台账回执，由本文链接，以免仅为回填结论再改变被测提交。

运行时未报告的实际模型、档位或用量记为未确认。用 CLI 回执、SQLite 历史、Git 提交关系及测试/审核日志相互核对；保留 T55、T56 的失败原件。真实结果和原件不包含登录凭据。

## 执行台账

| 项目 | 初始证据 | 结果与复核依据 |
| --- | --- | --- |
| R1 | T56 规划运行 `ceb0e540839d4e34812adad5a6c962cd` 的 Codex 服务端错误。[原始审计](</Users/owen/Desktop/Multi-agent-acceptance/phase-two/T56-native-70a819962727/audit.json>) | 规划复核的 Codex 出站副本去除 `blocking_issues.uniqueItems`，冻结和宿主校验继续使用原件。真实提交与复核决定见[总回执](</Users/owen/Desktop/Multi-agent-acceptance/phase-two/T57/RESULT.md>)。 |
| A1 | T56 通过 Claude 草拟，尚无候选或代码审核记录。 | 本轮各次尝试、候选、实际 build/test 和审核结果按提交登记于[总回执](</Users/owen/Desktop/Multi-agent-acceptance/phase-two/T57/RESULT.md>)。 |

基线 `70a8199` 上，`test_codex_planning_review_strict_wire_preserves_host_constraints` 经规划入口重现了旧协议失败。假服务端读取实际发送的 schema，确认回合后才发出 400 和失败终态，进程正常退出。修复后该用例核对实际出站副本与冻结文件的唯一差异，并证明宿主仍拒绝重复阻塞项、错误哈希和带阻塞的 ready 决定。`test_nonplanning_codex_preserves_array_constraints_on_wire` 覆盖具有同名字段的非规划调用。[基线失败记录](</Users/owen/Desktop/Multi-agent-acceptance/phase-two/T57/r1-baseline-corrected.txt>) · [定向检查](</Users/owen/Desktop/Multi-agent-acceptance/phase-two/T57/r1-focused.txt>)

T56 回归此前要求 Codex 规划复核 wire 与冻结文件完全相等，R1 改变了这一出站契约。本次将该断言更新为先核对冻结文件的 `uniqueItems=true`，再核对除该项外的完整 wire。Claude 草拟、适配器构造入参与冻结原件相等、宿主校验、检查脚本和回放均沿用原检查。R1 改动路径为 `adapters/runtime.py`、`cli/planning_runner.py`（均位于 `src/multi_agent/`）、`tests/unit/runtime_fakes/planning_cli.py`、`tests/unit/test_planning_runner.py`、`tests/unit/test_runtime.py`、本任务书和 T57 任务卡。
