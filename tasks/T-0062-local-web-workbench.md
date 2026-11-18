# T-0062 · 本地网页工作台开发任务书

**交付给 Claude Code。** 在 Mac 浏览器里完成一次真实的工作台操作：输入需求，选择规划模型与档位，派发实现，查看候选 diff，执行测试，选择单审或双审，跟踪一次返工，最后由用户显式接受指定集成版本。用户刷新页面或重启本地服务后，仍能找回已保存的结果。

用户已于 2026-11-18 确认阶段 3 收尾。产品基线为 `d27e69f`，本任务的交接基线为 `bdc1f7f`。项目书规定首版是本机单用户、单仓库网页，现有 Python CLI 和 SQLite 已负责规划、派发、证据和接受。仓库在此基线上尚无网页、HTTP 服务或前端包。下面的 **HTTP 接口是本次要实现的契约**，**CLI 映射是已经存在的能力**。桌面打包、云端、可视化规则编辑、原生 subagent 逐步进度均留在后续。[项目书](../PROJECT_PLAN.md) · [已验收功能与用法](../docs/phase-three-development.md)

仓库 `AGENTS.md` 的 Astra/Sol 分工对应已结束的阶段 3。本次网页实现由用户交给 Claude Code，仓库既有证据、权限和提交检查约定继续生效。

## 1. 用户界面与开发顺序

| 先后 | 用户能完成的操作 | 界面显示的依据 |
| --- | --- | --- |
| 1 | 打开项目，从历史记录或已知 ID 进入一次集成，点开文件、测试或审核意见 | 顶部显示仓库、目标分支、基线及候选短 SHA，详情可复制完整 SHA。任务详情显示原始需求、卡、worktree 和实际改动。审核意见可跳到冻结版本的文件、侧别和行号。先用文末 T61 样例走通这一条。 |
| 2 | 提交需求，分别配置 Claude 草拟和 GPT 复核，处理待决问题，运行一个计划或并行运行多个计划 | 显示请求模型及档位、派发所用规则和选择原因、尝试与实际报告值。批次中的成员分别展示结果；其中一个失败时仍能打开成功成员的候选。 |
| 3 | 选择任务候选及本地目标分支，按依赖集成，运行完整测试，发起单审或双审 | 显示依赖顺序、冲突任务和路径。测试展示注册命令、顺序、是否实际发送、退出码、输出及证据来源。双审先显示每席状态，报告齐全后展示汇总。 |
| 4 | 根据审核结论查看自动返工，或选择交叉确认、失败席补跑、显式返工、重建与接受 | 保留旧轮和旧候选的直达入口。对修订候选显示父 SHA、实际改动、重新执行的整套测试和复审。接受前展示目标分支、基线、候选和审核轮次，由用户点击后签发回执。 |

主视图以需求、任务卡、阶段和模型配置为中心。左侧历史列表可进入任一任务或集成；右侧详情切换「改动」「测试」「审核」「尝试记录」。保留现有单任务验证和独立代码审核入口。首次启动无历史时，直接给出输入需求和打开已有运行的入口。

## 2. 进程边界与启动约定

- **新增入口**：`uv run python -m multi_agent.web --repository /abs/project --data-dir /abs/runs`。启动时选择目标仓库并打印回环访问地址，同源提供页面与 `/api`。前端技术栈由 Claude Code 选择，在 `frontend/README.md` 给出安装、构建、启动和验收命令。
- 本地 Python 桥绑定一个仓库及一个仓库外的数据目录 `D`。新工作台统一使用 `DB=D/reviews.sqlite3`，从而兼容独立审核 CLI 的固定库名。桥创建权限为 `0700` 的规范绝对路径目录，避免符号链接别名。输入文本由桥写入私有文件；每次操作按 CLI 约定分配与 DB 同父、尚不存在的运行目录。项目自己的 `.multi-agent/rules.yaml` 从选定提交冻结。
- 启动配置支持受信的 `--codex-bin`、`--claude-bin`，以及可重复的 `--tool-directory`、`--runtime-read-path`。桥把程序选项传给支持它的**首次**产品操作，把后两项传给 `task-test run` 和 `integration test`；未配置时沿用产品默认值。这样非系统目录的 uv Python 可按已批准的目录在沙箱执行，离线验收可指定假模型 CLI。重试与返工沿用原冻结配置。浏览器动作不接受这些路径。
- 所有产品变更走下表的工作台命令。桥使用自己的 Python 解释器，以 `[sys.executable,"-m","multi_agent",...]` 参数数组启动。浏览器提交业务字段；仓库、DB、运行目录和可执行文件由桥解析。仅监听 loopback，同源页面发起写操作。日志与 diff 接口按已登记资源定位文件。
- 既有数据支持显式只读打开，启动参数约定为 `--read-only-database /abs/archive/workflow.sqlite3`，配合 `--repository` 选择该库绑定的仓库；此模式下 DB 使用指定文件，桥的索引与临时文件另存于自己的 `--data-dir`。通过现有只读 store 和 `load_review_input` 读证据，关闭变更入口。不得把该文件的父目录传给 `--data-dir` 后误开另一个库。封存样例只读使用。

## 3. 浏览器与本地桥的 HTTP 契约（待实现）

JSON 字段统一使用 snake_case。产品 ID 是不透明字符串；`task_id`（卡号，如 T-0200）、`task_run_id`、`planning_run_id`、`integration_id`、`round_id`、`snapshot_id` 和桥的 `job_id` 各有用途。尝试号为正整数。单个工作台进程可以先用轮询完成此契约。

| 请求 | 请求字段或查询参数 | 成功结果 |
| --- | --- | --- |
| `GET /api/workspace` | 无 | `{repository, target_branch, head, read_only, presets, environments}`。没有当前分支或环境读数时对应值为 null。项目规则与模型预设应标注来源。 |
| `GET /api/runs` | 可选 `kind` | `{items:[{kind,id,label,parent_id,updated_at}]}`，首版至少覆盖本桥登记的计划、任务、集成和独立审核轮。批量运行登记每个任务，自动返工登记新集成及父子轮。桥持久化导航索引；用户也可按类型和已知 ID 打开旧库记录。 |
| `POST /api/jobs` | `{action,args}`，action 白名单见第 4 节 | `202 {job_id,state}`。包括快速操作在内统一返回作业；浏览器按 job_id 取结果。 |
| `GET /api/jobs/{job_id}` | 无 | `{job_id,action,state,exit_code,result,error,resources}`。state 为 `queued/running/cancelling/finished/interrupted`；运行中 exit_code、result 可为 null。result 保留该命令的业务 JSON 结构；resources 为从结果取得的 `[{kind,id}]`。 |
| `POST /api/jobs/{job_id}/cancel` | 空对象 | 对桥仍持有的控制进程请求一次 SIGINT，返回 `202 {job_id,state:"cancelling"}`。随后观察真实退出及领域尝试状态；已结束或无可控进程回 409。页面刷新只恢复观察。 |
| `GET /api/resources/{kind}/{id}` | kind 为 `plan/task/task-test/task-review/integration/review`；可选 `history=1`。task-test 必传 `implementation_attempt`，可选 `test_attempt` | `{kind,id,data,artifacts}`；data 取该类 show 的 JSON，artifacts 为可预览日志的句柄。只有支持 history 的命令传 `--history`。 |
| `GET /api/diffs/{kind}/{id}` | kind 为 `task/integration/snapshot`；task 首版取当前已核验候选，历史审核用 snapshot | `{baseline,candidate,snapshot_id,diff,test_change_paths}`。已审核的改动按 snapshot 读；尚无审核快照的任务/集成，通过核验后绑定的基线与候选读取 Git diff。尚未冻结则返回不可用原因。 |
| `GET /api/snapshots/{id}/file` | `side=baseline\|candidate&path=仓库相对路径` | 在该快照已记录的文件中按 side/path 查找，返回 `{side,path,oid,mode,text}`。二进制或缺少文本时显示不可预览；粘贴 diff 可直接在 patch 中定位。 |
| `GET /api/artifacts/{artifact_id}` | 可选字节分页 `offset,limit` | `{stream,text,offset,next_offset,byte_count,sha256,truncated}`。只读与该作业/测试/审核关联的已登记日志，按需解码预览；摘要对应完整原始字节。业务结果中的本机路径是展示信息。 |

**作业与错误。** 页面正在等待时，轮询 job；已有产品 ID 才轮询对应 show。锁忙时保留最近一次成功读取和时间，再刷新。工作台 CLI 通常在操作结尾输出一份 JSON；首次启动且尚无领域 ID 时显示「运行中，等待可读结果」。有实际历史事件才展示消息、用量或进程终态。桥重启后恢复已登记导航和回执；原来运行中的作业若失去控制，可标为 `interrupted` 并查询产品状态，重派由用户沿既有入口触发。

错误体统一为 `{error:{code,message}}`。HTTP 400 表示动作或输入字段不合法，404 表示资源不存在，409 表示当前状态不允许操作。作业一旦启动，产品拒绝、失败或取消在 job 结果中保留 exit_code、结构化结果和 stderr 摘要。退出 1 也可能有可用的任务、冲突或审核结果；退出 2 或 130 可能无 JSON。独立审核发现 P0 仍可能退出 0。界面从领域字段判断业务结果。

## 4. 动作、参数与已有 CLI 的对应关系

各行只列子命令。以下缩写用于文档：`B` 展开为 `--database DB`，`R` 为 `--repository REPO`，`N` 为 `--directory NEW`，`D0` 为放在顶层子命令**之前**的 `--data-dir D`。大写 ID、MODEL 等表示一个 argv 元素；可重复选项逐个追加。下表中 ID 均从对应领域结果取得；参数同名字段使用 snake_case。

| action | 浏览器 args | 本地桥执行 |
| --- | --- | --- |
| `environment.refresh` | `provider: codex\|claude` | 用既有 `RuntimeAdapter.check_environment()` 做显式、无模型回合的环境检查；这是已有 Python 方法。完成后更新 workspace 中的观测时间及结果。 |
| `plan.create` | `baseline,requirements`；可选 `draft,plan_review,declarations` | `task plan B R N --baseline SHA --requirements FILE`；角色覆盖为 `--draft MODEL EFFORT`、`--plan-review MODEL EFFORT`。声明文件使用 `--declarations FILE`。 |
| `plan.retry` | `planning_run_id` | `task plan B --retry PLAN_ID`，复用原冻结输入。用户新回答改变输入时，带 resolutions 新建规划。 |
| `task.run` / `task.retry` | `planning_run_id`；首次可传 `implementation,fallbacks,task_type` | `task run PLAN_ID B R N`；覆写为 `--implementation PROVIDER MODEL EFFORT`、可重复 `--fallback PROVIDER MODEL EFFORT`、`--task-type NAME`。实施重试追加 `--retry` 并保留原策略。 |
| `task.run_batch` | `planning_run_ids[]`，同首次实施的可选策略 | `task run-batch PLAN1 PLAN2 … B R N`。至少两个同仓同基线、已审核且可并行的计划，N 在此处是目录前缀。 |
| `task.test` | `task_run_id,implementation_attempt,commands[],declaration_source` | `task-test run TASK_ID B R N --implementation-attempt I --command NAME … --declaration-source TEXT`。commands 仅接受冻结规则中注册的名称。 |
| `task.review` | `task_run_id,implementation_attempt,test_attempt,seats[],auto_rework?` | `task-review run TASK_ID B R N --implementation-attempt I --test-attempt T --seat PROVIDER MODEL EFFORT …`，可加 `--no-auto-rework`。 |
| `task.retry_review` / `task.confirm` / `task.rework` | `round_id`；retry 可传 `seat_keys[]` | `task-review retry ROUND B [--seat KEY …]`、`task-review confirm ROUND B`、`task rework ROUND B N`。单独 rework 只生成修订候选，默认自动流程会继续复测复审。 |
| `integration.create` | `task_run_ids[],target` | `integration create TASK_RUN_ID … B R --target LOCAL_BRANCH`。使用实施运行 ID，CLI 根据已核验成员决定依赖顺序。 |
| `integration.test` | `integration_id` | `integration test ID B R N`，按目标冻结基线运行**全部**已注册命令。 |
| `integration.review` | `integration_id,seats[],auto_rework?,test_attempt?` | `integration review ID B R N --seat PROVIDER MODEL EFFORT …`，可加 `--test-attempt T` 或 `--no-auto-rework`。默认按资格自动返工一次。 |
| `integration.retry_review` / `integration.confirm` / `integration.rework` | `integration_id,round_id`；retry 可传 `seat_keys[]` | `integration retry-review ID B R --review ROUND [--seat KEY …]`、`integration confirm ID B R --review PARENT_ROUND`、`integration rework ID B R --review PARENT_ROUND`。确认轮失败时用其子轮 ID 补跑，最后回读父轮。 |
| `integration.rebase` / `integration.accept` | `integration_id`；accept 另需 `round_id` | `integration rebase ID B R` 产生新 ID；`integration accept ID B R --review ROUND` 为当前候选签发回执。接收操作保持用户目标分支原位。 |
| `review.freeze` | `requirements,acceptance[]`，另选 `baseline+candidate` 或 `diff`；仓库模式可选 `tests` | `D0 freeze --repo REPO --baseline BASE --candidate HEAD --requirements FILE --acceptance TEXT … [--tests FILE]`；粘贴模式换成 `--diff PATCH_FILE`。上传测试证据格式见下文。 |
| `review.run` / `review.retry` / `review.confirm` | run 为 `snapshot_id,seats[]`；其余为 `round_id`，retry 可选 `seat_keys[]` | `D0 run SNAPSHOT --seat PROVIDER MODEL EFFORT …`、`D0 retry ROUND [--seat KEY …]`、`D0 confirm PARENT_ROUND`。这是独立审核入口，可查看外部提交或粘贴的补丁。 |

读回映射：`task show B --plan PLAN`、`task show B --task TASK`、`task-test show TASK B --implementation-attempt I [--test-attempt T]`、`task-review show ROUND B`、`integration show ID B`、`D0 show ROUND`，以及 `D0 snapshot SNAPSHOT`。独立 `D0 show` 无 ID 时列审核轮次；跨计划、任务和集成的列表由浏览器桥提供。

角色输入中 `draft/plan_review={model,effort}`，`implementation/fallbacks[]/seats[]={provider,model,effort}`（implementation 是单个对象）。实施 provider 为 `claude-code|codex`，审核为 `claude|codex`。单审传一席；双审传两席、两个不同模型 ID。第一次提交规划可附 `declarations={dependencies:[{task_id,baseline_commit,source,detail}],decisions:[{question,resolution,source,detail}]}`，用户声明的 source 为 `user_declared`。独立 freeze 的 tests 为 `{required_commands:[argv],results:[{commit,command,exit_code,log}]}`，界面标记「用户提供」。[规划与实现](../docs/task-workflow.md) · [独立审核](../docs/review-cli.md)

命令中的 `TASK_ID` 均取实施运行的 `task_run_id`；卡号只用于展示、依赖声明和归属定位。草拟默认 `claude-opus-5-5/high`、计划复核默认 `gpt-6.1-sol/high`，均可显式覆盖；实施默认项由目标仓库冻结的规则决定。规则指定 `frontend/**` 由 Claude 写入时，配置页应解释来源，返工沿用其约束，审核席仍可选择 GPT。[规划与实施解析器](../src/multi_agent/cli/task_cli.py) · [单任务测试](../src/multi_agent/cli/task_test_cli.py) · [单任务审核](../src/multi_agent/cli/task_review_cli.py) · [集成操作](../src/multi_agent/cli/integration_verification.py) · [独立审核及快照](../src/multi_agent/cli/main.py)

## 5. 字段与交互判定

| 界面问题 | 使用的真实字段与判定 |
| --- | --- |
| 任务能否派发、改了什么？ | plan 取 `phase,verification,next_action,card,questions`。task 取 `details.input.{requirements,card,frozen_rules}`、`attempts[].request.worktree`、`implementation_attempt,candidate,tests,reviews`。task 的 candidate 是 SHA 字符串；integration 的 candidate 是 `{commit,tree,ref}` 或 null。批次响应为 `tasks[]`。 |
| 用户选了哪个模型，实际跑了哪个？ | 环境投影 `{cli_version,logged_in,models,model_efforts}`，支持 null。Codex 选择器用当前可见模型及其支持档位；Claude 目录缺失时用标明来源的项目预设或手填。项目书的角色建议是预设；登录状态、目录查询、模型连通性分开显示。审核席显示 `requested_model/requested_effort`、当前尝试的 `reported_model/reported_effort` 及来源，缺值显示「未确认」。实施备选记录与实际改派事件分别显示，用量从可获得的 usage 事件读取。更换审核模型需发起新轮。 |
| 哪些测试真实运行过？ | 集成 show 的 `test.required_commands,unexecuted_commands,unconfirmed_commands,result.commands,evidence_hash`、`test_evidence_verification`、`test_source_errors`。显示每个 argv、退出码和 stdout/stderr 入口，区分尚未发送与已发未确认。集成成功快照的 `input.commands[].stdout.content.hex` 与 stderr 同形，桥按字节解码；超出预览长度时显示完整长度及摘要。阶段 1 用户上传的测试另列来源。 |
| 如何定位一条意见？ | 使用 `round_id → snapshot_id → side/path/line_start/line_end`；行号从 1 开始。普通快照读取 `input.diff/files`，task/integration 执行式审核读取 `input.source.diff/files`；文件含 `side,path,text,oid,mode`。意见在 `assessment.groups[].members[].finding` 中，保留 severity、quote、impact、reference_status 和来源席；删除行定位 baseline，引用待核实时照实标记。集成的 `input.members[].task_input.card` 和 `input.test_change_paths` 用来显示成员约定及突出测试修改。 |
| 是否可以接受？ | 区分集成 `status=ready`（候选就绪）、审核 `assessment.status=ready/needs_rework/user_decision/incomplete`、集成顶层 `current_eligible` 与已签发的 `acceptance`。只在新鲜 show 给出当前资格且无回执时开放接受，提交该集成 ID 和对应当前 round_id；以 CLI 的原子复核为最终结果。接受后显示回执，即便此时 current_eligible 为 false。 |
| 冲突、修订、过期如何继续？ | 冲突展示 `conflict.task_id/task_run_id/paths` 和 `applied`；未完成双审允许补跑失败席，保留成功报告。初审有阻塞时展示依据和责任成员；现有宿主组装返工交接包。自动路径可能一次返回 `review,rework,revision,retest,rereview`，从 `revision.integration_id` 打开修订版，用 `superseded_by/rebased_from` 导航历史。`target_is_current=false` 提供重建，新 ID 需完整重测复审；旧收据留在原候选。 |

**示例。** 用户点「接受」时，页面可提交以下请求（ID 来自隔离样例）。桥使用已绑定仓库和数据库组装 argv；收到作业结果后，重新读取该集成的 `acceptance`。

```json
{"action":"integration.accept","args":{"integration_id":"73942cf24cfa48a4b3760d22d57dd5c6","round_id":"7f3b9e044a11419c94dba3ec0964aa92"}}
```

## 6. 验收与交付

1. **首屏到接受。** 使用空白的工作台数据目录，以及已提交初始代码和规则文件的临时 Git 仓库，通过页面走完「规划 → 已核验成员 → 集成 → 实际执行注册测试 → 审核 → 显式接受」，刷新后可回到同一页、同一回执。日常验收使用已有假模型 CLI、真实 Git 和 SQLite。页面首次打开、刷新及查看历史均不启动模型会话。
2. **定位与返工。** 用 T61 脱敏样例或同形的本地回归，能从原轮 P0 跳到 `frontend/status.js:1` 的 `draft`，再到修订版的 `ready`，同时看到两套测试、两个轮次、相同审核配置及只有一轮返工。旧报告不能接受新 ID。[实机原件，仅本机可用](../../Multi-agent-acceptance/phase-three/T61-native-d27e69fbb2f4/result.json) · [可移植回归](../tests/unit/test_integration_rework_cli.py)
3. **双审、拒绝和恢复。** 从双席部分失败只补跑失败席；目标推进后从新 ID 重测复审。测试不通过、未核验的引用、没有真实档位、用户提供的测试及历史回执各显示其真实状态。取消操作能区分已请求和已确认；不能控制的历史作业仍可查看。[CLI 回归](../tests/unit/test_integration_verification_cli.py)
4. **交付凭据。** 提供一条启动命令、必要的前端构建/测试命令、一套可复现的离线样例和至少一条真实浏览器交互记录。运行现有 `scripts/verify.sh` 及新增前端的检查，列出结果、修改路径和已知限制。使用小型、可分发的 fixture；从本机原件取例时去除绝对路径、会话标识和无关日志。

### 实现方领取卡

下面是同一任务书的机器可读卡。Claude Code 可将此块原样保存为 `tasks/T-0062-local-web-workbench.json`，按仓库 task-card skill 做 `--dispatchable` 校验后开工。写入范围涵盖网页、薄桥和对应测试，既有 CLI 协议及编排语义继续作为接口依据。

```json
{
  "schema_version": 1,
  "id": "T-0062",
  "goal": "以本机网页操作现有多模型工作流，从需求与任务卡走到可追溯的集成接受回执。",
  "baseline_commit": "bdc1f7f50ce5c5c1aa7c124b5ef45d7a15be7559",
  "write_scope": ["frontend/**", "src/multi_agent/web/**", "tests/unit/test_web_*.py", "tasks/T-0062-local-web-workbench.json"],
  "depends_on": ["T-0061"],
  "interface_contracts": [
    {"name":"浏览器与本地桥","definition":"按本任务书第 2、3 节提供单仓库回环服务、异步作业、只读结果、冻结 diff 及证据预览。"},
    {"name":"工作台命令与证据","definition":"使用第 4、5 节的领域命令、ID、模式及状态；复用原执行、审核、返工与接受逻辑。"}
  ],
  "deliverables": ["可启动的本地网页与桥接服务", "frontend/README.md 内的启动和验证方法", "可复现的离线交互与验收记录"],
  "acceptance_criteria": ["页面能从规划走到显式接受并在刷新后读回同一回执。", "可从阻塞意见定位冻结行，跟随一次返工读回新候选及新测试和复审。", "双审补跑、目标变化、取消及缺失报告值遵守第 6 节的行为约定。", "给出可复现的启动和检查命令，完整 verify 与网页检查通过。"],
  "test_refs": ["lint", "format", "typecheck", "unit", "replay"],
  "open_questions": []
}
```
