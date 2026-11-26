# 工作台后端待办事项

这些事项来自 P1–P5 页面接入时的联调。页面没有绕过其中任何一条，都留给后端处理。每条给出位置、复现步骤、期望与实际、建议改法和验收方式。背景见[前端交接](workbench-frontend-handoff.md)和[优化项目书](workbench-improvement-plan.md)。

第 1–4 条来自离线环境。第 5–9 条来自 2026-11-25 用真实 Codex 和 Claude 走的一遍主路径，记录在文末「真实模型联调记录」。

复现用的离线环境在工作台源码目录启动，它使用临时 Git 仓库、临时数据目录和替代模型 CLI，不消耗模型额度：

```sh
uv run python frontend/e2e/workbench_env.py --scenario workbench --port 0
```

进程先打印一行 JSON（临时仓库路径），再打印服务地址。下文用 `$ORIGIN` 表示这个地址。

## 1　单卡规划的默认复核模型和页面预设不一致

**位置**

- `src/multi_agent/cli/task_cli.py:628`，没有 `--plan-review` 时使用 `("gpt-6.1-sol", "high")`。
- 同一个值还出现在 `src/multi_agent/adapters/probe/config.py:50` 和 `src/multi_agent/adapters/probe/cli.py:28`。
- 页面预设目录在 `src/multi_agent/web/workspace.py:24-26`，其中 Sol 是 `gpt-6.2-sol`，目录里没有 `gpt-6.1-sol`。

**复现步骤**

1. 启动上面的离线环境，先对 `codex` 和 `claude` 各提交一次 `environment.refresh`。
2. 提交 `plan.create`，只带 `baseline` 和 `requirements`，不带 `plan_review`：

   ```sh
   curl -s -X POST "$ORIGIN/api/jobs" -H 'Content-Type: application/json' \
     -d '{"action":"plan.create","args":{"baseline":"<HEAD 的完整 SHA>","requirements":"修复审核意见：示例\n"}}'
   ```

3. 轮询 `GET $ORIGIN/api/jobs/<job_id>` 到结束，再读 `GET $ORIGIN/api/resources/plan/<planning_run_id>?implementation=1`。

**期望与实际**

- 期望：不指定复核模型时，默认路径能完成复核，规划进入 `ready`；页面预设和服务默认值是同一个模型。
- 离线环境的实际：作业退出码 1；规划停在 `failed`；复核尝试的请求是 `{"provider":"codex","model":"gpt-6.1-sol","effort":"high"}`，状态一直是 `running`；`details.source_errors` 为 `["planning execution has no qualifying reconciliation"]`。
- 真实 Codex 的实际：默认路径能走通。codex-cli 0.169.0 的 `model/list` 里有 `gpt-6.1-sol`，`thread/start` 的答复也是这个模型。作业 `fd67750dd81942eb97a4dc40e8effa9b` 退出码 0，用时 44.3 秒，复核结论 `ready`，规划 `9e283349251f46c9b84254e022d46a57` 进入 `ready`。
- 所以这一条不影响真实使用，问题在两处不一致：页面的 Sol 候选是 `gpt-6.2-sol`，不勾选「指定复核模型」时实际请求的却是 `gpt-6.1-sol`，表单上看不到这个默认值；离线环境走不通默认路径，前端的 e2e 因此每次都勾选「指定复核模型」。
- 页面上「新建单卡规划」和审核意见里的「发起关联修复」，在不勾选「指定复核模型」时都会走到这条默认路径。

**建议改法**

二选一：

- 把三处默认值改成 `gpt-6.2-sol`，并让 CLI 的默认值和 `workspace.py` 的预设取自同一处定义。
- 如果有意保留 `gpt-6.1-sol`：让离线环境的替代 CLI 接受它，并在 `GET /api/workspace` 里给出规划草拟和复核的默认请求，页面就能在表单上写明「默认：gpt-6.1-sol · high」。

**验收方式**

- 上面的复现步骤在离线环境里得到退出码 0，规划为 `ready` 且 `verification` 为 `verified`。
- 补一条后端测试：`plan.create` 不带 `plan_review` 时，复核请求的模型要么出现在 `GET /api/workspace` 的 review 角色候选里，要么等于 `GET /api/workspace` 给出的默认复核请求。

## 2　硬停后的审核不进首页待办

**位置**

- `src/multi_agent/web/attention.py:388` 的 `_integration_reason`：当前审核轮是 `IN_PROGRESS` 时在第 421 行直接返回 `None`。
- `src/multi_agent/web/attention.py:278` 的 `_review_status`：只有每个席位最后一次尝试都是 `succeeded` 或 `failed` 时，才把进行中的轮次算作 `INCOMPLETE`。
- 任务审核（`_task_reviews`，第 340 行）和独立审核（`_standalone_reviews`，第 295 行）走同一个判断，行为相同。
- 恢复动作只在 `src/multi_agent/web/activity.py:220-232` 给出：作业已中断、控制进程已释放活动记录、席位仍记为 `running` 时，返回带 `recover: true` 的补跑动作。

**复现步骤**

1. 在离线环境里走到一个已测试的集成，提交双席 `integration.review`。这个环境里 Claude 席位的第一次集成审核会一直等待。
2. 等 `GET $ORIGIN/api/jobs/<job_id>/activity` 显示 `seat-1` 为 `succeeded`、`seat-2` 为 `running`，然后向环境进程发 `SIGUSR1`（硬停服务及其子进程并在原端口重启）。
3. 读 `GET $ORIGIN/api/attention` 和 `GET $ORIGIN/api/jobs/<job_id>/activity`。

`frontend/e2e/workbench.spec.ts` 的第三个用例自动走完了这三步，可以直接参照。

**期望与实际**

- 期望：待办里有一行指向这次等待恢复的审核。项目书对首页的要求是「用户离开再回来，应能看到哪一步可以继续」。
- 实际：`attention` 返回 `complete: true`，但没有这个集成的任何一行。原因是 `seat-2` 在库里仍是 `running`，轮次被当作进行中。
- 同一时刻 `activity` 给出了恢复动作 `integration.retry_review`，参数含 `seat_keys: ["seat-2"]`、`expected_attempts: {"seat-2": 1}` 和 `recover: true`。
- 结果是重启后首页和集成页都没有入口，用户只能到「作业」列表里找那条已中断的作业。

**建议改法**

在待办里补一行，页面不另做判断：

- 没有活动作业（读锁可用）时，如果当前审核轮或它的交叉确认轮处于进行中，且有席位的当前尝试是 `running` 而没有存活的控制进程，就产出一行。
- 判断「没有存活的控制进程」沿用 `activity.py` 里的条件，不要在 `attention.py` 里另写一套。
- `reason` 可以复用 `review_incomplete`，页面已有文案「审核没有完成，补跑缺失的席位」。如果要和「席位失败」区分，新增 `review_interrupted` 也可以，页面对未知值会显示 `detail`。
- `href` 指向持有恢复动作的页面。目前只有作业详情页能拿到带 `recover` 的动作，所以要么指向 `/jobs/<job_id>`，要么给集成页和任务页提供同样的恢复动作读取。

**验收方式**

- 上面的复现步骤之后，`GET /api/attention` 含一行指向该审核，`href` 打开的页面上能看到并执行「接管并补跑中断的席位」。
- 补跑完成后这一行消失；有作业在运行时这一行不出现在 `complete: false` 的答复里，也不误报正在正常运行的审核。
- 任务审核和独立审核各补一条同样的测试。

## 3　交付摘要和待办说明的语言不随界面变化

**位置**

- `src/multi_agent/web/delivery.py:53-76`：Markdown 的标题和字段名固定是中文（「集成交付摘要」「验收时间」「候选提交」等）。
- `src/multi_agent/web/attention.py`：各处 `_item(...)` 的 `detail` 是固定的英文句子，例如 `"Tests and review qualify this candidate for acceptance."`。

**复现步骤**

1. 把界面切到 English，打开一个已接受的集成页，看「Summary preview (Markdown)」。
2. 把界面切到中文，打开首页，看待办行下面的说明。

**期望与实际**

- 期望：导出的 Markdown 和待办说明跟随用户选择的界面语言，或者明确规定它们只用一种语言。
- 实际：英文界面里预览和复制出来的是中文 Markdown；中文界面里待办的说明是英文。
- 页面目前的处理：Markdown 原样预览和复制；待办只显示带记录信息的 6 种 `detail`（`choose_stage`、`planning_question`、`dispatch_failed`、`planning_attention`、`implementation_attention`、`task_test_failed`），其余固定句子不显示，改用页面自己的中英文文案。

**建议改法**

- 交付摘要：`GET /api/integrations/{id}/delivery` 接受 `?lang=zh` 或 `?lang=en`，按语言输出标题和字段名；不带参数时保持现在的中文。收据里的事实（SHA、哈希、命令、退出码）不变。
- 待办：`detail` 只保留记录自身的内容（阶段目标、问题原文、拒绝原因）。其余原因不需要句子；如果想保留阶段信息，改成结构化字段（例如 `phase`），由页面按语言呈现。

**验收方式**

- `?lang=en` 返回的 Markdown 不含中文标题，事实字段与中文版逐项相同；不带参数的答复与现在逐字节相同。
- 待办里 `planning_attention` 等行的阶段信息能在两种界面语言下读到。

## 4　演示样例缺少五组新接口

**位置**

- 录制脚本 `tests/unit/web_support/record_samples.py` 和场景 `tests/unit/web_support/sample_scenarios.py`。
- 已入库的样例 `docs/web-api/samples/`（`accept`、`rework`、`dual-partial`、`cancel`），都录制于新接口出现之前。

**复现步骤**

1. 在 `frontend/` 运行 `corepack pnpm mock`，打开 `/?mock=rework`。
2. 依次打开首页、`/jobs/100003e0000000000000000000000000`、`/reviews/10000360000000000000000000000000`、`/integrations/10000390000000000000000000000000`、`/new`。

**期望与实际**

- 期望：演示能展示首页待办、作业活动、审核意见的处理记录、返工资格、交付摘要和接入检查。
- 实际：样例里没有这些读取，页面按「接口不存在」降级：五个新面板都不出现，审核页退回只读的意见列表。
- 另外，集成页的返工按钮现在要先读 `rework-eligibility`。把演示回放到返工之前的步骤（`?mock=rework&at=<序号>`）时，这个按钮不再出现。

**建议改法**

在四个场景的录制步骤里补上新读取，然后重录：

- `GET /api/attention`：每个场景在关键节点各读一次（生成计划后、集成待验收时、验收后）。
- `GET /api/jobs/{id}/activity`：`dual-partial` 里失败席位的那个审核作业，以及 `cancel` 里被中断的作业。
- `GET /api/reviews/{id}/findings`：`rework` 场景的审核轮；可再提交一次 `POST /api/reviews/{id}/decisions` 和一次 `review.followup`，让处理记录不为空。
- `GET /api/integrations/{id}/rework-eligibility`：`rework` 场景返工之前和之后各一次。
- `GET /api/integrations/{id}/delivery`：`accept` 场景验收之后。
- `GET /api/workspace/onboarding`：每个场景开头一次。

重录后请告知前端：演示用的回放层 `frontend/src/mock/transport.ts` 目前只回放 `/api/jobs` 的 POST，`decisions` 的 POST 需要前端补一处回放。

**验收方式**

- `uv run python tests/unit/web_support/record_samples.py --output <临时目录>` 的结果与入库样例一致（按现有的归一化比较）。
- `corepack pnpm mock` 下，上面第 2 步的五个页面都能看到对应的新面板，浏览器控制台没有报错。
- `frontend/e2e/workbench.spec.ts` 里「离线演示模式」用例的断言需要随之从「面板隐藏」改为「面板出现」，这一处由前端配合修改。

## 真实模型联调记录

2026-11-25 在一个临时 Git 仓库上，通过页面用真实模型走了一遍主路径。仓库只有 `calc.py`、`tests/test_calc.py` 和 `.multi-agent/rules.yaml`，规则里登记了一条命令 `unit: python3 -m unittest discover -s tests -t .`。目标是给 `calc.py` 增加 `subtract(a, b)` 和一条测试。

- CLI：codex-cli 0.169.0，Claude Code 2.1.325。模型和推理等级都用页面预设的默认值。
- 一共 20 个作业，其中 11 个调用了模型，合计 17 次模型会话（一次在硬停时被中断）。同一时间只有一个作业在跑，没有遇到额度或限流提示。
- 截图和每一步的记录在 `frontend/e2e/records/real-trial/`（被 Git 忽略）：`evidence.json` 和 `shots/` 下的 43 张图。

| 步骤 | 作业 | 退出码 | 用时 | 请求的模型 · 等级 | 运行时报告的模型 · 等级 |
| --- | --- | --- | --- | --- | --- |
| 检查 Codex 登录 | `environment.refresh` `82347821` | 无 | 1.2 秒 | 不用模型 | — |
| 检查 Claude 登录 | `environment.refresh` `0f7c4f01` | 无 | 0.2 秒 | 不用模型 | — |
| 咨询（一位顾问） | `project.consult` `5beb93e1` | 0 | 17.5 秒 | gpt-6.2-astra · high | 未确认 · 未确认 |
| 确认目标 | `project.confirm` `761aa9a3` | 0 | 0.2 秒 | 不用模型 | — |
| 生成项目计划 | `project.plan` `9293b852` | 0 | 23.0 秒 | gpt-6.2-astra · high | 未确认 · 未确认 |
| 领取阶段（直接模式） | `project.stage.start` `684fffa3` | 0 | 100.1 秒 | 拟卡 gpt-6.2-sol · high；卡片复核 claude-opus-5-5 · high；实施 gpt-6.2-sol · high | 拟卡和实施未确认；卡片复核 claude-opus-5-5 · 未确认 |
| 运行登记的测试 | `task.test` `e563e579` | 1 | 28.5 秒 | 不用模型 | — |
| 双席审核，一席完成后硬停服务 | `task.review` `b1ac47dc` | 无（已中断） | — | gpt-6.2-astra · high；gpt-6.2-sol · high | 未确认 · 未确认 |
| 只补跑缺失的席位 | `task.retry_review` `c69e60d3` | 1 | 35.1 秒 | gpt-6.2-sol · high | 未确认 · 未确认 |
| 追问原审核者 | `review.followup` `ae493d0e` | 0 | 9.3 秒 | 席位原模型 gpt-6.2-sol | 未确认 · 未确认 |
| 按阻塞意见返工 | `task.rework` `8b789c25` | 1 | 34.8 秒 | gpt-6.2-sol · high | 未确认 · 未确认 |
| 修订计划 | `project.revise` `60697051` | 0 | 0.1 秒 | 不用模型 | — |
| 重新领取阶段 | `project.stage.start` `16b631bc` | 0 | 100.0 秒 | 同第一次领取 | 同第一次领取 |
| 运行登记的测试 | `task.test` `79b1a3b8` | 0 | 32.6 秒 | 不用模型 | — |
| 单席审核 | `task.review` `fd5dd458` | 0 | 38.4 秒 | gpt-6.2-astra · high | 未确认 · 未确认 |
| 创建集成 | `integration.create` `9d430524` | 0 | 2.7 秒 | 不用模型 | — |
| 集成测试 | `integration.test` `4784f299` | 0 | 7.3 秒 | 不用模型 | — |
| 集成审核（单席） | `integration.review` `2597911e` | 0 | 21.6 秒 | gpt-6.2-astra · high | 未确认 · 未确认 |
| 接受集成 | `integration.accept` `b991041b` | 0 | 0.8 秒 | 不用模型 | — |
| 单卡规划，默认路径 | `plan.create` `fd67750d` | 0 | 44.3 秒 | 草拟 claude-opus-5-5 · high；复核 gpt-6.1-sol · high | 草拟 claude-opus-5-5 · 未确认；复核未确认 |

走通的部分：接入检查、咨询、确认、规划、领取、作业活动、硬停后只补跑缺失的席位（同一审核轮、同一份冻结快照，页面发出的参数与服务给出的恢复动作逐字段相同）、在真实意见上记录判断、追问得到回答、集成、验收、交付摘要（预览和剪贴板里的文本都与接口返回的 Markdown 逐字符相同）。

第一次领取的阶段没有走到集成。原因见第 5 条和第 6 条：测试命令在沙箱里找不到可用的 `python3`，之后页面上只剩返工一条路，返工的实施者如实报告改不了，这个任务就停住了。修订计划后重新领取了一次阶段，才走完后半段。

## 5　测试沙箱里的 `python3` 是 Xcode 占位程序，接入检查看不出来

**位置**

- `src/multi_agent/adapters/test_isolation.py:34` 和第 127 行：测试命令的 `PATH` 是 `--tool-directory` 给的目录加上 `/usr/bin:/bin:/usr/sbin:/sbin`。
- `src/multi_agent/web/onboarding.py:16-21`：规则模板建议的命令是 `python3 -m unittest …`。
- `src/multi_agent/web/onboarding.py:31-48`：接入检查只确认规则文件能解析、登记了命令。

**复现步骤**

1. 在一台没有安装 Xcode 命令行工具的 Mac 上（`/usr/bin/python3` 是占位程序），按模板登记 `python3` 命令。
2. 不带 `--tool-directory` 启动服务，领取一个阶段，在任务页运行测试。

**期望与实际**

- 期望：接入检查在第一次提交目标之前就指出「这条命令在测试沙箱里跑不起来」。
- 实际：接入检查显示「已登记测试命令」，一切就绪。三次模型调用（拟卡、复核、实施）之后，测试作业 `e563e579518f40eea2211c77855f0e93` 退出码 1，标准输出为空，标准错误是 `xcode-select: error: No developer tools were found…`。
- 用 `--tool-directory /opt/homebrew/bin --runtime-read-path /opt/homebrew` 重启服务后，同样的命令通过（作业 `79b1a3b8710343fba1cf0708cc36ab03`）。

**建议改法**

- `GET /api/workspace/onboarding` 对每条登记的命令，用测试沙箱的 `PATH` 解析可执行文件，在 `rules.commands[]` 里加上解析到的路径和一个状态（找到、找不到、是 `/usr/bin` 下的 Xcode 占位程序）。页面会在命令旁边显示它，并在 `next_steps` 里给一步「补上工具目录」。
- 模板的注释里写明：macOS 上需要用 `--tool-directory` 指到装有解释器的目录。

**验收方式**

- 上面的环境里，接入检查对 `python3` 给出警告状态；带上 `--tool-directory` 后警告消失。
- 补一条后端测试：可执行文件在沙箱 `PATH` 里找不到时，`rules` 的状态不是 `ready`。

## 6　测试因为环境失败后，同一次实施没有办法重测

**位置**

- `src/multi_agent/cli/task_cli.py:488-501`：当前实施只要有过一次测试，不论 `succeeded` 还是 `failed`，下一步就是 `task_review`；`task_test` 只在还没有测试时给出（第 503-507 行）。
- `src/multi_agent/cli/task_cli.py:468-486`：审核结论是 `needs_rework` 且还没返工过，下一步是 `rework`。
- `src/multi_agent/cli/task_cli.py:508-515`：返工没有交付候选时，阶段变成 `planning_required`，下一步是 `user_decision`。

**复现步骤**

1. 按第 5 条的环境走到测试失败。
2. 任务页此时只提供「开始审核」。提交双席审核。
3. 两席都报告 P1（测试命令退出码 1），结论 `needs_rework`，原因 `blocking_findings_present` 和 `candidate_tests_failed`。任务页提供「按阻塞意见返工」。
4. 修好环境（带工具目录重启服务），点「按阻塞意见返工」。

**期望与实际**

- 期望：环境修好以后，能对同一个候选再跑一次登记的测试。
- 实际：返工作业 `8b789c25e5534aec81dd523a078bc5df` 退出码 1。实施者报告 `scope_blocked`，需要的路径是 `.multi-agent/rules.yaml`，说明是「候选已经包含要求的函数和测试，可写范围内的两个文件解决不了运行环境的问题」。任务随后是 `planning_required`、候选为空、`rework_used` 为真、下一步 `user_decision`，页面上没有任何可执行的动作。
- 到这一步已经用掉 3 次实施相关的模型调用、3 次审核调用和 1 次返工调用，候选本身是对的（在同一个 `PATH` 下手工运行，2 条测试通过）。
- 唯一的出路是修订计划、重新领取阶段，再花 3 次模型调用。
- 没有验证的一点：后端是否接受对同一次实施再提交一次 `task.test`。页面只按 `next_action` 提供按钮，没有试。

**建议改法**

- 当前测试是 `failed` 时，`next_action` 同时允许重测：要么给出 `task_test`（带新的 `test_attempt`），要么在 `task_review` 的答复里加一个字段说明可以重测，页面据此显示「重新测试」。集成页已经有「重新测试」，任务页可以用同样的规则。
- 返工以 `scope_blocked` 结束时，不要把一次返工额度算作已用，或者至少让候选回到返工之前的那一个，而不是清空。

**验收方式**

- 离线环境里让测试命令第一次失败、第二次通过：任务页能重测，重测通过后下一步是 `task_review`，审核看到的是通过的那次测试。
- 返工以 `scope_blocked` 结束后，任务仍然指向返工之前的候选，页面有可执行的下一步。

## 7　阶段任务在等测试、等审核、等创建集成时都不进待办

**位置**

- `src/multi_agent/web/attention.py:209-227`：绑定了任务的阶段，只在实施是 `failed`、`cancelled`、`planning_required`，或者当前测试是 `failed`、`cancelled` 时产出一行。
- `src/multi_agent/web/attention.py:340-386`：任务审核只在未完成、或者结论是 `needs_rework`、`user_decision` 时产出一行。
- 集成有 `test_integration`、`review_integration`、`accept_integration` 三种原因（第 388-431 行），任务没有对应的原因。

**复现步骤**

1. 领取一个直接模式的阶段，等作业结束（候选已核验，下一步 `task_test`）。读 `GET /api/attention`。
2. 运行测试并通过（下一步 `task_review`）。再读一次。
3. 单席审核通过（阶段页出现「创建集成」）。再读一次。

**期望与实际**

- 期望：三个时点各有一行，分别指向任务页的「运行测试」「开始审核」和阶段页的「创建集成」。项目书对首页的要求是「用户离开再回来，应能看到哪一步可以继续」。
- 实际：第 3 个时点在联调里留了记录，`attention` 里没有这个阶段的任何一行（`complete: true`），而阶段页上「创建集成」可以点。前两个时点是读代码得出的，联调里没有截图。
- 结果是领取阶段之后，用户要自己记得回到任务页和阶段页，首页不再提示，直到创建了集成。

**建议改法**

给任务补三种原因，页面已经会对未知原因显示 `detail`，补上文案即可：

- `test_task`：候选已核验、当前实施还没有测试，`href` 指向任务页。
- `review_task`：当前测试完整且已核验、还没有当前审核，`href` 指向任务页。
- `integrate_stage`：直接模式的阶段审核已满足、还没有包含这个任务的集成，`href` 指向阶段页。

**验收方式**

- 上面三个时点各读到一行，`href` 打开的页面上对应的按钮可用。
- 有作业在运行时这些行不出现在 `complete: false` 的答复里；创建集成后 `integrate_stage` 消失。

## 8　被新版本计划取代的任务一直留在待办里

**位置**

- `src/multi_agent/web/attention.py:160-184`：查询里只对「分派被拒绝且计划已有修订」的阶段做了排除（第 182-184 行），没有排除其他属于旧版本计划的阶段任务。

**复现步骤**

1. 让一个阶段任务停在 `planning_required`（例如第 6 条的结局）。
2. 在计划页修订计划，生成新版本，在新版本里重新领取同一个阶段，走到集成被接受。
3. 读 `GET /api/attention`。

**期望与实际**

- 期望：旧版本的那个任务不再出现，或者有办法把它关掉。
- 实际：新版本的阶段已经交付并验收，待办里仍然有旧任务 `5d1d9d26d68e432292ab07be83436ca2` 的一行 `implementation_attention`，`detail` 是 `Implementation is planning_required. Inspect the attempt and available next action.`。打开任务页，没有任何可执行的动作，这一行永远消不掉。

**建议改法**

- 阶段所属的计划有更新的修订版本，并且新版本里同一个 `stage_id` 已经有阶段运行时，不再为旧版本的这个阶段产出待办。
- 或者提供一个「放弃这个任务」的动作，记录一条用户决定，待办据此不再产出。

**验收方式**

- 上面的复现步骤之后，`GET /api/attention` 的 `items` 为空。
- 没有修订、或者新版本还没领取这个阶段时，这一行照常出现。

## 9　Codex 的角色没有运行时报告的模型和等级

**位置**

- `src/multi_agent/adapters/runtime_decode.py:376-454`：只有 Claude 的事件里读取 `reported_model`。
- Codex 的会话日志里，`thread/start` 的答复带 `model` 字段，`turn/start` 的请求带 `model` 和 `effort`。

**复现步骤**

1. 用真实 Codex 跑任意一个角色（顾问、规划者、拟卡、实施、审核、追问、返工）。
2. 读对应记录里的 `reported_model` 和 `reported_effort`。

**期望与实际**

- 期望：页面能区分「请求的模型」和「实际运行的模型」。
- 实际：这次联调里 Codex 启动了 14 次会话（13 次有完成通知，1 次在硬停时中断），`reported_model` 和 `reported_effort` 全部为空，页面都显示「未确认」。Claude 启动了 3 次会话，都报告了模型 `claude-opus-5-5`，等级为空。
- 这一条只是如实记录。`thread/start` 答复里的 `model` 是会话生效的模型还是请求值的回显，需要后端判断；如果是前者，可以记为报告值并注明来源。

**建议改法**

- 确认 `thread/start` 答复里 `model` 的含义。可以当作运行时证据的话，记到 `reported_model`，`reported_model_source` 写 `thread/start`。
- 不能当作证据的话，在文档里写明「Codex 不报告模型，页面显示未确认是预期行为」，页面可以把提示改得更具体。

**验收方式**

- 真实 Codex 的一次会话之后，记录里的 `reported_model` 有值且带来源，或者文档里有上面那句说明。

## 联调里顺带看到的小事

- 恢复作业 `task.retry_review`（`c69e60d3d3b7465aad3c1b4d25245678`）补跑的席位成功、审核轮完整，作业退出码却是 1，因为审核结论是 `needs_rework`。作业页上它显示为「已结束 · 退出码 1」。如果退出码表示的是结论而不是作业成败，建议在作业的答复里给一个区分两者的字段。
- 追问的回答说「冻结资料未提供 `calc.py` 的实现」。追问用的是同一份冻结资料，审核页的冻结差异里有这个文件，值得确认追问的上下文是否带上了差异。
- 待办和任务页里 `next_action.detail` 的英文句子在中文界面里原样出现，例如 `Inspect retained evidence; do not replay an old prepared runtime or adopt a partial candidate.`。归到第 3 条一起处理。
