# 项目 HTTP 服务

服务绑定一个本机 Git 仓库和独立的私有数据目录。CLI 和 HTTP 共用该目录下的 `reviews.sqlite3`。服务只监听 `127.0.0.1`，启动时在 stdout 打印实际 URL。一个数据目录同一时间由一个服务持有。

已有数据库在服务取得所有权后走工作台原有迁移。服务启动时保存数据库升级，HTTP 读取使用只读连接。空目录在首个需要持久化领域记录的动作时创建数据库。

```sh
uv run python -m multi_agent.web --repository "$REPO" --data-dir "$DATA" \
  --port 0 --runtime-read-path /System/Library/Perl/5.34
```

`--port 0` 选用空闲端口。服务端可通过 `--codex-bin`、`--claude-bin` 指定已安装的 CLI。首次运行注册测试所需的额外可执行目录或运行时只读路径，用重复的 `--tool-directory`、`--runtime-read-path` 在启动时配置。上例的 Perl 路径供账本样例在对应 macOS 版本使用。客户端提交项目文本、角色、注册测试名称和领域 ID，进程 argv、仓库、数据库和运行目录由服务构造。

## 同源网页

浏览器打开服务打印的地址即可进入工作台。`python -m multi_agent.web` 默认托管工作台源码下的 `frontend/dist`。可用 `--frontend-dist DIR` 指定其他构建目录。静态 GET 按真实路径限定在该目录内。`/assets/` 下的文件不存在时返回 404，其他前端路由回退到 `index.html`，入口页使用 `Cache-Control: no-cache`。构建目录缺失时，首页以 404 纯文本显示构建命令，`/api` 仍可用。直接构造 `WorkspaceServer` 时可省略页面目录，此时只提供 API。

## 工作区、作业与资源

| 请求 | 回执 |
| --- | --- |
| `GET /api/workspace` | 绑定仓库、分支、HEAD、项目规则来源和角色模型选项，另有本地分支列表 `branches` 和阶段默认值 `defaults`。`environments` 保留显式刷新观测。 |
| `POST /api/jobs`，JSON `{action,args}` | `202` 和 `job_id,state`。 |
| `GET /api/jobs` | `{items:[job]}`，按创建时间倒序返回最近 50 条，并包含更早且尚未结束的作业。 |
| `GET /api/jobs/{job_id}` | `state,exit_code,result,error,resources` 及可用的作业日志句柄。`result` 保留产品 CLI JSON。 |
| `POST /api/jobs/{job_id}/cancel`，JSON `{}` | 请求停止仍由服务控制的 CLI 作业，返回 `202`，随后轮询真实退出状态。已结算或无法控制时返回 `409`。 |
| `GET /api/runs`，可选 `kind` | `{items:[{kind,id,label,parent_id,updated_at}]}`，列出作业登记的领域资源。 |
| `GET /api/resources/{kind}/{id}` | `{kind,id,data,artifacts}`。已知 ID 也可以打开同一仓库下由 CLI 创建的记录。 |
| `GET /api/diffs/{kind}/{id}` | `baseline,candidate,snapshot_id,diff,test_change_paths`。kind 为 `task`、`integration` 或 `snapshot`。历史审核用对应的 `snapshot`。 |
| `GET /api/snapshots/{id}/file?side=baseline&path=app%2Famount.pl` | 已冻结文件的 `side,path,oid,mode,text`。side 也可为 `candidate`。 |
| `GET /api/artifacts/{artifact_id}?offset=0&limit=4096` | 已登记日志的 `stream,text,offset,next_offset,byte_count,sha256,truncated`，摘要对应完整原始字节。 |

资源 kind 为 `project`、`consultation`、`goal`、`project-plan`、`project-stages`、`stage`、`plan`、`task`、`task-test`、`task-review`、`integration`、`review` 和 `snapshot`。其中 `project-plan` 使用顶层 `plan_id`，`project-stages` 用同一 `plan_id` 列已领取阶段，`stage` 用 `stage_run_id`，`plan` 用单卡 `planning_run_id`，`task` 用 `task_run_id`。`task-test` 使用 `task_run_id` 与查询参数 `implementation_attempt`，可加 `test_attempt`。审核使用 `round_id`。审核快照使用 `snapshot_id`，咨询或项目规划回执的源快照使用 `source_snapshot_id`。支持历史的资源可加 `history=1`。`job_id` 仅用于作业轮询和取消。

Host 必须等于启动 URL 的权威地址。POST 带 Origin 时，Origin 必须等于该 URL。HTTP 错误使用 `{error:{code,message,details?}}`。进入作业后，计划、阶段、测试或审核可能返回业务拒绝。检查实际 `exit_code`、`result` 和 `error`，其中退出 1 或 130 的作业也可能保留有效的领域回执。重启后，以原 `job_id` 和领域 ID 读回。失去原进程控制权的作业显示 `interrupted`，查询仍可查看 SQLite 已保存的领域状态。恢复工作使用明确的续跑动作。普通查询和环境检查无模型回合。

### 找回作业、取消和并发

新作业的 `args` 记录提交的业务字段。`original_goal` 和 `confirmed_goal` 各用 `{"length":字符数}` 代替，`document.length` 计算紧凑 JSON 的字符数。只在对应动作的作业参数中做这项缩减，领域回执仍按原契约返回。四个 UTC ISO 8601 字段分别记录受理（`created_at`）、开始（`started_at`）、请求取消（`cancel_requested_at`）和观察到结束（`finished_at`）的时刻。尚未发生或缺少证据时为 `null`。CLI 退出后，`stderr_tail` 取 stderr 最后 8192 个原始字节，以 UTF-8 替换解码，空 stderr 为 `""`。运行中和缺字段的旧记录均返回 `null`。刷新页面后先查作业列表，再按原 `job_id` 恢复轮询。

任何一个 CLI 作业处于 `queued`、`running` 或 `cancelling` 时，提交第二个 CLI 作业返回 HTTP 409，错误码 `busy`，现有作业的 ID 在 `error.details.active_job_id`。请求不会排队。取消回执说明停止请求已登记，需继续轮询到 `finished`，以真实退出码和领域回执判断结果。本机在假 Fable 已进入规划回合后取消 `project.plan`，观察到退出码 1、`result.status:"failed"`、`result.error.failure_category:"cancelled"`、`stderr_tail:""`。服务重启时失去控制的作业记为 `interrupted`。环境检查可以与 CLI 作业同时运行，每个供应商一次。

| 情形 | 客户端可见结果 |
| --- | --- |
| CLI 作业期间读取 `task`、`task-test`、`task-review`、`integration`、`stage`、`plan` | HTTP 200，`{kind,id,data:null,artifacts:[],lock_busy:true}` |
| 读取任务或集成 diff | HTTP 200，保留 `baseline,candidate,snapshot_id,diff,test_change_paths`，`diff:null`、`unavailable_reason`、`unavailable_code:"lock_busy"`、`lock_busy:true` |
| 读取上述六类资源的日志页 | HTTP 200，保留 `stream,text,offset,next_offset,byte_count,sha256,truncated`，`text`、`next_offset` 和未取得的值为 `null`，带 `lock_busy:true` |
| 其他资源、冻结快照和已登记的作业日志 | 照常读取，按原端点规则检查资源归属和句柄 |

服务让需要仓库执行锁的读取逐个执行，包括 `project.stage.integrate` 提交前检查。在途读取完成后才启动新 CLI 子进程。网页看到 `lock_busy` 可保留现有显示，等作业结束再取一次。`lock_busy` 只表示本服务的读取闸拒绝了本次读取。并行运行的外部终端 CLI 也可能占用仓库锁，本机实测冲突原文为 `repository already has an implementation executor`。

### 日志坐标与返工差异

每条资源日志继续提供原来的 `artifact_id,stream,byte_count,sha256`。用新加的 `key` 和 `coordinates` 定位条目，再把服务给出的原句柄交给分页端点。下表的 `n`、`i` 表示这次回执内的日志位置。

| 来源 | key | coordinates |
| --- | --- | --- |
| 任务与集成测试命令 | `test-<测试尝试>-<命令序号>-<n>` | `{test_attempt,sequence,log_index}` |
| 需求咨询席位 | `seat-<席位>-<i>` | `{seat,log_index}` |
| 审核冻结的测试输出 | `frozen-<i>-<stream>` | `{frozen_index}` |
| 规划 | `planner-<i>` | `{log_index}` |

`GET /api/diffs/task/{id}?against=parent` 和 `GET /api/diffs/integration/{id}?against=parent` 返回从 `parent_candidate` 到当前 `candidate` 的 diff，`baseline` 保留原始基线。任务返工使用当前实施尝试记录的来源候选。修订集成先查 `rebased_from`，其次只采用直接生成本次子集成的返工记录。无父候选、候选未核验或作业占锁时，`diff` 为 `null`，分别返回 `no_parent`、`not_verified` 或 `lock_busy` 及对应的 `unavailable_reason`。省略查询参数仍读从原基线算起的累计 diff，快照 diff 使用原接口。

### 工作区和历史导航

`branches` 是本地分支的 `[{name,head}]`，`name` 可含斜线，`head` 为提交 SHA。`defaults` 包含 `source:"cli_default"`、`stage_mode`、`sol:{model,effort}` 和 `card_review:{provider,model,effort}`。后三个值可直接用于阶段启动表单。角色候选和显式环境观测见 [工作区接口](workspace-api.md)。

`GET /api/runs` 的 `label` 优先取卡号、目标或需求首行、目标分支名。修订集成的 `parent_id` 指向源集成。自动返工产生的原审核轮和确认轮也可从历史打开。

### HTTP 错误

| HTTP | `error.code` | 含义 |
| --- | --- | --- |
| 400 | `invalid_request`、`unknown_action`、`invalid_provider` | 请求体、参数、动作或供应商无效 |
| 403 | `invalid_host`、`invalid_origin` | Host 或 POST Origin 与服务地址不符 |
| 404 | `not_found` | 资源、作业、快照文件或已登记的日志不可用 |
| 409 | `busy`、`environment_busy`、`job_unavailable` | CLI 已有活动作业、同一供应商的环境检查在运行，或作业当前不可执行 |
| 500 | `workspace_unavailable` | 工作区文件或数据库操作失败 |
| 501 | `invalid_request` | HTTP 方法不受支持 |

已受理的 CLI 业务拒绝从作业读回。退出码 2 可能只给出 stderr，例如本卡 `integration.accept` 的资格拒绝，`result` 为 `null`。前端可在作业详情展示 `stderr_tail`。当前领域资源提供咨询、规划、测试和冻结输出；实施与代码审核席位的完整会话日志仍待后端提供入口。

## 动作与 CLI 对照

角色配置使用 `{provider,model,effort}`。Codex 的 provider 为 `codex`，Claude 顾问、规划和独立审核为 `claude`，Claude 实施为 `claude-code`。`sol` 使用 `{model,effort}`。表中 `?` 表示可省略。每个动作使用下述字段，省略的角色使用对应产品入口的默认值。

| `action` | `args` | 对应入口与主要回执 |
| --- | --- | --- |
| `environment.refresh` | `provider` | `RuntimeAdapter.check_environment()`。记录该供应商的版本、登录、目录和档位。 |
| `project.consult` | `baseline,original_goal,advisors,project_id?` | `project consult`。`consultation_id`、同一 `source_snapshot_id`、席位报告、可编辑草案。 |
| `project.confirm` | `baseline,original_goal,confirmed_goal,project_id?,consultation_id?,adoption?` | `project confirm`。`goal_id,project_id`，有 `consultation_id` 时可给 `adoption`，省略则根据文本推导；单独给 `adoption` 回 400。 |
| `project.plan` | `goal_id,planner` | `project plan GOAL_ID`。新 `plan_id`、`document`、`markdown` 和运行证据。 |
| `project.revise` | `plan_id,document` | `project revise PLAN_ID --document FILE`。新 `plan_id` 和 `source_plan_id`。 |
| `project.stage.start` | `plan_id,stage_id,mode?,baseline?,sol?,card_review?` | `project stage start`。`stage_run_id,execution_baseline`，直做候选或委派叶子。再次提交已领取的委派阶段时须传原 `mode:"delegate"`，省略会按 CLI 默认的 `direct` 校验。 |
| `project.stage.resume` | `stage_run_id,prepare?` | `project stage resume`。用原阶段继续明确可恢复的工作。 |
| `project.stage.leaf` | `stage_run_id,card_id,implementation?,baseline?,review_only?,refreeze?` | `project stage leaf`。叶卡的 `planning_run_id`、复核与候选。初次领取需要实施角色。 |
| `project.stage.integrate` | `stage_run_id,target?` | `project stage integrate`。全组叶子的 `integration_id,target_ref,target_baseline,candidate`。 |
| `task.run` | `planning_run_id,implementation?,fallbacks?,task_type?` | `task run`。单卡的 `task_run_id` 和实施证据。 |
| `task.retry` | `planning_run_id` | `task run --retry`。沿用原配置。 |
| `task.run_batch` | `planning_run_ids,implementation?,fallbacks?,task_type?` | `task run-batch`。分别返回每个任务的实施证据。 |
| `task.test` | `task_run_id,implementation_attempt,commands,declaration_source` | `task-test run`。同一任务候选的 `test_attempt` 与命令结果。 |
| `task.review` | `task_run_id,implementation_attempt,test_attempt,seats,auto_rework?` | `task-review run`。`round_id,snapshot_id` 和单审或双审结果。 |
| `task.retry_review` | `round_id,seat_keys?` | `task-review retry`。原轮次与重试结果。 |
| `task.confirm`、`task.rework` | `round_id` | `task-review confirm` 或 `task rework`。对应确认轮或返工实施。 |
| `integration.create` | `task_run_ids,target` | `integration create`。显式选择成员的通用集成。 |
| `integration.test` | `integration_id` | `integration test`。覆盖冻结目标规则中的完整注册测试。 |
| `integration.review` | `integration_id,seats,auto_rework?,test_attempt?` | `integration review`。同一集成候选的审核与快照。 |
| `integration.retry_review` | `integration_id,round_id,seat_keys?` | `integration retry-review`。按原快照补跑。 |
| `integration.confirm`、`integration.rework` | `integration_id,round_id` | `integration confirm` 或 `integration rework`。对应确认或新修订。 |
| `integration.rebase` | `integration_id` | `integration rebase`。根据现有资格建立新集成。 |
| `integration.accept` | `integration_id,round_id` | `integration accept`。以现有测试、审核和目标资格签发显式接受回执。 |

`project.stage.integrate` 通过本阶段当前全部核验过的叶子候选创建集成。省略 `target` 时，工作台可在阶段执行基线创建或复用 `refs/heads/multi-agent/stages/<stage_run_id>`。显式指定 target 时，分支必须已经位于阶段执行基线。缺少叶子候选时不会创建目标引用。创建、验证与接受均保持项目分支原位。

## 两阶段账本验收

复制一份样例仓库，用新的私有数据目录启动服务。以样例 `REQUIREMENTS.md` 作原话及确认内容，可直接确认，也可先提交 `project.consult` 后编辑采纳。以下示例使用 Python 标准库，`ORIGIN` 是服务打印的回环 URL。

```sh
curl -sS "$ORIGIN/api/workspace"
curl -sS -H 'Content-Type: application/json' \
  --data '{"action":"environment.refresh","args":{"provider":"codex"}}' "$ORIGIN/api/jobs"
curl -sS "$ORIGIN/api/jobs/$JOB_ID"
curl -sS "$ORIGIN/api/resources/project-plan/$PLAN_ID"
```

```python
import json
import time
from urllib.request import Request, urlopen


def call(origin, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(
        origin + path, data=body, headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def job(origin, action, **args):
    started = call(origin, "/api/jobs", {"action": action, "args": args})
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        result = call(origin, "/api/jobs/" + started["job_id"])
        if result["state"] in ("finished", "interrupted"):
            return result
        time.sleep(0.5)
    raise TimeoutError(started["job_id"])
```

1. `project.confirm` 持久化目标，`project.plan` 生成三阶段计划。保存目标和计划 ID。读取计划的结构化阶段列表。
2. 对金额阶段发 `project.stage.start`。保存直做 `task_run_id`、实施尝试和候选。以 `task.test` 运行注册 `test`，再以 `task.review` 审核同一候选。等阶段资源显示测试和审核通过。
3. 对交易汇总阶段发 `project.stage.start`，选择 `delegate`。读取两个叶子的卡号。分别以 `project.stage.leaf` 领取并实施。两个候选验证后，发 `project.stage.integrate`。在集成 ID 上先 `integration.test` 再 `integration.review`，并核对阶段详情和冻结 diff。
4. 用该集成 ID 和当前合格审核轮显式 `integration.accept`。保存接受回执。以 Git 固定候选检出分别运行 `perl checks/ledger.pl direct`、`income`、`expense` 和 `delegated`。月报阶段留在计划中，运行列表只有金额与汇总。
5. 停止并重启服务，仓库与数据目录保持相同。用原作业 ID、计划 ID、阶段 ID 和审核快照读回，和 CLI `project show`、`project stage show`、`project stage list` 的对应结果核对。


## 离线联调

从仓库根目录启动一个场景，打开终端第一行打印的回环地址。可追加 `--frontend-dist /绝对路径/dist` 加载网页构建。每次启动创建临时 Git 仓库、数据目录、HOME 和 CODEX_HOME，模型调用走场景提供的两支假 CLI。合成的 Codex `model/list` 故意列出八个家族，覆盖 Luna 和 Haiku 的过滤；Claude 环境检查沿用适配器的观测能力，`models` 为 `null`。需要模拟服务崩溃时给启动器进程发送 SIGUSR1，随后在同一地址用原 ID 读回作业。SIGHUP 正常重启，Ctrl-C 停止服务并清理场景目录。

```sh
uv run python tests/unit/web_support/offline_env.py --scenario accept --port 0
uv run python tests/unit/web_support/record_samples.py
```

四个场景共用下列页面输入。

| 输入 | 填写或选择 |
| --- | --- |
| 目标与基线 | 选择 `main` 当前 HEAD。原话填 `Build the amount and summary, then outline monthly reporting.` 加换行。咨询后保留原话，追加一行 `Use the checked output format.` 和换行，按“编辑后采纳”确认。 |
| 需求顾问、项目规划 | Claude `claude-fable-5-2`，档位 `low`。先刷新两家环境以查看目录和档位来源。 |
| 阶段负责人、卡片复核 | `gpt-6.2-sol` 和 `claude-opus-5-5`，均为 `low`。金额阶段 `amount` 选 `direct`；汇总阶段 `summary` 选 `delegate`。月报 `report` 留在计划中。 |
| 叶子实施、审核 | 两张叶卡选 `claude-code / claude-sonnet-5-5 / low`。常规审核选 `claude / claude-opus-5-5 / low`，双席再加 `codex / gpt-6.2-astra / low`。页面显示运行时报告的模型，未报告时显示未确认。 |
| 测试与集成 | 直接阶段测试命令按顺序选 `build`、`test`，声明来源填 `PM`。集成测试执行冻结规则中的完整套件，集成目标省略，使用阶段分支。 |

| 场景 | 操作与预期 |
| --- | --- |
| `accept` | 完成金额阶段实施、测试和审核；领取汇总阶段两张叶卡并分别实施，开始集成、跑完整测试并审核。用不属于本集成的审核轮尝试接受，得到作业退出码 2、`result:null` 和 stderr 说明。用本集成当前合格轮提交，读回绑定候选的接受回执。 |
| `rework` | 沿同一路径到汇总集成初审。单席给出 `frontend/status.js` 第 1 行的阻塞意见，可按快照侧和路径核对引文。发起一次集成返工，在新候选上核对重测、复审、相对父候选的改动和接受回执。 |
| `dual-partial` | 汇总集成使用上述两个审核席位。Claude 首次失败，Codex 成功但实际模型为 `null`。在原轮只补跑失败席位，确认冻结快照一致、尝试数分别为 2 和 1，随后接受。 |
| `cancel` | 汇总阶段先复核两张叶卡。实施第一张期间读取任务、阶段、diff 和已登记的测试日志，均可看到 `lock_busy`。此夹具支持用并发的 Codex 环境刷新作为同步点：该次刷新在当前 Sonnet 叶子的会话和 waiting 消息落库后完成。手工联调可在取消或硬重启前各刷新一次。取消回执为 `cancelling`，随后轮询至 `finished`，退出码为 130。实施第二张期间向启动器发 SIGUSR1，重启后原作业为 `interrupted`，前一作业的已完成记录仍在。 |

每个场景的 `index.json` 按顺序给出 `seq,method,path,query,request_body,status,response_file`。查询字符串不含问号，没有查询或请求体时为 `null`。`NNN.json` 是对应的 HTTP 响应体，只保留能展示状态转换的轮询。录制时按首次出现映射 ID、Git 提交、摘要、时间、进程与文件身份、临时路径和端口，保持跨请求引用一致。对 `art-` 句柄先解码、替换引用再重新编码。样例中的 `byte_count` 保留实录值；独立重录比较时仅规范化含动态元数据的 CLI JSON 回执和协议日志长度，固定测试输出、分页字段与内容、退出码均逐值核对。样例通过录制脚本生成，可用 `--scenario 名称 --output 目录` 单独重录。[样例索引说明](web-api/samples/README.md)
