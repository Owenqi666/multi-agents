# 工作台 P1–P5 联调 · 后端核对答复

核对日期为 2026-11-25，基于 `main` 的 `83de584` **及当前未提交的前后端实现**。A 对应[前端联调总结第 3 节的 18 条假设](workbench-frontend-integration-summary.md)，Q 对应其第 7 节的 22 问，F 对应[后端待办第 1–9 条](workbench-backend-followups.md)。本文记录 11 月 25 日当时的判定。11 月 26 日已落地的后端改动、对前端三项追问的答复及演示回放位置，见[项目状态与前端交接](workbench-frontend-handoff.md#11-月-26-日联调交接)。

## 本轮核对操作与结果

| 证据 | 检查方式 | 结果 |
| --- | --- | --- |
| 前端已完成的真实联调 | 对照浏览器侧 `evidence.json`、20 个作业的持久化记录、活动日志和打包的 SQLite 证据。原始材料在本机被 Git 忽略的 `frontend/e2e/records/real-trial/`。 | 31 个观察点，43 张截图。20 个作业中 11 个启动过模型会话。按作业、资源、席位和尝试次数去重，有 17 次 `session_started`，其中 **Codex 启动 14 次（13 次有完成通知、1 次硬停），Claude 启动 3 次**。F9 的 13/4 混淆了计数。三份独立 Claude 运行结果报告了模型，分别是两次阶段卡片复核和步骤 20 的草拟；均未报告推理等级。 |
| 真实主流程 | 核对第 7、8、9、11、14、19、20 步的领域结果，区分作业退出、审核结论及候选身份。 | 第 7 步是原候选的测试失败。第 8 步硬停，第 9 步在原轮次和快照补跑成功，审核仍要求返工。第 11 步实施者报告超出范围。修订计划后的**另一个候选**在第 14 步通过测试，第 19 步集成被接受。第 20 步未显式指定复核模型，真实默认路径成功。完整作业 ID 和提交参数见前端总结第 4 节。 |
| 本次新增的离线 HTTP 实验 | 临时 Git 仓库、真实 Web 服务及 SQLite、替代 CLI。对**相同候选、实施尝试 1、测试声明和命令**连续提交 `task.test`。 | 首次测试尝试 1 失败，作业退出 1。只改变服务传入的工具目录后再提交，作业退出 2，原因为 `test retry must preserve its managed policy`。恢复原策略并让替代命令正常执行，测试尝试 **2** 成功，作业退出 0，候选提交与第一次相同。无需再次实施。 |
| 本次针对性测试与样例核对 | 运行仓库已有的仓库/粘贴补丁意见绑定、旧作业恢复、首次接入 HTTP 用例。扫描四个演示场景的录制索引。 | **5 项通过**。四份索引共 216 条请求，六个新 GET 和判断 POST 的匹配数均为 0。录制索引与可实时操作的替代 CLI 环境是两套证据。 |

本次没有再次调用真实模型。真实模型未覆盖集成返工、带阻塞意见的返工资格、交叉确认、关联修复的实际提交、粘贴补丁审核、委派阶段及子卡。集成审核中断后的补跑也仅有替代 CLI 验证，真实补跑发生在任务审核。在主仓库的 `docs/` 和留存记录目录中未找到单列的 `real-trial-summary-2026-11-25.md`；20 步表和原始证据均可核对。

前端总结里标记“未见文档”的项目，有几条已在[既有接口交接](workbench-frontend-handoff.md)部分约定过，例如完整 `href`、同一作业内用 `sequence` 去重，以及讨论和原审核结论分别呈现。粘贴补丁返回空修复基线也已有明文说明。第 6 节的“只有组件测试”应限定为前端覆盖范围，后端已有接入状态、过期 anchor 和粘贴补丁等用例。第 2.6 节应引用[按验收收据导出的 HTTP 测试](../tests/unit/test_integration_verification_cli.py)；原列的 `test_workspace_delivery.py` 覆盖的是候选文件与提交的交付。

## 22 个问题与 18 条假设逐项答复

| 问题 / 假设 | 后端答复 | 前端用法或改动归属 |
| --- | --- | --- |
| Q1 · A3　待办原因 | **是，当前 20 个逐个相符。** 目标/计划有 `confirm_goal, consultation_failed, plan_goal, retry_project_plan, choose_stage`。执行有 `dispatch_failed, planning_attention, planning_question, implementation_attention, task_test_failed`。审核/集成有 `integration_conflict, rebuild_integration, inspect_candidate, test_integration, integration_test_failed, inspect_tests, review_integration, review_incomplete, review_decision, accept_integration`。 | 保留未知原因显示 `detail`。F7 中的三个新原因属于提案。[后端生成器](../src/multi_agent/web/attention.py) · [前端文案](../frontend/src/pages/Attention.tsx) |
| Q2 · A1　行标识与导航 | **站内路径是；组合键在当前受支持的数据路径上可用。** 阶段、子卡与单卡问题分别用查询参数区分。同一计划下不同阶段共享计划 ID。接口没有另发持久行 ID 或承诺将来仍用这套组合。 | 渲染键包含完整 `href`，导航保留查询字符串，跨次查询以最新列表为准。[待办入口](../src/multi_agent/web/attention.py) |
| Q3 · A2　部分响应 | **“只在作业占锁时”不严格成立。** CLI 作业登记后到结束前，或服务关闭读入口时，都可能得到 `complete:false`。返回的是能从当时记录推导的子集；归档返回完整空列表。多个查询及随后点击不构成一个原子快照。 | 可保留旧行并标待刷新。不能据此宣称操作仍可执行，或把部分空列表显示为全部完成。[读协调](../src/multi_agent/web/gate.py) · [待办读取](../src/multi_agent/web/attention.py) |
| Q4　空闲时串行读取 | **会占服务端读入口；客户端串行是调度选择。** 服务端已经串行化需要现场核验的读取。 | 可以保留现有排队，忙碌时仍可请求部分列表。[ReadGate](../src/multi_agent/web/gate.py) |
| Q5 · A4　事件序号 | **是，在同一作业正常追加的私有日志内成立。** `sequence` 是完整 JSONL 行结束处的字节偏移，可去重并排序。服务重启后读同一文件，序号不变；相邻序号可以相差很多。 | 以 `(job_id, sequence)` 合并。[日志读取](../src/multi_agent/web/activity.py) · [日志写入](../src/multi_agent/job_activity.py) |
| Q6　保留 50 条 | **否。** 文件持续追加。一次读取最多扫描文件尾部 4 MiB，再返回最近 50 个事件。任一窗口截断都会置 `history_truncated`。 | 将“服务只保留最近 50 条”改为“本次返回最多 50 条”。页面缓存 500 条、列表显示最近 100 条是前端策略。[后端窗口](../src/multi_agent/web/activity.py) · [页面](../frontend/src/pages/JobActivity.tsx) |
| Q7 · A5　恢复提交及错误 | **是，原样发送返回的 `action,args`。** 结构合法但 `expected_attempts` 已过期时，POST 通常先返回 **202**；GET 作业返回 200，终态 `finished`、`exit_code:2`、`error.code:cli_refused`。参数格式不对可直接收到 400 `invalid_request`；并发作业也可在提交时收到 409 `busy`。 | 检查终态并刷新；沿用返回的确认子轮 ID、席位、尝试编号和可选 `recover`。[执行时检查](../src/multi_agent/cli/runner.py) · [恢复用例](../tests/unit/test_web_job_activity.py) |
| Q8 · A6　恢复动作的刷新 | **否。** 新作业开始、旧控制器释放日志或咨询锁、另一个 CLI 更改当前席位或测试，都可改变恢复资格。这些变化可能发生在当前页面最后一次收到作业终态之后。 | 已中断且仍有控制器时继续低频刷新；页面回到前台或用户手动刷新时重读，提交后处理过期结果。[恢复资格](../src/multi_agent/web/activity.py) · [当前触发方式](../frontend/src/pages/JobActivity.tsx) |
| Q9 · A7　意见身份与过期提交 | **五字段是；统一报错否。** 完整身份包含 `round_id,snapshot_id,seat,attempt,finding_id`。同一轮内可用后三项找回成员。被替换的是所选席位的尝试时，旧 anchor 无效；其他席位补跑不自动使它过期。直接 POST 判断通常是 400 `invalid_request`；合法形状的追问或规划可在 202 入队后由 CLI 拒绝。 | 保留完整 anchor。三类写操作分别处理同步 HTTP 错误与异步作业错误。[身份验证](../src/multi_agent/store/review_annotations.py) · [Web 动作](../src/multi_agent/web/actions.py) |
| Q10 · A9　附注与审核结论 | **是，三种附注本身都不修改原轮的 `status/reasons`。** 同一原轮的视图仍可因席位补跑、交叉确认或确认不完整而变化；对后来候选的测试影响当前资格。 | 在界面分别显示人工判断和原轮评估。[评估生成](../src/multi_agent/cli/runner.py) · [意见读取](../src/multi_agent/web/review_findings.py) |
| Q11 · A8、A10　讨论读取与条目 | **是。** 只在原始审核轮读取 findings；确认子轮请回原轮。当前数据库约束仅允许 `decision,answer,linked_plan`。成功的回答挂回同一 anchor，计划条目带 `planning_run_id`；规划执行失败也可能已经创建可重试的关联计划。 | 操作完成后重读原轮。已有归档受其来源版本限制。[迁移约束](../src/multi_agent/store/migrations/0014_review_finding_activity.sql) · [HTTP 用例](../tests/unit/test_web_finding_followup.py) |
| Q12 · A11　关联修复基线 | **是，已由实现和参数化用例确认。** 仓库来源返回被审候选提交的 SHA。粘贴补丁即使绑定了当前仓库，`source.correction_baseline` 仍为 `null`。 | 前者锁定基线，后者由用户明确选本仓库提交。此项已有后端覆盖。[字段来源](../src/multi_agent/web/review_findings.py) · [两类来源的测试](../tests/unit/test_web_review_findings.py) |
| Q13 · A11　基线不相等 | **仓库来源是；粘贴来源无需与 null 相等。** 校验的是解析后的 Git 提交，指向同一 SHA 的分支名也能匹配。错仓库或错候选会拒绝。粘贴补丁仍须选择有效基线，并满足已有规划要求。 | 处理异步作业拒绝；UI 可直接使用服务给出的 SHA。[事务内的关联检查](../src/multi_agent/store/planning_store.py) |
| Q14 · A12、A14、A16　作业运行期间读取 | **四个 GET 均可发送**，findings、activity、delivery、onboarding 均不通过用于串行化仓库操作的 `ReadGate`。activity 会读私有日志锁和咨询所有者，onboarding 会读 Git 与已有环境观测。**返工资格**单独经过读入口；忙碌时只保证 `integration_id,eligible:false,lock_busy:true`。 | 四个普通读不必等作业结束。返工预检可以等待。预检复用执行逻辑，但状态可变，两次拒绝的文字也不保证相同。[HTTP 路由](../src/multi_agent/web/server.py) · [预检](../src/multi_agent/web/review_findings.py) |
| Q15 · A13　返工结果 | **成功创建子集成时是；每个回执都有子 ID 则否。** 可读 `result.rework.child_id` 或 `result.revision.integration_id`。拒绝、取消或没有产出修订时可能没有 ID；有子 ID 也不代表随后的重测与复审通过。 | 可以跳至子集成，同时显示后续验证结果；无子集成时显示原因。[实际返工路径](../src/multi_agent/cli/integration_autorework.py) |
| Q16 · A15　交付摘要的稳定范围 | **验收事实固定；整个响应逐字节固定则否。** 已知且支持验收的集成在验收前返回 **409 `job_unavailable`**。验收后使用收据指定的测试尝试、不可变成员和审核快照。目标分支移动不改变这些事实；换端口后 Markdown 中的本机绝对链接会变。不存在或不属于本仓库的记录通常为 404，旧归档还可能是 `archive_unsupported`。 | 对跨实例结果比较收据、SHA 与证据哈希。页内导航使用相对 `links`。[摘要](../src/multi_agent/web/delivery.py) · [实际验收测试](../tests/unit/test_integration_verification_cli.py) |
| Q17 · A14　接入状态枚举 | **规则和下一步清单是；供应方清单漏了服务端 `checking`。** 规则 5 项为 `no_commit,missing,invalid,no_commands,ready`，供应方 6 项为 `missing_cli,checking,check_failed,login_required,ready,unknown`。下一步 8 项为 `commit_baseline,add_rules,fix_rules,register_commands,install_cli,check_provider,sign_in,create_goal`。服务的供应方为 `codex,claude`。 | 页面已经支持 `checking`，只需修正接口说明。打开面板不触发检查；显式 `environment.refresh` 按供应方运行。[状态生成](../src/multi_agent/web/onboarding.py) · [页面映射](../frontend/src/pages/Onboarding.tsx) |
| Q18 · A17　已结束与业务成功 | **没有通用的“动作成功”字段。** 先看 `state,error`，再看对应 `result`。真实第 9 步两个当前席位都 `succeeded`、轮次 `ready_for_assessment`，但 `assessment.status=needs_rework`，退出码 1。第 7 步的 `verification=verified` 表明失败的测试结果有可核验的证据。 | 审核页分别展示席位和结论，测试页展示测试阶段与命令退出码。环境检查与中断作业可无退出码。[CLI 回执解码](../src/multi_agent/web/jobs.py) · [审核退出语义](../src/multi_agent/cli/task_review_workflow.py) |
| Q19　实施者报告 | **是，已成功解析的实施报告只接受 `completed,scope_blocked`。** 没有有效报告时 outcome 可缺失。真实第 11 步运行本身成功，实施者报告 `scope_blocked`，任务转为 `planning_required`，当前候选为空。 | “实施者报告”的标签恰当。交付另读 candidate 和 verification。[报告定义](../src/multi_agent/cli/task_prompt.py) · [输出解析](../src/multi_agent/cli/task_output.py) |
| Q20　13 个状态和 13 个事件 | **否，三个字段分属不同集合。** 目前活动写入 12 种事件，详见下表。审核席位为 `pending,running,cancelling,succeeded,failed`。actor 的来源状态集合包括 `pending,ready,running,cancelling,awaiting_evidence,succeeded,failed,cancelled`，读接口还可派生 `unconfirmed`。作业的 `queued,finished,interrupted` 单独读取。 | 前端文案可以是超集，保留未知值回退。勿以标签表作封闭协议。[观察事件类型](../src/multi_agent/core/events.py) · [活动组装](../src/multi_agent/web/activity.py) |
| Q21　e2e 测试扩展点 | **可暂时作为仓库内测试依赖；当前没有公开扩展契约。** 实际替换五个名字，含 `fake_runtime._planning/_response` 与 `offline_env._serve/create_fixture/SCENARIOS`。后两个没有下划线，同样有变更成本。 | 后续调整测试支持代码时联跑 workbench e2e。若要解除耦合，优先给测试启动器显式传入 fixture、响应策略和服务就绪回调，无需改生产 HTTP API。[前端环境](../frontend/e2e/workbench_env.py) · [后端测试启动器](../tests/unit/web_support/offline_env.py) |
| Q22　F1–F9 取舍 | **F2、F7、F8 是已确认的首页功能缺口。** 其余六条各包含现有设计、兼容边界或改进建议，归属见下节。 | 请分别跟踪接口改动、页面说明、演示录制及运行环境。 |

**A18（没有单独的 Q）成立，但依赖同一仓库和完好的工作台数据目录。** 页面刷新和服务重启后仍可按原作业 ID 获取详情，之前活跃的作业会登记为中断。列表的最近条数限制不等于按 ID 删除记录。换一份全新的数据目录或仅打开 SQLite 归档，不会自动带入原作业目录。[持久化和恢复](../src/multi_agent/web/jobs.py)

| Q20 的事件来源 | 当前写入值 |
| --- | --- |
| 执行者、供应方和测试 | `attempt_started,attempt_settled,provider_wait,provider_admitted,test_command_started,suite_settled` |
| 从模型适配器转发 | `session_started,message,usage,completed,error,process_exited` |

`cancel_requested` 是审核领域事件，当前没有将其写入该活动日志的调用。用户请求取消可以通过作业状态看到。[活动写入](../src/multi_agent/job_activity.py) · [适配器转发](../src/multi_agent/adapters/runtime.py)

## F1–F9 处置建议

| 项 | 判定与责任 |
| --- | --- |
| F1 单卡默认模型 | **默认与预设不一致，真实默认调用成功。** 非必选字段缺省时单卡 CLI 使用 `gpt-6.1-sol/high`，页面列出的 Sol 为 `gpt-6.2-sol`。后端应让有效默认可见，前端据此展示，替代 CLI 应覆盖省略参数。是否升级默认模型另作决定；独立 probe 的选型不必跟随表单改动。[单卡默认](../src/multi_agent/cli/task_cli.py) · [界面预设](../src/multi_agent/web/workspace.py) |
| F2 硬停审核没有待办 | **后端优先修。** 仍标为 running 的席位可使首页判为进行中，而作业活动已给出可接管动作。让首页复用同一套归属与尝试判断，链接到有恢复按钮的作业。验收应同时覆盖控制器仍存活、已释放、补跑完成三个时点。[待办](../src/multi_agent/web/attention.py) · [恢复](../src/multi_agent/web/activity.py) |
| F3 混合语言 | **观察属实，前后端协调。** 当前 delivery 固定中文，待办说明含固定英文和用户内容，交付路由也不接受 `lang` 查询参数。若支持双语导出，后端加可选语言并保留缺省行为；页面翻译稳定原因码，保留用户原文。可先标注当前导出语言。[摘要](../src/multi_agent/web/delivery.py) |
| F4 静态演示 | **录制数据与前端回放缺口。** 四套样例都缺新接口。后端维护的录制脚本要先纳入相应 GET 和判断记录；前端 mock 增加 decisions POST 的回放。替代 CLI e2e 通过不能证明静态演示已展示这些面板。[录制器](../tests/unit/web_support/record_samples.py) · [回放层](../frontend/src/mock/transport.ts) |
| F5 沙箱找不到可用解释器 | **真实环境问题，接入检查只验证了规则。** 第 7 步封存的 stderr 有 Xcode 工具诊断。修正工具目录后成功的是另一个候选。建议把“命令已登记”和“运行环境已验证”分开显示。按实际沙箱 PATH 查找能发现缺失，不能单靠存在或 `/usr/bin` 路径判定占位程序是否可用。[规则读取](../src/multi_agent/web/onboarding.py) · [测试隔离](../src/multi_agent/adapters/test_isolation.py) |
| F6 同一候选重测 | **现有后端支持有条件重测，页面缺少入口；更换策略是另一个后端需求。** 本次 HTTP 实验已验证两条路径。下一步可先暴露“同策略、当前失败已停止、候选仍有效”的重测资格；若要允许更换工具目录，需记录新策略及新测试证据，保留原次结果，并按新测试判断当前审核资格。已经耗用的返工额度和 `scope_blocked` 后的历史候选属于独立产品决策。[任务下一步](../src/multi_agent/cli/task_cli.py) · [测试保留策略](../src/multi_agent/cli/task_test_context.py) · [状态转换](../src/multi_agent/core/transitions.py) |
| F7 阶段正常推进缺待办 | **后端修。** 任务有候选待测试、测试完成待审核、直接阶段通过审核待创建集成，均应对照详情页已有资格来生成入口。第三个时点有真实现场记录；前两个来自源码核对。由前端补新原因的文案。[待办分支](../src/multi_agent/web/attention.py) |
| F8 修订后的陈旧待办 | **后端修。** 普通旧阶段没有按新版本实际领取情况退出待办；现有特殊排除仅针对分派失败。按同一计划修订链与 `stage_id` 判断替代，只在新版本已实际接管时移除旧待办，旧任务详情继续可查。[阶段查询](../src/multi_agent/web/attention.py) |
| F9 Codex 实际模型与等级 | **保留“未确认”的证据口径，修正会话统计。** 这次捕获的 `thread/start` 返回会话配置，`turn/start` 可覆盖模型或等级；这些字段不能单独证明某一轮的最终执行。已用现场版本 `codex-cli 0.169.0` 生成并核对本机协议类型。官方协议另有带 turn ID 的 `model/rerouted` 事件，项目已单独解析其路由证据。本次 14 份 Codex 会话记录均未观察到该事件。当前项目没有把会话配置写进 `reported_model/reported_effort`；不宜用请求值补空。[项目适配器约定](../src/multi_agent/adapters/base.py) · [官方 App Server 文档](https://developers.openai.com/codex/app-server/) |

### 追问里为什么会说“没有提供实现”

真实追问对应的冻结快照中，`source/candidate/calc.py` 非空，差异也包含该文件。后端把材料放入了只读工作目录。该次发给模型的文本包含意见、意见里的引文和用户问题，没有附相关 diff，也没有指明 `diff.patch` 或源码位置。留存协议记录只见用户消息与模型答复，没有读文件或执行命令的记录。可确认的是材料可用，而该次回答没有表现出读取完整实现的证据。后端可在提示里列出冻结文件位置并附上有界的相关行，再用需引用实际改动才能答对的问题验证。现有替代 CLI 的固定答复只能验证问答挂接。[追问入口](../src/multi_agent/cli/finding_followup.py) · [材料生成](../src/multi_agent/cli/snapshot.py) · [对应测试](../tests/unit/test_web_finding_followup.py)

### 本次离线测试命令

```sh
.venv/bin/python -m pytest -q \
  tests/unit/test_web_review_findings.py::test_decisions_and_linked_plans_are_bound_to_the_reviewed_evidence \
  tests/unit/test_web_job_activity.py::test_legacy_interrupted_retry_requires_an_identified_seat_attempt \
  tests/unit/test_web_onboarding.py
```

同一候选重测另用临时仓库从 `POST /api/jobs` 提交，结果见首表。前端此前报告的组件与浏览器 e2e 结果沿用其记录。本轮新增的是这份核对文档，产品代码、演示录制和原始联调记录均未修改。
