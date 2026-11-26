# 工作台优化项目状态与 Claude 前端交接

此文档对应[工作台优化项目书](workbench-improvement-plan.md)。以下页面入口均属于仓库根目录的 `frontend/src/`。

**截至 2026 年 11 月 26 日，P1–P5 后端和前端均已在本地工作区接入。** 11 月 25 日的真实模型联调、后端对照答复和前端修订记录见[前端联调记录](workbench-frontend-integration-summary.md)与[后端逐项核对](workbench-backend-integration-review.md)。本页汇总联调后的接口变动、复验结果和前端处理约定。

## 11 月 26 日联调交接

### 前端第二轮反馈的后端答复

**1　区分尚未启动和已耗尽预算的关联规划。** 相同请求仍返回原 `planning_run_id` 和 `reused:true`。页面打开该计划并读取 `GET /api/resources/plan/{id}?implementation=1`。

| 原计划状态 | 后端结果和前端动作 |
| --- | --- |
| `pending`，草拟与复核均无尝试 | 已关联意见的单卡规划返回 `next_action.kind="plan_retry"` 和 `retry_configurable=true`。用户可点现有「重试规划」，提交 `plan.retry`，参数只有原 `planning_run_id`。首次准备沿用已保存的需求、基线和模型请求，采用当前服务的 CLI 配置并冻结执行策略。两个控制器争抢首次准备时只有一个能启动。已有 v14 记录也可恢复。 |
| 已经启动且还有重试预算 | 继续使用原计划、原执行策略和剩余额度，按规划详情提供的动作操作。 |
| 草拟或复核的冻结预算已经耗尽 | 规划详情返回 `budget_exhausted`；可操作视图中 `retry_configurable=false`。强行提交 `plan.retry` 返回 HTTP 409。相同关联请求仍指向原 ID，不会开启新额度。确有新的修复要求或模型请求时，可从原意见发起一份带新输入的规划。使用同样输入给原计划追加预算，目前没有这个操作。 |

在临时仓库里，服务重启后沿原 ID 成功完成草拟和复核各一次，计划进入 `ready` 且可派发。用原 CLI 手工启动这类尚未准备的计划，需额外提供 `--repository` 和新的 `--directory`；通过 HTTP 时由服务补齐。页面已有的「重试规划」按钮可以直接使用这些字段。`reused` 只表示找回了哪份计划，是否可以运行查看该计划的 `next_action`。[同一 ID 恢复、预算与并发回归](../tests/unit/test_correction_idempotency.py)

**2　页面可以写「被拒绝」。** 在临时仓库里让另一个规划控制器占用同一把锁，再提交 `plan.create`。HTTP 先返回 202，作业随后以退出码 2 结束，返回 `cli_refused`、`error_kind="refused"`、`result=null`。服务重启后仍能读取刚才保存的规划、原始需求及两组空尝试。这里的「拒绝」指控制器未取得继续执行所需的锁。页面应重读已有记录，避免承诺「没有执行」或「没有写入」。[HTTP 重启回归](../tests/unit/test_web_project_flow.py)

**3　三处演示已录到动作完成后的状态。** 每个场景都在下一次写操作前，用原来的对象 ID 重读。可用以下 `?mock=场景&at=步骤` 对照，并已通过 Chromium 页面检查。

| 场景 | 动作前后 |
| --- | --- |
| `rework` | 55 的父集成可返工。71 的[父集成详情](web-api/samples/rework/082.json)写明 `superseded_by` 对应的子集成。72 的父集成页同时显示子集成链接和返工已不可用，78 仍可看子集成交付。 |
| `cancel` | 41 的原作业活动显示执行者运行中。48 的作业详情返回退出码 130，49 的[原作业活动](web-api/samples/cancel/060.json)显示作业已结束、执行者最后事件为 `attempt_settled`，且没有恢复动作。执行者的最后状态记录为 `failed`，作业结局为已取消。55 仍是另一个服务重启后中断的作业。 |
| `dual-partial` | 49 的原作业可以补跑缺失席位。54 的[原作业活动](web-api/samples/dual-partial/063.json)保留首次执行记录，并显示当前审核两席均已成功，缺失席位已完成第 2 次尝试。此时 `recovery=[]`，旧补跑按钮消失。 |

四个场景合计 269 次 HTTP 交换，已分别在独立临时仓库重录并逐文件比较。现有离线演示浏览器用例以及上述三处的 Chromium 核对均通过。指定 `at` 时，页面展示截至该步已录到的最后一份响应。按既定静态回放约定，普通模拟作业和取消操作沿用当前时间点，只有已匹配的人工判断会推进读切片。因此验证操作前后可分别打开表中时间点，或打开场景末尾。实际用现有回放层在返工提交已录到的第 68 步发起返工，模拟作业结束后页面仍停在第 68 步，直接打开第 72 步才会读到子版本。

**4　取消不保证有错误对象。** `cancel` 场景的取消回执是 `state="finished"`、`exit_code=130`、`result.phase="cancelled"`、`error=null`、`error_kind=null`。CLI 输出了完整领域结果，页面可按退出码或结果显示「已取消」。另一条路径是 CLI 因中断而没有输出领域结果，此时服务给出 `error.code="cli_cancelled"` 和 `error_kind="cancelled"`。沿用页面现有判定。[取消回执](web-api/samples/cancel/044.json)

### 11 月 26 日产品决定与真实追问复验

1. **已有审核也可按原环境重测。** 对当前实施的当前测试，以 `details.retry_ready === true` 作为重测资格。任务页应直接从 `task.tests` 选择对应记录，取消对 `next_action.kind` 和 `next_action.test_attempt` 的依赖。审核后下一步可能是返工或用户决定，且没有测试编号。使用这条测试的实施编号、原命令顺序和声明来源提交。归档仍只读，运行中的作业沿用现有互斥。该决定只覆盖相同测试策略；改换工具目录属于另一个功能。重测结束后，原审核保留供查阅，新审核绑定新的测试次数。前端已按这一决定更新[重测入口](../frontend/src/pages/TaskPage.tsx)及[组件测试](../frontend/src/test/task-evidence.test.tsx)。后端已有[同一候选重测再审的回归](../tests/unit/test_task_review_workflow.py)。
2. **静态演示维持录制回放。** 普通作业按样例里该动作的录制顺序取结果。人工判断匹配审核轮、意见坐标、决定和理由。追问匹配意见和问题原文，关联修复匹配意见，环境检查匹配提供方。匹配到的响应始终来自录制，即使表单里改了模型或需求，演示结果也仍取自样例。保留已有的全页演示提示。[回放实现](../frontend/src/mock/transport.ts)
3. **真实模型追问已复验一次，通过原问题的验收。** 使用 11 月 25 日原数据的 SQLite 副本，通过本机服务的 `POST /api/jobs` 提交 `review.followup`。原意见、问题原文、冻结快照和审核席位均相同。作业 `2fb4aa2299ef413bb851b31ba95dc1b0` 用时 17.7 秒、退出码 0。请求的是 Codex `gpt-6.2-sol / high`，CLI 版本为 `0.169.0`；模型和 effort 的运行时回报为空。

| 单例核对项 | 原始证据与本次回答 |
| --- | --- |
| 能否看到候选实现？ | 上次回答称冻结资料没有提供 `calc.py`。这次正确指出模块顶层定义了 `subtract(a, b)`，测试导入与定义一致，`subtract(2, 3)` 得到 `-1`，原有加法用例仍在。冻结差异实际添加了 `return a - b`，回答正确给出了运算结果。 |
| 如何解释测试失败？ | 冻结记录为退出码 1、stdout 为空、stderr 有 `xcode-select` 工具链报错。回答将这次观测到的失败归到环境，并明确现有记录无法确认两条用例确实运行并通过。 |
| 是否查阅了材料并保留原结论？ | 协议记录显示审核者读取了 `diff.patch`、`review-input.json` 并计算了两份冻结日志的摘要。副本中同一意见的回答从 1 条增到 2 条，审核状态仍是 `needs_rework`，原因与追问前一致。 |

原意见为 `T-0001-UNIT-EXIT`，原审核轮 `0ff9d899b0c24df4bf713114fa3cff58`，席位 `reviewer-2` 第 2 次尝试，冻结快照 `7e152a11524d81f3953b92b7e2f36e299faf542a9decb08931eab1b56cd9ee96`。本次复验只覆盖该意见下的一次真实 Codex 回答。前端 11 月 26 日上一轮接入及其测试结果详见[前端联调总结第 10 节](workbench-frontend-integration-summary.md)。

### 前端请后端确认的三件事

| 页面问题 | 后端答复与页面用法 |
| --- | --- |
| 作业如何区分拒绝和故障？ | 读 `GET /api/jobs/{id}` 或 `GET /api/jobs` 的 `error_kind`。有错误时，它是 `refused`、`failed`、`cancelled` 或 `interrupted`。例如同为退出码 2，过期请求的 `cli_refused` 是 `refused`，追问没有回答的 `followup_failed` 和空输出的 `cli_failed` 都是 `failed`。旧作业通过新服务读取也有此字段。HTTP 202 的初始作业凭据沿用原格式。 |
| 取消或证据不足后还能重测吗？ | **可以**。取当前实施的当前测试，检查 `details.retry_ready === true`。该字段表示候选仍匹配，测试已结束，阶段为 `failed`、`cancelled` 或 `awaiting_evidence`，且停止证据对应最后一次命令。保留当前实施、最新测试、声明和命令的关联。用原命令顺序和 `declaration_source` 提交 `task.test`。服务在执行时再核对工具目录等策略。已有当前审核时也可按上述条件重测。 |
| 中断的关联修复晚一步落库，会不会出现两个计划？ | **相同请求只建一份**。带 `review_anchor` 的 `plan.create` 按完整意见坐标和冻结规划输入去重。确认已经创建时，作业退出码为 0，`result` 含 `{operation:"plan", planning_run_id, reused:true}`。页面直接打开这个 ID；原计划仍可能正在运行或需要显式重试。不同需求、基线、模型请求或声明可建立不同计划。仍被占用而没有可确认的计划时，服务可返回忙，页面按现有流程重读。 |

`error_kind` 只对作业的 `error` 对象分类。CLI 正常返回领域结果并以 130 退出时，`state="finished"`、`exit_code=130`、`error=null`、`error_kind=null`，页面应显示「已取消」。CLI 被中断且没有领域结果、产生 `error.code="cli_cancelled"` 时，`error_kind="cancelled"`。展示时仍应分别读取 `state`、取消信息、`exit_code` 和 `result`。例如返回领域结果且退出码为 1 的审核，可以提示用户查看返工意见；已中断的作业可同时带有 `state_write_failed` 这类具体故障。HTTP 400、409 等同步拒绝沿用原有错误体。已接受作业的 `error.code="cli_refused"` 只描述 CLI 对本次操作的结论。先前步骤可能已经保存了记录。页面显示「被拒绝」，重新读取相关资源后再提供下一步。

### Claude 已接入的三项

| 项目 | 页面与接口 |
| --- | --- |
| F7 首页文案 | 直接模式的阶段依次给出 `test_task`（运行任务测试）、`review_task`（开始任务审核）和 `integrate_stage`（创建阶段集成）。前两项的 `href` 指向任务，第三项指向阶段，创建集成后该行退出。请在首页中英文原因表补齐这三项，继续沿用服务给的 `href` 和未知原因的 `detail`。 |
| F1 单卡规划默认模型 | 新建单卡和意见发起的关联修复共用 `GET /api/workspace` 的 `defaults.task_plan.draft` 与 `defaults.task_plan.plan_review`。每个对象都有 `provider, model, effort`。未指定时实际请求分别是 `claude / claude-opus-5-5 / high` 和 `codex / gpt-6.1-sol / high`。只在用户显式选择时提交覆盖。阶段卡已有独立的 `defaults.card_review`。 |
| F4 静态演示 | `frontend/src/mock/transport.ts` 已支持 `POST /api/reviews/{id}/decisions` 的录制回放，并按轮次、意见坐标和录制的请求匹配 201 响应。操作前后的处理记录见下方索引，页面验收使用相应的 GET 切片。 |

**演示回放索引**　`index.json` 的 `seq` 是页面的 `?at=` 参数，`response_file` 是该步实际读取的文件名。原有响应文件名继续可用。

| 场景 | 适合页面验收的步骤 |
| --- | --- |
| [rework](web-api/samples/rework/index.json) | `?mock=rework&at=55` 时父集成允许返工。56 已有意见、处理记录为空，57 是判断提交的 201 响应。58、61、67 的意见详情依次包含判断、回答、关联规划。71 重读父集成并显示子集成链接，72 的父集成已经不能再返工，78 有已验收子集成的交付摘要。需要看到模型活动时用 seq 49 的审核作业。 |
| [accept](web-api/samples/accept/index.json) | seq 59 有已验收集成的交付摘要。 |
| [dual-partial](web-api/samples/dual-partial/index.json) | seq 49 的 activity 给出成功席位、失败席位和恢复动作；seq 54 重读原作业时两份报告均有效，恢复动作已清空。 |
| [cancel](web-api/samples/cancel/index.json) | seq 41 有运行中的作业活动，47 发出取消请求，48 收到退出码 130，49 重读原作业活动。55 是另一作业在服务重启后的中断活动。 |

### 其他后端处理

- **F2**　硬停的审核作业释放控制进程、且可接管的席位和尝试仍匹配时，首页用已有的 `review_incomplete` 链接到对应作业页。实际补跑入口来自该作业最新的 `activity.recovery`。
- **F8**　计划沿修订链重新领取同一个阶段后，首页移除被接替阶段的旧任务及其审核待办。旧任务、审核仍保留在各自的详情和历史里。
- **追问**　发送给原审核者的消息含冻结差异预览、可取得的引用文件上下文，以及完整材料的位置。短补丁直接附原文。大补丁优先选择意见所指文件和一侧行号对应的改动，预览最多 8192 个字符。缺少冻结源码时，`source_excerpt` 为 `null`。页面继续读取已有的意见活动。
- **F9**　请求值与运行时报告值继续分开。运行时缺少可核验的模型或 effort 回报时，页面继续显示「未确认」。F3 中文导出提示和 F5 已登记命令的提示已由前端处理。

### 验证与验收边界

| 范围 | 11 月 26 日本地结果 |
| --- | --- |
| 后端作业、重测、工作区、追问及关联规划 | 主窗口选取的 114 项回归通过，覆盖同为退出码 2 的业务拒绝和运行故障、历史作业读取、关联规划并发写入与重启复用。另测同一实施先测试失败并完成审核，再按原策略测试通过。任务详情指向第 2 次测试的待审核动作，第 1 次审核仍可查看。 |
| 首页待办 | 8 项通过。真实临时 Git、SQLite 和替代 CLI 覆盖直接阶段三步交接、重新领取同一阶段、双席中断只接管缺失席位。 |
| 演示录制 | 最初在四个临时仓库录制 266 次 HTTP 交换，本次补读后共 269 次，覆盖 6 类新读接口与判断提交。3 项录制测试通过，其中一项逐文件比较了四个场景的独立重录结果。 |
| 前端 | 新录制数据下，`corepack pnpm run check` 的 120 项通过。单独运行静态演示浏览器用例，首页实际出现 1 个「需要我处理」区域，旧用例在第 759 行要求 0 个，故失败。这次后端交接后，前端已更新 mock 和断言，并在联调总结第 10 节报告 127 项 check 与 21 项 e2e 通过。审核后的重测入口也已在任务页接入，见前端联调总结第 10 节后续记录。 |
| 追问与代码检查 | 3 项追问测试通过，含同一大补丁的基线侧、候选侧和后部改动。后端 Ruff 检查、格式和 mypy 均通过。 |
| 真实模型 | 11 月 25 日的 20 步联调是上一版的真实记录。新接口先经替代 CLI 和浏览器验证。11 月 26 日另用同一份冻结材料完成一次真实 Codex 追问，结果见本节上方。 |

## 本轮后端审核记录

| 模块 | 审核观察 | 模块校验 |
| --- | --- | --- |
| P1 首页待办 | 可领取的阶段分别出现；前置阶段交付后后续阶段出现。单卡问题、交叉确认和验收的待办会随处理结果退出。作业占用时仍可看到已记录的集成冲突。 | 8 项通过，涵盖 HTTP、CLI、重启及待办状态变化。 |
| P2 作业进展与恢复 | 双席审核沿用原快照并保留有效报告。确认失败时定位到子轮；旧请求给不出尝试编号时只展示记录入口。 | 3 项作业活动测试通过；主窗口调整旧请求的行为后，单独复核了有、无尝试编号及后续版本变化的场景。 |
| P3 审查意见处理 | 同组两席即使返回相同的意见 ID，判断、回答和修复计划也只挂到所选席位。任务和集成审核在追问后保留原归属。 | 4 项通过，包含返工资格与实际 CLI 对照：多任务归属、确认存疑和原审核目录移动。 |
| P4 仓库首次接入 | 选定提交决定规则和测试命令。可写仓库有有效基线即可直接确认目标；缺少规则或供应方登录时，另行显示对应提示。 | 2 项 HTTP 测试通过，验证了空仓库、不同提交、无效规则、登录状态及直接保存原文。 |
| P5 验收交付摘要 | 按收据中的测试次数导出命令和退出码，并保留测试与审核的完整证据哈希。分支移动并重新验收后，两次交付各自对应当时的结果。 | 1 项完整集成流程通过，验证了旧收据的两条测试、新收据的三条测试，以及旧收据对应的冻结差异和双席审核记录。 |

## 前端接入位置

| 优先级 | 页面 | 在现有代码中接入的位置 |
| --- | --- | --- |
| P1 | 首页 `/`、计划 `/plans/:id` | 在 `pages/HomePage.tsx` 的时间线旁加入跨阶段待办。每行直接使用后端返回的 `href`。计划页 `pages/PlanPage.tsx` 需要读取 `?stage=`，定位或展开对应阶段的领取表单。 |
| P2 | 作业 `/jobs/:id` | 在 `pages/JobPages.tsx` 的现有作业详情显示执行者、席位和恢复操作。`pages/common.tsx` 提供 `RecordLink`。 |
| P3 | 审核 `/reviews/:id`、集成 `/integrations/:id`、单卡规划 `/task-plans/new` | 意见和追问放在 `pages/ReviewPage.tsx`。返工条件放在 `pages/IntegrationPage.tsx`。关联修复由 `pages/NewTaskPlanPage.tsx` 带入来源。 |
| P4 | 提出目标 `/new` | 在 `pages/GoalPages.tsx` 的 `NewGoalPage` 连接接入检查与本地草稿，沿用已有的分支选择和环境检查入口。 |
| P5 | 集成 `/integrations/:id` | 在 `pages/IntegrationPage.tsx` 已有的验收收据旁增加摘要预览与复制。 |

在 `api/types.ts` 定义响应，在 `api/client.ts` 接入请求。`api/transport.ts` 已经添加 `/api` 前缀，例如读首页待办时向 transport 传 `/attention`。作业使用 `api/jobs.ts`，读取和刷新使用 `api/reads.ts`。页面文案沿用现有 `tx` 与英文词条。联调使用[本地服务启动说明](../README.md#run-against-your-own-repository)。

## 1　首页待办

在现有时间线附近显示“需要我处理”。每行放一个可读的任务名、等待原因和直达处理页面的链接。同一计划可同时有多个 `choose_stage` 行，`id` 是共享的计划 ID，渲染键应包含 `href`。例如先领取阶段 A 后，仍可领取的阶段 B 应保留。完整读取且为空时显示“已全部处理”，部分读取时显示“待刷新”。完成现有页面操作后刷新列表。

## 2　作业进展和恢复

从作业栏进入详情后，按执行者列出最近观察到的事件、时间和状态。重复轮询不得重复显示事件。断线重连后恢复已有活动。服务重启后先展示已保存的结果，再展示可以继续的动作。用户点击后走现有作业提交、冲突处理和取消流程。

## 3　审查意见

在审核页按现有分组显示发现者、相同证据、交叉确认和分歧，沿用冻结代码行链接。用户能在一条意见下查看追问与回答、记录判断、跳到关联修复。集成页执行返工之前显示归属和不可执行的具体原因。分别呈现用户判断和原审核结论。

## 4　仓库首次接入

在提出目标页提供“检查接入”面板。复用已有分支选择和供应方环境检查，展示当前选中基线、登录观测、规则文件和已登记命令。展示模板时提供复制按钮，并告知用户在自己的仓库审阅并提交，再重新检查对应基线。

## 5　验收交付

在已接受的集成页面增加“复制交付摘要”。预览 Markdown，并显示完整提交、目标与基线及测试和审核链接。复制操作应给出成功或失败反馈。

## 新目标草稿

在提出目标页按仓库标识保存未提交的原始目标。离开页面、刷新和切换界面语言时恢复。服务成功接收并返回目标或咨询记录后清除已提交的那一份；作业失败、取消和内容已在另一标签页更新时保留草稿。使用浏览器本地存储即可。仓库首次访问没有草稿时显示现有空表单。

## HTTP 接口

以下路径均相对于当前工作台。读取沿用同源 GET、错误结构 `{error:{code,message}}` 与现有客户端。提交模型工作统一经过 `POST /api/jobs`，返回 HTTP 202 和 `job_id` 后轮询作业。启动成功与作业完成是两个不同状态。以下 JSON 中的简称 ID 仅用于示例；实现时直接使用服务返回的 ID。

### 1. 首页

`GET /api/attention` 返回 `{items,complete}`。每一项包含 `kind,id,label,reason,detail,href`。使用返回的 `href` 导航，审核待办随可用操作指向任务、审核或作业详情。列表来自当前仓库的状态，包含从 CLI 创建的相关记录。例如：

```json
{"items":[{"kind":"integration","id":"i1","label":"汇总报表","reason":"accept_integration","detail":"Tests and review qualify this candidate for acceptance.","href":"/integrations/i1"}],"complete":true}
```

用 `reason` 映射中英文动作文案。目标和计划相关值有 `confirm_goal, consultation_failed, plan_goal, retry_project_plan, choose_stage`。执行相关值有 `dispatch_failed, planning_attention, planning_question, implementation_attention, task_test_failed, test_task, review_task, integrate_stage`。测试、审核和集成相关值有 `integration_conflict, rebuild_integration, inspect_candidate, test_integration, integration_test_failed, inspect_tests, review_integration, review_incomplete, review_decision, accept_integration`。未知值可显示 `detail`。

`complete:false` 表示这次只返回了部分待办。页面保留上次已知的相关行、标注待刷新，并在可见时重读。归档没有需要处理的待办。

`consultation_failed` 会进入 `/new?consultation=<id>`。提出目标页应读取已有的 consultation 资源，带入 `original_goal`、`baseline_commit` 和 `project_id`，让用户重新咨询或直接确认原文。该入口有明确的来源，浏览器中其他未提交的草稿应保留。

委派子卡的入口形如 `/stages/<id>?card=<card_id>`，在 `pages/StagePage.tsx` 定位到对应卡片。独立单卡的阻塞问题直接复用 `/task-plans/new?from=<plan_id>&question=<index>`。早期记录缺少直接确认和回答问题的父记录 ID，后端按仓库、基线和内容匹配。完全相同上下文的独立操作可能合并显示，待办列表不适合作为操作历史。

### 2. 作业详情

`GET /api/jobs/{job_id}/activity` 返回一次轮询快照。使用现有 job 类型和作业轮询，避免另起一个独立轮询器。

| 字段 | 用法 |
| --- | --- |
| `state, created_at, started_at, finished_at` | 作业本身的状态和时间。`observed_at` 是服务读取时间。 |
| `events[]` | 每项包含 `sequence, at, event, state, resource:{kind,id}, seat, provider, attempt`。同一作业内用 `sequence` 去重；事件中没有原始模型消息。 |
| `actors[]` | 按对象、席位和尝试次数聚合。用 `last_event, observed_at` 显示最近观测。`provider_wait` 表示等待本地 provider 配额。作业结束而最后一次观测仍在运行时，`state` 为 `unconfirmed`，`last_observed_state` 保留上次状态。 |
| `reviews[]` | 包含 `round_id, snapshot_id, seats[], retry`。席位含 `seat, provider, attempt, state, retryable`，用于区分已完成与缺失的报告。 |
| `resources[], recovery[]` | 对象由 `kind,id` 标识，复用现有详情路由。每个恢复动作包含 `resource, action, args`。只在用户点击后，将返回的 `{action,args}` 原样提交至作业接口。 |
| `history_truncated, controller_holds_journal` | 本次最多返回最近 50 条事件。旧作业可能没有事件，控制器观测可能为 null。后端按当前归属和席位记录给出可用恢复动作。 |

先按整个作业状态解释执行者最后一次观测。若作业中断，应同时显示“中断前最后观测于……”和相关记录入口。对没有恢复动作的记录，引导用户查看相应阶段的现有操作。服务不提供可核验的剩余时间。

例如双审中仅第二席失败时，用户点击恢复后发送的作业形如下面的请求。`expected_attempts` 由后端生成，用于避免两个旧页面重复启动同一席位。交叉确认的恢复动作会给出确认子轮 ID。旧作业仅留下轮次和席位名、缺少日志、收据和尝试编号时，界面仍可打开详情，`recovery` 为空。重试作业可能先收到 HTTP 202，随后在执行时发现尝试已更新。此时显示作业错误并刷新活动。

```json
{"action":"review.retry","args":{"round_id":"r1","seat_keys":["reviewer-2"],"expected_attempts":{"reviewer-2":1}}}
```

### 3. 审查详情与集成返工

`GET /api/reviews/{round_id}/findings` 读取原始审核轮。响应包含 `round_id, snapshot_id, status, reasons, source, groups`。组保留 `location, blocking, independent_reports, confirmations, members`。每个成员包含 `seat, finding, anchor, activity`。直接保存返回的 `anchor`，其字段为 `round_id, snapshot_id, seat, attempt, finding_id`。区分同一代码位置的不同发现者。交叉确认轮请返回其原始审核轮查看意见。

| 用户操作 | 请求与结果 |
| --- | --- |
| 留下判断 | `POST /api/reviews/{round_id}/decisions`，JSON 为 `{"anchor": …,"decision":"disputed","reason":"该分支在上游已排除"}`。支持 `confirmed, disputed, deferred`。HTTP 201 返回活动条目。 |
| 追问 | 提交作业 `review.followup`，参数为 `{"review_anchor": …,"question":"什么输入能触发这个问题？"}`。使用原席位的模型配置和冻结资料开启新的只读轮次。完成后重读 findings。连续追问时给出独立、完整的问题。 |
| 发起关联修复 | 打开现有单卡规划表单，预填 finding 和拟修内容。提交 `plan.create` 时在原有 `baseline, requirements` 等参数外附上 `review_anchor`。用 `source.correction_baseline` 选择评审过的 commit；粘贴 diff 时该值为 null，由用户明确选本仓库基线。 |

`activity[]` 按写入顺序返回。`decision` 条目含判断及原因，`answer` 含问题、回答和运行证据，`linked_plan` 含 `planning_run_id`，链接到 `/task-plans/{planning_run_id}`。审核结论通过现有测试和复审更新。

`GET /api/integrations/{id}/rework-eligibility` 在集成返工按钮旁展示当前资格。普通响应含 `integration_id, round_id, eligible, reason, owner, findings, lock_busy`。每个阻塞项含 `seat, finding_id, path, reference_status, owners`；归属使用 `task_id,task_run_id`。仅在 `eligible:true` 时启用 `integration.rework`，用该次响应的 `integration_id` 和 `round_id` 提交。资格读取后现场可能发生变化，以作业回执显示执行结果。若 `lock_busy:true`，等正在执行的作业结束后再读取，其余详情可能省略。

### 4. 首次接入

`GET /api/workspace/onboarding` 默认检查 HEAD。选择其他基线后使用 `?baseline=<URL 编码的分支或提交>`。响应含 `repository, read_only, branch, head, branches, baseline, providers, rules, rules_template, next_steps`。

- `rules` 的 `path, status, detail, commands[]` 对应文件位置、状态、具体错误与已登记命令。状态为 `no_commit, missing, invalid, no_commands, ready`，命令结构为 `{name,argv}`。模板是可复制的 Python unittest 示例，用户需选用适合自己仓库的命令。
- `providers.codex` 和 `providers.claude` 提供 `status, cli_version, observed_at, check_job_id, error`。`unknown` 表示还未确认登录；其余状态为 `missing_cli, checking, check_failed, login_required, ready`。
- 按 `next_steps[].kind` 展示提示。仓库动作有 `commit_baseline, add_rules, fix_rules, register_commands`。供应方动作有 `install_cli, check_provider, sign_in`，同时提供 `provider`。`create_goal` 指向现有表单，可与补规则、登录等提示同时出现。只要可写仓库有有效基线，用户就能直接确认原文。检查按钮提交已有 `environment.refresh` 作业，再读取本接口。打开面板只读，选用哪个供应方由用户决定。

### 5. 交付导出

`GET /api/integrations/{id}/delivery` 在有验收收据时返回 `{receipt,tasks,tests,links,markdown}`。复制 `markdown` 原文。页面内使用相对路径 `links.integration, links.diff, links.review`。Markdown 中的链接指向导出时的本机工作台实例。供其他机器核对的内容包括完整候选提交 SHA、验收基线、`test_evidence_hash` 和 `review_checkpoint_hash`。目标分支移动后，仍显示当时接受的版本。尚未验收返回 HTTP 409。读取或复制摘要均不会触发 Git 操作。

## 页面验收样例

1. 同一仓库有失败阶段和待接受集成时，首页同时展示二者。接受后该候选从待办中消失。
2. 双席审核只收到第一份有效报告后，关闭页面再打开。同一报告仍在，恢复动作只包含缺失席位。
3. 在意见下记录不同判断并追问。刷新后能看到双方记录，原冻结代码行与结论仍可追溯。
4. 选择没有规则的旧提交，能复制模板；切到已配置的提交，能看到该提交登记的测试命令。
5. 复制已接受候选的摘要，粘贴到文本框后能看到完整 SHA、测试结果和审核依据。
