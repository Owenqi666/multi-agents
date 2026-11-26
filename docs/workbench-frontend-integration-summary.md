# 工作台前端联调总结（供后端核对）

这份文档写给后端开发代理。它说明 P1–P5 页面怎样使用后端接口、页面成立所依赖的假设、验证是怎么做的，以及需要后端逐条确认的问题。说到前端行为时给出 `frontend/` 下的文件和行号，说到后端依据时给出 `src/` 或 `tests/` 下的位置。

第 2–4 节的行号对应后端核对之前的工作区（2026-11-25 21:50 前后）。后端核对之后又改过一批前端文件，清单和改动见第 9 节；这些文件里的行号有偏移，请以函数名和组件名为准。后端的核对答复在 [`docs/workbench-backend-integration-review.md`](workbench-backend-integration-review.md)。

## 1　范围与边界

- 日期：2026-11-25。主仓库在 `main`，基线提交 `83de58424202ae69f1b68649b6a8fda95ff762a0`。
- 后端没有被前端这一侧改动。`git diff --stat -- src tests` 从开始到结束都是 32 个文件、485 增 35 删，这些是仓库主人自己的未提交改动。未跟踪的后端新文件（例如 `src/multi_agent/job_activity.py`）不计入这个统计，也没有被改过。
- 所有前端改动都没有提交，留在工作区。
- P1–P5 指：P1 首页待办，P2 作业活动和中断后的恢复，P3 审核意见的判断、追问和关联修复，P4 首次接入检查和目标草稿，P5 验收后的交付摘要。

前端新增的文件：

- `frontend/src/pages/Attention.tsx`、`JobActivity.tsx`、`ReviewFindings.tsx`、`IntegrationExtras.tsx`、`Onboarding.tsx`、`goalDraft.ts`
- `frontend/src/app/PageBoundary.tsx`
- `frontend/src/test/workbench-panels.test.tsx`、`frontend/src/test/page-boundary.test.tsx`
- `frontend/e2e/workbench.spec.ts`、`frontend/e2e/workbench_env.py`

前端修改的文件：

- `frontend/src/api/client.ts`、`jobs.ts`、`reads.ts`、`types.ts`
- `frontend/src/app/App.tsx`、`app.css`、`labels.ts`
- `frontend/src/components/Action.tsx`、`DiffView.tsx`
- `frontend/src/i18n/en.ts`
- `frontend/src/pages/GoalPages.tsx`、`HomePage.tsx`、`IntegrationPage.tsx`、`JobPages.tsx`、`NewTaskPlanPage.tsx`、`PlanPage.tsx`、`ReviewPage.tsx`、`StagePage.tsx`、`TaskAttemptEvidence.tsx`、`common.tsx`
- `frontend/src/styles/tokens.css`
- `frontend/src/test/task-evidence.test.tsx`
- `frontend/e2e/support.ts`、`frontend/playwright.config.ts`、`frontend/README.md`

文档：新增 `docs/workbench-backend-followups.md` 和本文件。`docs/workbench-frontend-handoff.md` 和 `docs/workbench-improvement-plan.md` 是联调的输入，不是这一侧写的。

## 2　前端对后端接口的使用清单

### 2.0　所有读取和提交共用的机制

- **读取**走 `useRead`（`frontend/src/api/reads.ts:70-124`）。每个读取声明自己是否「占锁」：
  - 占锁读取在有作业排队、运行或正在提交时不发送，页面保留上一次读到的内容并标为过期（`reads.ts:74`、`:97`）。
  - 占锁读取一次只发一个，排队时如果作业开始了就不再发（`reads.ts:66-67`、`:103`）。
  - 答复带 `lock_busy: true` 或 `unavailable_code: "lock_busy"` 时，按「没读到」处理，保留旧内容（`reads.ts:48-51`、`:105`）。
  - 每当一个作业结束，所有读取重读一次（依赖项里的 `generation`，`reads.ts:124`）。
  - 声明 `pollWhileJob` 的非占锁读取，在有作业期间每 5 秒重读（`reads.ts:62`、`:119`）。
- **哪些资源读取算占锁**：`GET /api/resources/{kind}/{id}` 中 kind 为 `task`、`task-test`、`task-review`、`integration`、`stage`、`plan` 的（`frontend/src/api/client.ts:21`）。
- **提交**走 `ActionButton`（`frontend/src/components/Action.tsx:14-62`），发 `POST /api/jobs`，正文 `{action, args}`（`client.ts:29-30`）。有其他作业在跑、正在提交、或页面还有读取在途时按钮不可用（`Action.tsx:41`）。一次点击只发一次（`Action.tsx:35-53`）。提交后等作业结束（`jobs.waitFor`）再调用各自的 `onDone`。
- **作业轮询**：`GET /api/jobs/{id}` 每 1 秒一次（`frontend/src/api/jobs.ts:32`、`:201-204`）。作业状态的先后顺序按 `queued < running < cancelling < finished = interrupted` 处理（`jobs.ts:33`）。
- **错误**：非 2xx 答复按 `{error: {code, message, details}}` 解析成 `ApiError(status, code, message, details)`（`frontend/src/api/transport.ts:5-17`、`:48-49`）。

### 2.1　`GET /api/attention`（P1）

- 前端位置：`client.ts:40`；`frontend/src/pages/Attention.tsx:73-107`；首页挂载在 `frontend/src/pages/HomePage.tsx:33`、`:75`、`:91`。
- 调用时机：首页打开时一次；每个作业结束后一次；有作业期间每 5 秒一次（`Attention.tsx:85`）；用户点「刷新待办」（`Attention.tsx:117`）。
- 作业期间是否回避：不回避。没有作业时它和其他占锁读取排队发送（`Attention.tsx:76`）；有作业时直接发送（`Attention.tsx:77`）。
- 参数：无。
- 读取的字段和解释：
  - `items[].reason`：查页面自己的文案表，共 20 个值（`Attention.tsx:23-44`）。不认识的值显示 `detail`（`Attention.tsx:131`）。
  - `items[].kind`：查类别名称表，不认识的原样显示（`Attention.tsx:132`）。
  - `items[].label`：作为链接文字（`Attention.tsx:135`）。
  - `items[].href`：原样作为站内链接的地址，页面不拼接也不改写（`Attention.tsx:135`）。
  - `items[].detail`：只在 6 种原因下显示，分别是 `choose_stage`、`planning_question`、`dispatch_failed`、`planning_attention`、`implementation_attention`、`task_test_failed`（`Attention.tsx:49`、`:136`）。其余原因的 `detail` 不显示。
  - `complete`：只有明确的 `false` 才算部分答复（`Attention.tsx:79`）。
  - 行的身份是 `kind/id/reason/href` 四项的组合（`Attention.tsx:52`）。
- 降级处理：
  - `complete: false`：显示这次返回的行；上一次完整读取里有、这次没有的行继续显示并标「待刷新」（`Attention.tsx:92-98`、`:119-124`）。
  - HTTP 404：整个面板不出现（`Attention.tsx:89`、`:111`）。
  - 其他错误：显示错误文字，已有的行保留（`Attention.tsx:125`）。
  - 缺少 `items` 键：按空列表处理（`Attention.tsx:79`）。
- 链接落点：`/plans/{id}?stage=` 由计划页定位到那个阶段，`/stages/{id}?card=` 定位到子卡，`/new?consultation=` 把失败的咨询带进表单（`frontend/src/app/App.tsx:33`、`:38`、`:39`）。
- 后端依据：`src/multi_agent/web/server.py:227-229`，`src/multi_agent/web/attention.py:468-491`，`tests/unit/test_web_attention.py`。

### 2.2　`GET /api/jobs/{id}/activity`（P2）

- 前端位置：`client.ts:41`；`frontend/src/pages/JobActivity.tsx:33-80`；作业详情页 `frontend/src/pages/JobPages.tsx:59`、`:106`。
- 调用时机：作业详情页打开时一次；这个作业每次轮询返回时一次（`JobActivity.tsx:76`）；任何作业结束后再一次（`JobActivity.tsx:74`）。同一时间只有一个请求在途，期间的触发合并成一次补读（`JobActivity.tsx:44`、`:69`）。
- 作业期间是否回避：不回避。
- 参数：路径里的作业号。
- 读取的字段和解释：
  - `events[]`：按 `sequence` 去重后合并，页面最多留 500 条、显示最近 100 条（`JobActivity.tsx:18-19`、`:58-61`），按 `sequence` 升序显示。用到 `at`、`seat`、`provider`、`event`、`state`。事件名查文案表，不认识的原样显示。服务目前写入 12 种事件：`attempt_started`、`attempt_settled`、`provider_wait`、`provider_admitted`、`test_command_started`、`suite_settled`、`session_started`、`message`、`usage`、`completed`、`error`、`process_exited`。文案表里多出的 `cancel_requested` 服务目前不写。
  - `history_truncated`：表示这一次读取被窗口截断（服务一次最多扫描日志尾部 4 MiB、返回最近 50 个事件），不表示服务丢了记录。页面的提示是「一次读取最多返回最近 50 条」。
  - `actors[]`：用到 `seat`、`provider`、`attempt`、`state`、`last_observed_state`、`observed_at`、`last_event`、`resource`。actor 的来源状态有 8 种：`pending`、`ready`、`running`、`cancelling`、`awaiting_evidence`、`succeeded`、`failed`、`cancelled`；读接口另外会派生出 `unconfirmed`。`state` 为 `unconfirmed` 时，用 `last_observed_state` 说明「中断前最后观测到的状态」（`JobActivity.tsx:131-132`）。
  - `reviews[]`：用到 `round_id`、`snapshot_id`、`seats[].seat`、`provider`、`attempt`、`state`、`retryable`（`JobActivity.tsx:189`）。审核席位的状态有 5 种：`pending`、`running`、`cancelling`、`succeeded`、`failed`。
  - `recovery[]`：每项渲染成一个按钮，见 2.9。
  - `controller_holds_journal`：作业已中断而它为真时，说明控制进程还在；为 `null` 时说明这个作业没有活动记录（`JobActivity.tsx:213`、`:243`）。
  - 三个状态字段共用一张文案表（`JobActivity.tsx:84-87`），它是上面几组取值的超集，另外含作业自己的 `queued`、`finished`、`interrupted`。文案表不是封闭的协议，不认识的值原样显示。
- 降级处理：HTTP 404 时整块不出现；其他错误显示错误文字（`JobActivity.tsx:153-154`）。缺少的数组键按空数组处理（`JobActivity.tsx:54-55`）。
- 后端依据：`server.py:247-249`，`src/multi_agent/web/activity.py:260-425`，`src/multi_agent/job_activity.py`，`tests/unit/test_web_job_activity.py`。

### 2.3　`GET /api/reviews/{id}/findings`（P3）

- 前端位置：`client.ts:42`；`frontend/src/pages/ReviewFindings.tsx:31-34`（`useFindings`）、`:216-250`（`FindingsPanel`）；审核页 `frontend/src/pages/ReviewPage.tsx:104`、`:122`；关联修复表单 `frontend/src/pages/NewTaskPlanPage.tsx:201`。
- 调用时机：审核页打开时一次，并且只对原始审核轮读取，交叉确认轮不读（`ReviewPage.tsx:104`）；每个作业结束后一次；记录判断之后一次（`ReviewFindings.tsx:250` 的 `onChanged`）。不轮询。
- 作业期间是否回避：不回避，按非占锁读取处理。
- 参数：路径里的审核轮号。
- 读取的字段和解释：
  - `round_id`：必须等于当前页的审核轮，否则这份答复不用（`ReviewFindings.tsx:222`）。
  - `status`、`reasons`：显示为审核结论。
  - `source.correction_baseline`：关联修复的基线，见 2.8。
  - `groups[].location`、`blocking`、`independent_reports`、`confirmations`：分组标题、阻塞标记、各席是否独立报告、交叉确认。
  - `groups[].members[].seat`、`finding`、`anchor`、`activity[]`：每条意见一张卡。`activity[].kind` 认 `decision`、`answer`、`linked_plan`，其他值只显示 kind 和时间（`ReviewFindings.tsx:49-83`）。
- 降级处理：HTTP 404 或还没读到时，退回审核轮记录自带的 `assessment.groups`，只读显示，不出现三个操作（`ReviewFindings.tsx:224-225`、`:250`）。只读归档下也不出现操作（`ReviewFindings.tsx:221`）。
- 后端依据：`server.py:262-264`，`src/multi_agent/web/review_findings.py:25-65`，`tests/unit/test_web_review_findings.py`。

### 2.4　`GET /api/integrations/{id}/rework-eligibility`（P3）

- 前端位置：`client.ts:47`；`frontend/src/pages/IntegrationExtras.tsx:24-83`；集成页 `frontend/src/pages/IntegrationPage.tsx:101`。
- 调用时机：集成页上，不是归档、还没验收、至少有一轮审核时读取（`IntegrationPage.tsx:101`）；每个作业结束后一次。不轮询。
- 作业期间是否回避：回避。它声明为占锁读取（`IntegrationExtras.tsx:26`），有作业时不发送。
- 参数：路径里的集成号。
- 读取的字段和解释：
  - `integration_id`：必须等于当前集成（`IntegrationExtras.tsx:29`）。
  - `eligible`：决定返工按钮是否可用。
  - `reason`：不可返工时原样显示。
  - `owner`、`findings[].owners[]`：显示返工交给哪个成员、每条阻塞意见归谁。
  - `findings[].reference_status`：为 `unverified` 时提示引用没有对上冻结文件。
  - `round_id`：有值且集成还没有返工记录时才出现返工按钮（`IntegrationExtras.tsx:69`）。
- 降级处理：`lock_busy: true` 由 `useRead` 当作没读到；有作业时显示「等作业结束后读取」（`IntegrationExtras.tsx:31`）；错误码 `not_found` 时整块不出现（`IntegrationExtras.tsx:33`）；没有阻塞意见且不可返工时整块不出现（`IntegrationExtras.tsx:39`）。
- 后端依据：`server.py:265-274`，`review_findings.py:111` 起，`tests/unit/test_web_review_findings.py`、`tests/unit/test_web_read_gate.py`。

### 2.5　`GET /api/workspace/onboarding`（P4）

- 前端位置：`client.ts:49`；`frontend/src/pages/Onboarding.tsx:41-86`；提出目标页 `frontend/src/pages/GoalPages.tsx:74`、`:177`。
- 调用时机：提出目标页打开时一次；用户改基线输入框后停 400 毫秒再读（`Onboarding.tsx:19`、`:58-62`）；每个作业结束后一次，环境检查结束也算。不轮询。
- 作业期间是否回避：不回避。
- 参数：`baseline`。输入框的值等于当前 HEAD 或为空时不带这个参数（`Onboarding.tsx:63`）；否则原样带上，可以是分支名或提交。
- 读取的字段和解释：
  - `baseline`、`head`：显示检查的是哪个提交、是不是当前 HEAD（`Onboarding.tsx:185`）。
  - `rules.status`：认 `ready`、`no_commands`、`missing`、`invalid`、`no_commit` 五个值（`Onboarding.tsx:88-94`）。`rules.detail` 作为错误说明，`rules.commands[]` 列出命令名和参数。
  - `rules_template`：没有规则文件时给用户复制。
  - `providers.{codex,claude}.status`：服务会返回 6 个值，`missing_cli`、`checking`、`check_failed`、`login_required`、`ready`、`unknown`，页面都有文案（`Onboarding.tsx:95-102`）。另外，这个提供方有环境检查作业没结束时，页面不等服务的答复就先显示 `checking`（`Onboarding.tsx:129`）。还用到 `cli_version`、`observed_at`、`check_job_id`、`error`。
  - `next_steps[].kind`：认 `commit_baseline`、`add_rules`、`fix_rules`、`register_commands`、`install_cli`、`check_provider`、`sign_in`、`create_goal` 八个值（`Onboarding.tsx:106-121`）。
- 降级处理：HTTP 404 或答复里没有 `rules` 时整块不出现（`Onboarding.tsx:42`、`:78`、`:83`）。错误只显示给它对应的那个基线，换基线后不沿用（`Onboarding.tsx:64-77`）。缺少的键按空值处理（`Onboarding.tsx:41-52`）。
- 后端依据：`server.py:233-243`，`src/multi_agent/web/onboarding.py:31-129`，`tests/unit/test_web_onboarding.py`。

### 2.6　`GET /api/integrations/{id}/delivery`（P5）

- 前端位置：`client.ts:51`；`IntegrationExtras.tsx:113-165`；集成页 `IntegrationPage.tsx:118`。
- 调用时机：集成记录里已经有验收记录时才读（`IntegrationPage.tsx:118`）；每个作业结束后一次。不轮询。
- 作业期间是否回避：不回避。
- 参数：路径里的集成号。
- 读取的字段和解释：
  - `receipt.integration_id`：必须等于当前集成（`IntegrationExtras.tsx:117`）。
  - `receipt` 的其余字段：候选提交和引用、目标分支、验收基线、收据号和时间、测试证据哈希、审核检查点哈希，全部完整显示，不截短。
  - `tasks[]`、`tests[]`：交付内容和每条命令的退出码。
  - `links`：三个站内链接。
  - `markdown`：原样放进只读文本框，「复制交付摘要」复制的就是这个字符串。
- 降级处理：HTTP 409 或错误码 `job_unavailable` 时显示「还没有验收收据」；错误码 `not_found` 时整块不出现；错误码 `archive_unsupported` 时说明这份归档早于验收收据功能；其他错误显示错误文字（`DeliverySummary` 组件开头）。
- 后端依据：`server.py:275-277`，`src/multi_agent/web/delivery.py:20-76`，`tests/unit/test_integration_verification_cli.py`（按验收收据导出的 HTTP 测试）。

### 2.7　`POST /api/reviews/{id}/decisions`（P3）

- 前端位置：`client.ts:44-45`；`ReviewFindings.tsx:88-108`。
- 调用时机：用户在一条意见下选了判断、写了理由，点「记录判断」。理由为空时按钮不可用。
- 发出的正文：`{anchor, decision, reason}`。
  - `anchor`：就是 2.3 读到的 `members[].anchor`，原样回传，路径里的审核轮号取 `anchor.round_id`（`ReviewFindings.tsx:98`）。
  - `decision`：`confirmed`、`disputed`、`deferred` 之一。
  - `reason`：去掉首尾空白的输入。
- 答复：页面不读答复正文，成功后清空理由并重读 2.3。
- 降级处理：失败时在表单下显示原因，并重读 2.3。错误码是 `invalid_request` 时把服务给的 message 一起显示（`ReviewFindings.tsx:103`）。
- 后端依据：`server.py:345-346`，`review_findings.py:68-81`。

### 2.8　作业动作 `review.followup` 和带 `review_anchor` 的 `plan.create`（P3）

- `review.followup`：`ReviewFindings.tsx:142-146`。参数 `{review_anchor, question}`，`review_anchor` 是 2.3 的 anchor 原样。作业退出码非 0 时在表单下显示作业的错误信息或退出码并给出作业链接；退出码为 0 时清空已发送的那个问题。回答靠作业结束后重读 2.3 出现。后端依据：`src/multi_agent/web/actions.py:581-584`、`:366-374`，`tests/unit/test_web_finding_followup.py`。
- 关联修复：意见卡上的链接地址是 `/task-plans/new?review=&seat=&attempt=&finding=`（`ReviewFindings.tsx:27-29`）。表单页重读 2.3，用 `seat`、`attempt`、`finding_id` 三项找到那条意见（`NewTaskPlanPage.tsx:200-205`）；找不到时提示「这个席位可能补跑过」并给回审核轮的链接（`NewTaskPlanPage.tsx:209-210`）。
- `plan.create` 的参数（`NewTaskPlanPage.tsx:162-172`）：
  - `baseline`：`source.correction_baseline` 有值时用它并且不可改（`NewTaskPlanPage.tsx:55`、`:108`）；为 `null`（粘贴补丁的审核）时由用户填写。
  - `requirements`：按意见的标题、位置、影响、依据预填，用户可改。
  - `review_anchor`：那条意见的 anchor 原样（`NewTaskPlanPage.tsx:165`）。
  - `draft`、`plan_review`：只在用户勾选「指定」时带上，否则不带。
  - `declarations`：用户填了依赖或决定时带上。
- 后端依据：`actions.py:544-548`、`:347-348`。

### 2.9　恢复动作（P2）

- 前端位置：`JobActivity.tsx:139-145`、`:200-206`。
- 页面对 2.2 的 `recovery[]` 每一项渲染一个按钮，提交时 `action` 和 `args` 都原样取自那一项，页面不增删也不改任何参数（`JobActivity.tsx:205`）。
- 按钮文字只看 `args.seat_keys` 和 `args.recover`：两者都有时是「接管并补跑中断的席位」，只有 `seat_keys` 时是「只补跑缺失的席位」，否则用动作的通用名称。
- 任何作业结束后，在 2.2 重读完成之前，旧按钮不可用（`JobActivity.tsx:206`）。这是为了不把上一次读到的 `expected_attempts` 再发一遍。
- 后端依据：`activity.py:218-250`，`actions.py:191-208`、`:566-570`、`:607-611`。

### 2.10　`integration.rework`（P3）

- 前端位置：`IntegrationExtras.tsx:69-74`。
- 参数 `{integration_id, round_id}`，两个值都取自 2.4 的同一份答复。`eligible` 为假或答复已过期时按钮不可用。
- 作业结束后找新集成的顺序：`result.rework.child_id`，然后 `result.revision.integration_id`，最后是作业 `resources` 里另一个 `integration`（`IntegrationExtras.tsx:41-44`）。找到就跳转；找不到就显示 `result.rework.reason` 或作业错误信息（`IntegrationExtras.tsx:45`、`:76-80`）。
- 后端依据：`actions.py:619` 起。

### 2.11　`environment.refresh`（P4）

- 前端位置：`Onboarding.tsx:140-141`，以及表单上方的提示条 `GoalPages.tsx:39`。
- 参数 `{provider}`，取值是 2.5 的 `providers` 的键，即 `codex` 或 `claude`。
- 这个动作没结束时，作业中心按提供方记着它（`jobs.ts:16-17`、`:179-182`），两处按钮都不可用，状态显示「检查中」。

### 2.12　目标表单的 `project.consult` 和 `project.confirm`（P4）

- 两个动作本身是原有的，这次改的是草稿和提交后的处理。
- 草稿按仓库路径存在浏览器本机（`frontend/src/pages/goalDraft.ts:13-48`），不发给服务。
- 提交后记下 `{job_id, goal}`（`GoalPages.tsx:191`、`:201`）。页面刷新后用 `GET /api/jobs/{job_id}` 找回那个作业（`GoalPages.tsx:93-100`），按结果决定是否清草稿。
- 成功的判断是：退出码为 0，并且作业 `resources` 里有 `consultation` 或 `project`（`GoalPages.tsx:86`）。
- 从待办进入 `/new?consultation={id}` 时，读那条咨询记录，把它的原始目标带进表单（`GoalPages.tsx:215-218`）。

### 2.13　只改了定位的页面

- 计划页接受 `?stage=`，阶段页接受 `?card=`，只用来滚动并标出那一项，不改变请求（`App.tsx:38-39`）。
- 任务页每次实施新增一行「实施者报告」，读的是任务记录里 `attempts[].runtime.result` 的 `outcome`、`required_paths`、`summary`（`frontend/src/pages/TaskAttemptEvidence.tsx` 的 `Report` 组件）。`outcome` 认 `completed` 和 `scope_blocked`，其他值原样显示。后端依据：`src/multi_agent/cli/task_prompt.py:28-32`。

## 3　前端依赖的后端行为假设

每条后面是依据的来源。标「未见文档」的，是只从实现或测试里看出来的。下面各条已经按后端的核对答复改过：交接文档里其实有约定的，注明了出处；说得不准确的，改成了准确的说法。

1. 待办行的 `href` 是站内相对路径，可以直接导航；`kind/id/reason/href` 的组合在当前支持的数据路径上可以当作行的键。同一个计划的两个可领取阶段共享计划 ID，靠 `href` 的查询参数区分。接口没有另发持久的行 ID，也不承诺将来仍用这套组合，跨次读取以最新列表为准。出处：[`docs/workbench-frontend-handoff.md`](workbench-frontend-handoff.md) 里约定了完整 `href`；`attention.py:38-45`、`:145`。
2. `complete: false` 不只在作业占锁时出现：CLI 作业登记之后到结束之前、服务关闭读入口时都会。这时 `items` 是能从当时的记录推导出来的子集。归档返回的是完整的空列表。几次读取和随后的点击不构成一个原子快照，所以保留的旧行只表示「待刷新」，不能据此说操作仍然可做；部分答复为空也不能显示成全部处理完。依据：`src/multi_agent/web/gate.py`，`attention.py:468-491`。
3. 待办的 `reason` 目前是页面有文案的那 20 个值（列在第 7 节第 1 问），后端已逐个确认相符。不认识的原因显示 `detail`。依据：`attention.py`，后端核对答复 Q1。
4. 活动事件的 `sequence` 是完整 JSONL 行结束处的字节偏移。在同一个作业正常追加的日志里它唯一、只增不减，服务重启后读同一个文件序号不变，相邻的序号可以差很多。页面按作业号加 `sequence` 合并。出处：交接文档约定了同一作业内用 `sequence` 去重；`activity.py:70`，`src/multi_agent/job_activity.py`。
5. `recovery[]` 给出的 `action` 和 `args` 原样提交。`expected_attempts` 已经过期但结构合法时，`POST /api/jobs` 通常先返回 202，作业的终态是 `finished`、`exit_code: 2`、`error.code: "cli_refused"`；参数格式不对时直接是 400 `invalid_request`；有并发作业时提交会收到 409 `busy`。依据：`activity.py:218-250`，`actions.py:191-208`，`src/multi_agent/cli/runner.py`，`tests/unit/test_web_job_activity.py`。
6. 恢复动作可以在没有作业结束的情况下变化：新作业开始、旧控制进程释放日志或咨询锁、另一个 CLI 改了当前席位或测试，都会改变它。这些变化可能发生在页面最后一次收到作业终态之后。所以页面除了在作业结束时重读，还在作业已中断且控制进程仍在时低频重读、在页面回到前台时重读，并提供手动刷新；提交之后按第 5 条处理过期的结果。依据：`activity.py:324` 起。
7. 意见的完整身份是 `round_id`、`snapshot_id`、`seat`、`attempt`、`finding_id` 五个字段，同一轮内可以用后三项找回成员，两席可以有相同的 `finding_id`。只有所选席位的那次尝试被替换时旧 anchor 才失效，其他席位补跑不会让它过期。过期时的报错不统一：直接提交判断通常是 400 `invalid_request`；形状合法的追问或规划可能先得到 202，再被 CLI 拒绝（终态同第 5 条）。依据：`src/multi_agent/store/review_annotations.py`，`review_findings.py:36-49`、`:73-77`，`actions.py`。
8. 讨论读取只对原始审核轮有效；交叉确认轮要回到原轮去读。依据：`review_findings.py:31`。
9. 记录判断、追问和关联修复这三种附注本身都不修改原审核轮的 `status` 和 `reasons`。同一轮的视图仍然可能因为席位补跑、交叉确认或确认不完整而变化；对后来候选的测试影响的是当前资格。出处：交接文档约定了讨论和原审核结论分别呈现；`src/multi_agent/cli/runner.py`，`review_findings.py`。
10. 追问的回答以 `kind: "answer"` 的条目出现在同一个 anchor 的 `activity` 里；关联修复以 `kind: "linked_plan"` 出现并带 `planning_run_id`。数据库约束只允许 `decision`、`answer`、`linked_plan` 三种。规划执行失败时也可能已经建好了可以重试的关联计划。依据：`src/multi_agent/store/migrations/0014_review_finding_activity.sql`，`tests/unit/test_web_finding_followup.py`。
11. `source.correction_baseline` 在仓库来源的审核里是被审核候选提交的 SHA；粘贴补丁的审核即使绑定了当前仓库，它也是 `null`。仓库来源时服务校验的是解析后的 Git 提交，指向同一个 SHA 的分支名也能通过；粘贴来源仍要选一个有效的基线。出处：交接文档对粘贴补丁的空基线有明文说明；`review_findings.py`，`tests/unit/test_web_review_findings.py`，`src/multi_agent/store/planning_store.py`。
12. 返工资格读取单独经过读入口；忙碌时只保证答复里有 `integration_id`、`eligible: false`、`lock_busy: true`。预检复用执行逻辑，但状态会变，预检给的原因和之后返工命令给的拒绝原因不保证字面相同。依据：`server.py:265-274`，`review_findings.py:111` 起。
13. `integration.rework` 成功创建子集成时，作业结果用 `result.rework.child_id` 或 `result.revision.integration_id` 给出它。被拒绝、被取消或没有产出修订时可能没有 ID；有子集成的 ID 也不代表随后的重测和复审通过。依据：`src/multi_agent/cli/integration_autorework.py`。
14. 接入检查是只读的，不经过读入口，不调用模型，有作业时也能读；它会读 Git 和已有的环境观测。打开面板不触发检查，只有显式的 `environment.refresh` 才按提供方运行。规则状态 5 个值、提供方状态 6 个值（含服务端的 `checking`）、下一步 8 个值，见 2.5。依据：`onboarding.py:31-129`。
15. 已知且支持验收的集成，在验收前读交付摘要得到 409 `job_unavailable`。验收后固定的是验收事实：收据指定的测试尝试、不可变的成员和审核快照，目标分支移动不改变它们。整个答复不保证逐字节固定：换端口后 Markdown 里的本机绝对链接会变。不存在或不属于本仓库的记录通常是 404，旧归档还可能是 `archive_unsupported`。跨实例比较时比收据、SHA 和证据哈希，页内导航用相对的 `links`。依据：`delivery.py`，`tests/unit/test_integration_verification_cli.py`。
16. 讨论读取、活动读取、交付摘要、接入检查都不经过串行化仓库操作的读入口，作业运行期间可以发送。活动读取会读私有日志的锁和咨询所有者。依据：`server.py:233-277`，后端核对答复 Q14。
17. 没有通用的「动作成功」字段。先看作业的 `state` 和 `error`，再看对应的 `result`。真实联调第 9 步两个席位都成功、审核轮可以评估，但 `assessment.status` 是 `needs_rework`，退出码是 1；第 7 步的 `verification: verified` 表示失败的测试结果有可核验的证据。环境检查和被中断的作业可以没有退出码。依据：`src/multi_agent/web/jobs.py`，`src/multi_agent/cli/task_review_workflow.py`。
18. `GET /api/jobs/{id}` 在页面刷新、服务重启之后仍能按作业号读到已结束或已中断的作业，之前在运行的作业会登记为中断。这依赖同一个仓库和完好的工作台数据目录；换一份新的数据目录或只打开 SQLite 归档，不会带入原来的作业。列表的最近条数限制不等于按 ID 删除记录。依据：`src/multi_agent/web/jobs.py`。

## 4　验证是怎么做的

### 4.1　组件测试

`frontend/src/test/workbench-panels.test.tsx`（35 个用例），用替身传输层，不启动服务：

- 首页待办：同一计划两个可领取阶段分成两行；部分答复时保留并标记旧行；「都处理完了」和「只读到一部分」的区别；每个原因的中英文文案；计划页和阶段页的定位。
- 作业活动：重复轮询不重复显示事件；作业结束后只给缺失席位的恢复动作；慢答复晚到时会补读；重读期间旧恢复按钮不可用；每个状态值都有文案；缺键不崩溃。
- 审核意见：两席同一个意见 ID 时，判断、追问、关联修复各自绑定到选中的席位；判断失败的提示；作业期间新输入的问题不被清掉；关联修复从被审核的提交开始；粘贴补丁要求填基线；过期 anchor 被拒；返工资格的归属、拒绝原因和按钮状态。
- 首次接入：按选中的提交读规则；每种提供方状态；检查中不出现第二个按钮；没登录也能确认原文；空仓库不能确认；草稿按仓库恢复；换基线不沿用旧错误。
- 交付摘要：完整标识符；复制成功与失败的提示；409 的说明。
- 新读取缺键时页面仍能渲染；英文界面里新面板没有中文。

`frontend/src/test/page-boundary.test.tsx`（4 个用例）：页面抛错时保留导航并说明原因，重试时重新读取。

`frontend/src/test/task-evidence.test.tsx`：实施者报告的三种情况（没有、完成、范围不够）。

`corepack pnpm run check` 共 101 项，全部通过。

### 4.2　e2e

- 全量 21 个用例，跑在离线环境上：真实的 Git、SQLite 和 HTTP 服务，替代的模型 CLI。2026-11-25 最后一次全量结果是 19 个直接通过，2 个（`cancel.spec.ts:20`、`claim.spec.ts:7`）因为电脑合盖睡眠超时，单独重跑后通过。
- `frontend/e2e/workbench.spec.ts` 的 4 个用例覆盖 P1–P5：
  - 首次接入：按选中的提交检查规则，草稿留在本机，没登录也能确认原文。
  - 空仓库：提示先提交基线。
  - 主流程：两个可领取阶段进待办；作业运行中的部分答复；集成双席审核中一席完成后硬停服务，重启后只补跑缺失的席位；两席同一个意见 ID 下的判断、追问、关联修复；返工资格和返工；验收后的交付摘要；移动目标分支后摘要不变；只读归档。
  - 离线演示模式：样例里没有这些接口时新面板隐藏。

### 4.3　`frontend/e2e/workbench_env.py` 怎样搭环境

- 它建立在 `tests/unit/web_support/` 的离线环境上，导入 `fake_runtime`、`offline_env`、`offline_fixture`（`workbench_env.py:21-25`）。
- 它替换了测试支持代码里的五个名字：`fake_runtime._planning`、`fake_runtime._response`（`workbench_env.py:166-167`），`offline_env._serve`、`offline_env.create_fixture`、`offline_env.SCENARIOS`（`workbench_env.py:177-179`）。后两个没有下划线，同样没有公开的扩展约定。这些名字改了它就要跟着改，后端调整测试支持代码时请联跑 workbench 的 e2e。
- 它不读写后端的数据库，也不导入 `src/multi_agent` 的内部模块。临时仓库用 `git` 命令建立，额外加了两个分支（一个没有规则文件，一个规则无效）。
- 替代 CLI 额外提供：一个首尾两个阶段都能领取的计划；目标里带 `FAIL-CONSULT` 时顾问失败；集成审核两席报告同一个意见 ID；第一次集成审核的 Claude 席位一直等待，直到服务被停；追问的回答；交叉确认；关联修复的卡。
- `workbench.spec.ts` 有两处直接碰后端的私有文件：归档用例把数据目录里的 `reviews.sqlite3` 和 `reviews.sqlite3-wal` 复制出来，再用 `--read-only-database` 另起一个服务（`workbench.spec.ts:171-175`）；移动目标分支是在临时仓库里直接跑 `git`（`workbench.spec.ts:214`、`:644`）。
- 硬停是向环境进程发 `SIGUSR1`，由 `offline_env.py` 的 `_hard_stop` 停掉服务和它启动的所有进程，再在原端口重启（`frontend/e2e/support.ts:61-63`）。

### 4.4　真实模型联调

- 目标仓库是新建的临时 Git 仓库，基线 `409be79c8eba6d20ec219e5e88fb23d2fcf19969`，只有 `calc.py`、`tests/test_calc.py`、`.multi-agent/rules.yaml`。规则登记一条命令 `unit: python3 -m unittest discover -s tests -t .`。
- 服务启动参数：`python -m multi_agent.web --repository <临时仓库> --data-dir <临时数据目录> --port 61598 --frontend-dist frontend/dist`。没有指定 `--codex-bin` 和 `--claude-bin`，用本机的 codex-cli 0.169.0 和 Claude Code 2.1.325。
- 第 9 步之后加了 `--tool-directory /opt/homebrew/bin --runtime-read-path /opt/homebrew` 重启。原因：测试沙箱的 `PATH` 是工具目录加 `/usr/bin:/bin:/usr/sbin:/sbin`（`src/multi_agent/adapters/test_isolation.py:34`、`:127`），这台 Mac 的 `/usr/bin/python3` 是 Xcode 占位程序，测试命令因此失败。
- 所有写操作都是无头浏览器在页面上点出来的；模型和等级用页面默认值；同一时间只有一个作业。共 20 个作业，其中 11 个启动过模型会话，合计 17 次：Codex 启动 14 次（13 次有完成通知，1 次在硬停时中断），Claude 启动 3 次（两次阶段卡片复核和第 20 步的草拟）。

| # | 动作 | 作业号 | 退出码 | 秒 | 请求的模型 | 报告的模型 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `environment.refresh` codex | `8234782181a54f41aa203b26f84787d2` | 无 | 1.2 | 不用模型 | — |
| 2 | `environment.refresh` claude | `0f7c4f010fda4bd9b5996fc80e44f36e` | 无 | 0.2 | 不用模型 | — |
| 3 | `project.consult` | `5beb93e1158e456aad4bbac32c41e5d4` | 0 | 17.5 | gpt-6.2-astra high | 未确认 |
| 4 | `project.confirm` | `761aa9a3c0284b1fbfe4c19197ef3793` | 0 | 0.2 | 不用模型 | — |
| 5 | `project.plan` | `9293b8524a954dafa08910cca54e5bb7` | 0 | 23.0 | gpt-6.2-astra high | 未确认 |
| 6 | `project.stage.start` | `684fffa38944460d8e0190c973a19a2d` | 0 | 100.1 | 拟卡和实施 gpt-6.2-sol high；卡片复核 claude-opus-5-5 high | 拟卡和实施未确认；复核 claude-opus-5-5 |
| 7 | `task.test` | `e563e579518f40eea2211c77855f0e93` | 1 | 28.5 | 不用模型 | — |
| 8 | `task.review` 双席 | `b1ac47dc1482488f8c326b07c4a9ef5e` | 无 | — | gpt-6.2-astra high；gpt-6.2-sol high | 未确认 |
| 9 | `task.retry_review` | `c69e60d3d3b7465aad3c1b4d25245678` | 1 | 35.1 | gpt-6.2-sol high | 未确认 |
| 10 | `review.followup` | `ae493d0e9b334cfdac53c8165ecb6a8c` | 0 | 9.3 | 席位原模型 gpt-6.2-sol | 未确认 |
| 11 | `task.rework` | `8b789c25e5534aec81dd523a078bc5df` | 1 | 34.8 | gpt-6.2-sol high | 未确认 |
| 12 | `project.revise` | `6069705100bb443a9da300387e2d4a6c` | 0 | 0.1 | 不用模型 | — |
| 13 | `project.stage.start` | `16b631bc65de485ebac2a67cd9de4f5f` | 0 | 100.0 | 同第 6 步 | 同第 6 步 |
| 14 | `task.test` | `79b1a3b8710343fba1cf0708cc36ab03` | 0 | 32.6 | 不用模型 | — |
| 15 | `task.review` 单席 | `fd5dd45818d54e3cb32e18d98153cfa5` | 0 | 38.4 | gpt-6.2-astra high | 未确认 |
| 16 | `integration.create` | `9d430524dbf9460ba2ddaa7b2d16cf36` | 0 | 2.7 | 不用模型 | — |
| 17 | `integration.test` | `4784f299b72048c7a7eb778923e0c1ed` | 0 | 7.3 | 不用模型 | — |
| 18 | `integration.review` 单席 | `2597911e9bb545598a654ae5b0ea68d7` | 0 | 21.6 | gpt-6.2-astra high | 未确认 |
| 19 | `integration.accept` | `b991041b33664b349c365080064abb8e` | 0 | 0.8 | 不用模型 | — |
| 20 | `plan.create` | `fd67750dd81942eb97a4dc40e8effa9b` | 0 | 44.3 | 草拟 claude-opus-5-5 high；复核 gpt-6.1-sol high | 草拟 claude-opus-5-5；复核未确认 |

推理等级在所有会话里都没有报告值。

各步要点：

- 第 8 步在 reviewer-1 成功、reviewer-2 还在运行时被硬停，作业状态是 `interrupted`，错误码 `service_interrupted`。
- 第 9 步的参数原样取自活动读取的恢复动作：`{round_id, seat_keys: ["reviewer-2"], expected_attempts: {"reviewer-2": 1}, recover: true}`。之后审核轮和冻结快照都没变，reviewer-2 的第 1 次记为失败、第 2 次成功。
- 两次记录判断都返回 201。第 10 步的 anchor 是 `{round_id, snapshot_id, seat: "reviewer-2", attempt: 2, finding_id: "T-0001-UNIT-EXIT"}`。
- 第 11 步运行时状态是成功，`result.outcome` 是 `scope_blocked`。之后任务 `phase: planning_required`、`candidate: null`、`rework_used: true`、下一步 `user_decision`。
- 第 19 步之前读交付摘要返回 409；之后读到的 Markdown 共 955 个字符，页面预览和剪贴板里的文本与接口返回逐字符相同。验收后临时仓库的 `main` 没有移动。
- 第 20 步只带 `baseline` 和 `requirements`。

主要记录：

- 第一次领取：阶段运行 `stage-78f639df8c7d4f3d8db4ac5d04a84df5`，任务 `5d1d9d26d68e432292ab07be83436ca2`，候选 `9076defb8b0f6dc795db1ee3c5c31006e26c3273`，审核轮 `0ff9d899b0c24df4bf713114fa3cff58`。
- 第二次领取：计划 `plan-f33577206cec4487ba2bfc87480c5e55`，阶段运行 `stage-09db69eba5594aa2a94b10535a23afb1`，任务 `e8d16476583d43dcab5e3854f23c2fd5`，候选 `0b752adf60f2243de75bbea03e782ab04224880d`，审核轮 `da0db8c33d90443bb64138aa985d398a`。
- 集成 `35c2e57be8494e37a440f0b977a8ad9a`，候选 `e062da8c68a6de5e3e91110547305c04010ffb36`，审核轮 `0de61c3033584bdda7306ce5e4d1972c`，验收收据 `ed6809e8996742c0806f048600490730`。
- 默认路径的单卡规划 `9e283349251f46c9b84254e022d46a57`。

原始记录：截图和逐步记录在 `frontend/e2e/records/real-trial/`（被 Git 忽略），其中 `trial-data.tgz` 是临时仓库和数据目录的打包。

## 5　发现的后端问题

细节都在 [`docs/workbench-backend-followups.md`](workbench-backend-followups.md)，这里只列编号。

1. 单卡规划的默认复核模型和页面预设不一致；真实 Codex 能走通，离线环境走不通。见第 1 条。
2. 硬停后等待恢复的审核不进首页待办。见第 2 条。
3. 交付摘要的 Markdown 和待办的 `detail` 不随界面语言变化。见第 3 条。
4. 演示样例里没有这些新接口。见第 4 条。
5. 测试沙箱里的 `python3` 是 Xcode 占位程序，接入检查看不出来。见第 5 条。
6. 测试因为环境失败后，同一次实施没有办法重测。见第 6 条。
7. 阶段任务在等测试、等审核、等创建集成时都不进待办。见第 7 条。
8. 被新版本计划取代的任务一直留在待办里。见第 8 条。
9. Codex 的角色没有运行时报告的模型和等级。见第 9 条。

## 6　没有验证或只有组件测试覆盖的部分

- `integration.retry_review` 的恢复在 e2e 的离线环境里走过；真实模型下只走过 `task.retry_review`。
- 真实模型下没有走过：`integration.rework`、返工资格里有阻塞意见的情况、交叉确认、关联修复的提交（只打开了预填的表单）、粘贴补丁的审核、分派模式的阶段和子卡。
- 前端这一侧只有组件测试覆盖（后端另有自己的用例，例如接入状态、过期 anchor 和粘贴补丁）：待办每个原因的文案；每种提供方状态；`check_failed`、`missing_cli`、`login_required` 的接入检查；过期 anchor 被拒；复制失败的提示；新读取缺键时的渲染。
- 没有验证：后端是否接受对同一次实施再提交一次 `task.test`。页面只按 `next_action` 给按钮，没有绕过去试。
- 第 7 条里「等测试」「等审核」两个时点不进待办，是读 `attention.py:209-227` 得出的，联调里没有截图。
- 两个浏览器标签页同时编辑同一个仓库的目标草稿时，后写的覆盖先写的。这是有意的，没有做合并。

## 7　请后端核对的问题

请逐条回答「是」「否」，否的话说明该改哪一边。

1. 待办的 `reason` 是否只有这 20 个值：`confirm_goal`、`consultation_failed`、`plan_goal`、`retry_project_plan`、`choose_stage`、`dispatch_failed`、`planning_attention`、`planning_question`、`implementation_attention`、`task_test_failed`、`integration_conflict`、`rebuild_integration`、`inspect_candidate`、`test_integration`、`integration_test_failed`、`inspect_tests`、`review_integration`、`review_incomplete`、`review_decision`、`accept_integration`？
2. 待办行的 `href` 是否保证是站内相对路径，并且 `kind/id/reason/href` 在一次答复里唯一？
3. `complete: false` 是否只在有作业占执行锁时出现？这时返回的 `items` 是否都是当时确实成立的待办？
4. 没有作业时 `GET /api/attention` 是否会占读锁？页面在空闲时把它和其他占锁读取串行发送，这样做是否必要？
5. 活动事件的 `sequence` 是否在一个作业内唯一、只增不减，服务重启后也不会重排或复用？
6. 服务是否只保留每个作业最近 50 条事件？`history_truncated` 是否就表示这个？
7. 页面把 `recovery[]` 的 `action` 和 `args` 原样提交，这是否是预期用法？`expected_attempts` 不匹配时服务返回什么状态码和错误码？
8. 恢复动作是否只会在某个作业结束时变化？有没有不经过作业就变化的情况？
9. 意见的 anchor 是否必须原样回传五个字段？席位补跑后旧 anchor 是否一定被拒绝，错误码是否是 `invalid_request`？
10. 记录判断、追问、关联修复是否都不改变审核轮的 `status` 和 `reasons`？
11. `activity[].kind` 是否只有 `decision`、`answer`、`linked_plan` 三个值？
12. `source.correction_baseline` 是否在仓库来源时总是被审核的候选提交，在粘贴补丁来源时总是 `null`？
13. `plan.create` 带 `review_anchor` 时，`baseline` 不等于 `correction_baseline` 是否会被拒绝？
14. 讨论读取、活动读取、交付摘要、接入检查这四个读取，在作业运行期间发送是否安全，是否都不占执行锁？
15. `integration.rework` 的作业结果里，新集成的编号是否总在 `result.rework.child_id` 或 `result.revision.integration_id` 里？
16. 交付摘要是否保证只取决于验收收据，之后任何操作都不改变同一个集成的答复？验收前的 409 是否稳定，错误码是什么？
17. 接入检查里 `rules.status`、`providers.*.status`、`next_steps[].kind` 的取值是否就是 2.5 列的那些？`providers` 的键是否只有 `codex` 和 `claude`？（已答复：提供方状态是 6 个，含服务端的 `checking`，2.5 已改。）
18. 作业退出码非 0 而动作本身成功（例如补跑成功但结论是需要返工）时，页面应该用哪个字段区分「作业失败」和「结论不是通过」？
19. `attempts[].runtime.result.outcome` 是否只有 `completed` 和 `scope_blocked`？页面把它标成「实施者报告」而不是核验结论，这个定位对不对？
20. 活动读取里 `actors[].state`、`reviews[].seats[].state` 和 `events[].event` 的全部取值，是否就是 2.2 里页面有文案的那 13 个状态和 13 个事件？（已答复：三个字段是三组不同的取值，事件 12 种、审核席位状态 5 种、actor 状态 8 种加派生的 `unconfirmed`，2.2 已分开列。）
21. `frontend/e2e/workbench_env.py` 替换了 `fake_runtime._planning`、`fake_runtime._response`、`offline_env._serve`、`offline_env.create_fixture`、`offline_env.SCENARIOS` 五个名字。后端是否愿意把这几个点改成公开的扩展点，或者接受这种依赖？
22. `docs/workbench-backend-followups.md` 第 1–9 条里，哪些是有意的设计、不需要改？

## 8　对仓库和这台电脑做过的其他操作

- 重新生成的被忽略目录：`frontend/dist`（多次重建）、`frontend/test-results`、`frontend/e2e/records/`。`records/` 下新增了 `workbench-shots/`、`ink-3/`、`tone-unknown/`、`real-trial/`。
- 临时仓库和数据目录：e2e 和离线环境的临时目录由环境进程自己清理。真实联调的临时仓库和数据目录还在那次会话的系统临时目录里，没有清理，重启或系统清理后可能消失；打包副本在 `frontend/e2e/records/real-trial/trial-data.tgz`。
- 真实联调中做过一次没成功的探测：把数据目录复制一份另起服务，提交的作业被拒，退出码 2，信息是 `task execution paths differ from the owned sibling layout`。副本已删除。
- 试验服务已停，没有残留进程。
- `frontend/multi-agents/` 是主仓库的一个嵌套克隆，没有独有内容。里面的 mock 服务由仓库主人自己停掉，目录由他移到了 `~/.Trash/multi-agents`。
- 设计令牌只动了两个，都在 `frontend/src/styles/tokens.css`：
  - `--ink-3`：亮色 `#8c939a` 改为 `#616971`，暗色 `#66717b` 改为 `#7f8a93`。
  - `--tone-unknown`：亮色 `#8c939a` 改为 `#616971`；暗色保持 `#66717b`。
- `frontend/playwright.config.ts` 把 `reducedMotion` 挪到了 `contextOptions` 里，原来的位置不生效。
- 没有提交、推送、stash、reset、checkout 或 clean。

## 9　后端核对后的更正

后端的核对答复在 [`docs/workbench-backend-integration-review.md`](workbench-backend-integration-review.md)。下面是据此对这份文档和前端做的更正。Q、A、F 的编号沿用那份答复。

### 9.1　文档里改了什么

- 2.5 和第 7 节第 17 问：提供方状态是 6 个，`checking` 是服务会返回的值。
- 2.2 和第 7 节第 20 问：事件 12 种、审核席位状态 5 种、actor 状态 8 种加派生的 `unconfirmed`，分开列出；文案表是超集，不是封闭的协议。`history_truncated` 的含义改成「这次读取被窗口截断」。
- 2.6：后端依据改为 `tests/unit/test_integration_verification_cli.py`；降级处理补上 `job_unavailable` 和 `archive_unsupported`。
- 第 3 节：第 1、4、9、11 条注明了交接文档里已有的约定；第 2、5、6、7、13、15、17 条按答复改成了准确的说法；第 3、10、12、14、16、18 条补了答复里确认的细节。
- 第 4.3 节和第 7 节第 21 问：被替换的测试支持名字是五个。
- 第 4.4 节：会话计数改为 Codex 启动 14 次（13 次完成、1 次硬停）、Claude 3 次。`docs/workbench-backend-followups.md` 第 9 条的计数同步改了。
- 第 6 节：「只有组件测试覆盖」限定为前端这一侧。

### 9.2　前端改了什么

改动都没有提交。函数名和组件名后面是所在文件。

| 依据 | 改动 | 位置 |
| --- | --- | --- |
| Q6 | 「服务只保留最近 50 条」改成「一次读取最多返回最近 50 条，更早的记录还在服务那边」。 | `JobActivityView`，`frontend/src/pages/JobActivity.tsx` |
| Q8 | 作业已中断且 `controller_holds_journal` 为真时，活动每 15 秒重读一次，页面不可见时暂停；页面回到前台时重读；「可以继续的动作」下加了「重新读取」按钮和读取时间。事件按作业分开合并：作业详情页以作业号为 key 重新挂载（`frontend/src/app/App.tsx` 的 `/jobs/:id`），钩子在作业号变化时也会清空已合并的事件。 | `useJobActivity`，`JobActivity.tsx` |
| Q7、Q9、Q11、Q13、Q18 | 新增 `jobEnding`，把作业的结局分成七种，`jobBadge` 和各处提示都用它。判定顺序见 9.5。 | `frontend/src/components/Badge.tsx` |
| Q7 | 恢复按钮提交后被拒绝时，说明是服务拒绝、读到的动作已过期，并立即重读活动。活动读取失败后每 5 秒自己重试一次，出错时也有「重新读取」按钮。 | `JobActivityView`、`useJobActivity` |
| Q9、Q11 | 追问被拒绝时，显示服务给的原因，说明这条意见可能属于已被替换的尝试，并重读意见；取消和中断各有说法；问题留在输入框里。 | `followupProblem`、`FollowupForm`，`frontend/src/pages/ReviewFindings.tsx` |
| Q11、Q13 | 关联修复的 `plan.create` 结束时如果没有可打开的规划、结局又不是成功，就留在表单上说明结局并重读意见。这条意见下如果多出了这次提交建的 `linked_plan`，就给出那份规划的链接并禁用提交按钮。作业被中断时，提交按钮先禁用，用户点「重新读取这条意见」之后才放开，因为它启动的 CLI 可能还在建规划。 | `TaskPlanForm`，`frontend/src/pages/NewTaskPlanPage.tsx` |
| Q7 | 同步的拒绝：400 `invalid_request` 显示服务给的原因；409 `busy` 说明这次没有提交。两种情况都会通知调用处重读（`onRejected`）。 | `ActionButton`，`frontend/src/components/Action.tsx` |
| Q3 | 部分待办的说明不再归因于「有作业在运行」；保留的旧行只说「待刷新」，并写明现在能不能操作要等重新读取；部分答复为空时不显示成「没有待办」。没有作业而答复是部分的时候，每 10 秒重读一次，页面不可见或上一次读取还没回来时跳过。「还没有项目」的空首页上，部分答复和读取失败也会显示出来。 | `useAttention`、`AttentionPanel`，`frontend/src/pages/Attention.tsx`；`frontend/src/pages/HomePage.tsx` |
| Q15 | 作业的结局不是成功而又产生了子集成时，跳转地址带上 `?rework_job=`，子集成页顶部说明那个作业的结果不是成功、返工之后的重测或复审没有通过或没有跑完，并给出返工自己说的原因和作业链接。手动返工，以及审核、交叉确认、补跑里的自动返工，都这样处理。没有子集成时显示原因；被拒绝、被中断、被取消各有说法，被中断时说明 CLI 可能还会产生新的集成。只有还停在原集成页上的用户才会被带到子集成。 | `ReworkBlock`、`ReworkOutcome`，`frontend/src/pages/IntegrationExtras.tsx`；`IntegrationPage`，`frontend/src/pages/IntegrationPage.tsx`；`openIntegrationRevision`，`frontend/src/pages/bits.tsx` |
| Q18 | 作业详情页对「跑完了但结果不是成功」和「被拒绝」各加一段说明；作业栏对这两种结局各加一句话。环境检查没有退出码，照常显示；其他没有退出码的已结束作业写「没有观测到退出码」。作业刚结束、详情页自己的读取还没回来或失败时，用作业中心最后一次读到的结果，读取失败时可以重试。 | `JobPage`，`frontend/src/pages/JobPages.tsx`；`JobBar`，`frontend/src/app/JobBar.tsx` |
| Q16 | 交付摘要读取同时认 409 和错误码 `job_unavailable`；`archive_unsupported` 给出说明，不当作错误。 | `DeliverySummary`，`IntegrationExtras.tsx` |
| F3 | 英文界面下在预览下面标注导出的摘要是中文。 | `DeliverySummary` |
| F5 | 接入检查在已登记的命令下面写明：这里只确认命令已登记，运行环境能不能跑还没有验证。 | `OnboardingPanel`，`frontend/src/pages/Onboarding.tsx` |
| F6 | 任务页加了「按原环境重新测试」。提交 `task.test`，参数取自上一次测试的记录：`task_run_id`、`implementation_attempt`、按 `sequence` 排好的命令名、`details.declaration_source`。出现条件见 9.3。 | `TaskPanel`，`frontend/src/pages/TaskPage.tsx`；`TaskTest` 类型，`frontend/src/api/types.ts` |
| Q20 | 状态和事件的文案表保持为超集，不认识的值原样显示；没有测试把它断言成封闭集合，另加了一条测试确认回退。 | `JobActivity.tsx`、`Attention.tsx` |

### 9.3　重测入口的出现条件

入口只在下面的条件都从任务记录里读得出来时出现：

- `next_action.kind` 是 `task_review` 或 `reconciliation_required`，并且它的 `test_attempt` 就是当前实施最新的那次测试。按 `src/multi_agent/cli/task_cli.py` 的逻辑，这两种情况都表示候选已核验、当前实施还没有审核。前一种是测试的证据已核验、命令失败；后一种是测试超时、被取消或证据没有核验通过。
- 那次测试的 `phase` 是 `failed`、`cancelled` 或 `awaiting_evidence`，`details.finished` 为真，`source_current` 不为假。这三种阶段是 `src/multi_agent/core/transitions.py` 的 `_task_test_start` 接受新一次测试的阶段。
- 任务有候选；测试记录里有 `details.declaration_source` 和命令列表。

把 `reconciliation_required` 也算进来，是因为否则会出现死路：用户点了重测又取消它，新的那次测试成为当前测试，`task_review` 不再给出，页面上就既不能测试也不能审核。

当前实施已经有审核时不给入口。那之后重测会让已有的审核不再对应当前测试，而 `task_cli.py` 在有审核时不再给出 `task_review`，页面上就既不能返工也不能重新审核。

入口旁边写明：重新测试会成为这个候选当前的测试，它结束之前不能开始审核。

有两个条件任务记录里读不出来，由服务在提交时检查，被拒绝时页面显示它给的原因：

- 那次测试的所有进程是否真的已经停止。记录里的 `details.all_descendants_stopped` 和 `command_group_gone` 在这个视图里总是 `null`。
- 服务现在的测试策略（工具目录等）是否和上一次相同。不同的话服务返回 `test retry must preserve its managed policy`。

### 9.4　这次没有做的

- F7 三个新待办原因的文案，等后端定下原因码。
- F1 默认复核模型的显示，等后端把生效的默认值暴露出来。
- F4 演示回放里判断提交的回放，等后端重录样例。
- F2、F8、F9 和「追问的提示里不带差异」，都归后端。

### 9.5　作业结局的判定

`jobEnding` 按下面的顺序判定，依据是 `src/multi_agent/cli/errors.py` 和 `src/multi_agent/web/jobs.py` 里的错误码：

1. `state` 是 `interrupted`：已中断。`state` 不是 `finished`：未结束。
2. 退出码 130，或 `error.code` 是 `cli_cancelled`，或结果里写明取消：已取消。
3. `error.code` 是 `cli_runtime_failed`、`followup_failed`、`invalid_cli_output`、`cli_io_failed`、`cli_launch_failed`、`state_write_failed` 之一：失败，不看退出码。
4. 退出码 2 且没有结果：被拒绝。原因在 `error` 里，错误码可以是 `cli_refused`，也可以是业务自己的码，例如 `integration_accept_refused`。
5. 还有别的 `error`：失败。
6. 退出码 0，或者没有退出码：已结束。
7. 退出码 1 且有结果：跑完了但结果不是成功。页面不说它是哪一种，因为测试没过、审核要求返工、规划或返工没有做成都是这个样子，具体看对应的记录。
8. 其余：失败。

第 3 步的错误码清单是照后端源码抄的。后端以后新增一个同样以退出码 2 结束的失败码时，页面会把它显示成「被拒绝」。请后端确认这份清单，或者在作业答复里给一个区分「拒绝」和「出错」的字段。

### 9.6　复核和验证

- 两个只读子代理复核了这一轮的改动，一个看重测入口和重读的状态逻辑，一个看文案和错误处理。共 19 条发现，都改了。其中「空首页显示部分答复」和三处空值保护没有单独的测试。
- 有一条只能缓解：关联修复的作业被中断后，页面要求先重新读取意见再提交；重新读取之后 CLI 仍可能晚一步建好规划，这时再提交就会有两份。
- `corepack pnpm run check`：120 项通过。
- e2e：复核之前的版本全量跑过一次，21 个通过，用时 12.7 分钟。复核修正之后的版本分三组在前台跑完了全部 12 个文件、21 个用例，都通过，用时 4.7、5.8 和 2.7 分钟。分组是因为保持唤醒的请求被这台电脑的权限分类器拒绝了，前台运行期间电脑不会因闲置睡眠。
- `git diff --stat -- src tests` 仍是 32 个文件、485 增 35 删。

## 10　按 11 月 26 日交接做的改动

依据是 [`docs/workbench-frontend-handoff.md`](workbench-frontend-handoff.md) 的「11 月 26 日联调交接」一节，字段按当时的后端代码核对过。改动都没有提交。

- **9.5 的错误码清单已被 `error_kind` 取代。** `jobEnding`（`frontend/src/components/Badge.tsx`）只按作业的 `error_kind` 区分「被拒绝」和「失败」。这个字段缺失时页面不下判断，显示「有错误」和原始的错误信息。`state`、取消、退出码和结果仍然分别读取：退出码 1 且带结果的审核作业提示去看审核意见，测试作业提示去看失败的命令。
- **重测入口按 `details.retry_ready` 出现**（`frontend/src/pages/TaskPage.tsx`）。`next_action.kind` 是 `task_review` 或 `reconciliation_required` 时，取它的 `test_attempt` 对应的测试，这个字段为真才给入口。页面不再自己推断资格，9.3 里的推断规则作废。
- **关联修复去重**（`frontend/src/pages/NewTaskPlanPage.tsx`、`TaskPlanPage.tsx`）。`plan.create` 的结果里有 `reused: true` 时，直接打开返回的 `planning_run_id`，规划页说明这是已经存在的那一份。上一轮「查 `linked_plan` 再禁用提交」和「中断后先重读」两处缓解逻辑已删除。
- **F7**：首页原因表补了 `test_task`、`review_task`、`integrate_stage`，共 23 个值。
- **F1**：单卡规划表单在没有勾选「指定」时显示 `defaults.task_plan` 里的默认请求，只显示，不提交。接口没有这个字段时不显示。
- **F4**：演示回放（`frontend/src/mock/transport.ts`）支持 `POST /api/reviews/{id}/decisions`，只回放录制过的那一次判断，之后的读取用录制里紧跟它的切片。追问和关联修复只对录制过的那条意见回放，追问还要求问题与录制的一致。环境检查按提供方匹配。录制里没有的提交会被拒绝，并说明这是演示。每一页顶部都有演示提示。
- **验证**：`corepack pnpm run check` 127 项通过；e2e 的 12 个文件、21 个用例在这一轮的代码上都通过，复核后的修正又重跑了 `workbench.spec.ts` 和 `cancel.spec.ts`。后端工作区没有因为这一轮而变化。

**重测入口的后续决定（11 月 26 日，仓库主人）。** 入口现在只看当前实施的当前测试的 `details.retry_ready`，不再看 `next_action.kind` 和 `next_action.test_attempt`，所以已有审核时也会出现。任务记录里每次实施只带一条测试记录，就是它的当前测试（`src/multi_agent/cli/task_cli.py:412-413`），页面取 `implementation_attempt` 等于当前实施的那一条。提交的参数不变：原来的实施编号、命令顺序和 `declaration_source`。已有审核时入口旁说明：之前的审核会保留，重测通过后要对新的测试结果重新送审。上面第 10 节第二条里关于 `next_action` 的条件作废。

**后端第四轮答复之后（11 月 26 日）。** 核对为主，改了三处：作业提交被 409 拒绝时，按钮旁显示服务给的原因（原来只有一句通用的话）；规划页的「重试规划」被拒绝后会重读规划并留下原因；复用提示和预算耗尽的说明改成新的行为，即还没开始或没完成的规划可以在本页重试，预算用完时相同的输入不会重置预算。还没开始的关联规划，后端给出 `plan_retry` 和 `retry_configurable: true`，页面原来就跟随这两个字段显示「重试规划」（`frontend/src/pages/TaskPlanPage.tsx`），没有自己的隐藏条件。演示用例按新样例补了三步检查：`rework` 第 72 步父集成指向子集成，`dual-partial` 第 54 步两席报告都有效，`cancel` 第 49 步执行者不再显示进行中。

