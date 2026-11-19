# 项目 HTTP 服务

服务绑定一个本机 Git 仓库和独立的私有数据目录。CLI 和 HTTP 共用该目录下的 `reviews.sqlite3`。服务只监听 `127.0.0.1`，启动时在 stdout 打印实际 URL。一个数据目录同一时间由一个服务持有。

已有数据库在服务取得所有权后走工作台原有迁移。服务启动时保存数据库升级，HTTP 读取使用只读连接。空目录在首个需要持久化领域记录的动作时创建数据库。

```sh
uv run python -m multi_agent.web --repository "$REPO" --data-dir "$DATA" \
  --port 0 --runtime-read-path /System/Library/Perl/5.34
```

`--port 0` 选用空闲端口。服务端可通过 `--codex-bin`、`--claude-bin` 指定已安装的 CLI。首次运行注册测试所需的额外可执行目录或运行时只读路径，用重复的 `--tool-directory`、`--runtime-read-path` 在启动时配置。上例的 Perl 路径供账本样例在对应 macOS 版本使用。客户端提交项目文本、角色、注册测试名称和领域 ID，进程 argv、仓库、数据库和运行目录由服务构造。

## 工作区、作业与资源

| 请求 | 回执 |
| --- | --- |
| `GET /api/workspace` | 绑定仓库、分支、HEAD、项目规则来源和角色模型选项。`environments` 保留显式刷新观测。 |
| `POST /api/jobs`，JSON `{action,args}` | `202` 和 `job_id,state`。 |
| `GET /api/jobs/{job_id}` | `state,exit_code,result,error,resources` 及可用的作业日志句柄。`result` 保留产品 CLI JSON。 |
| `POST /api/jobs/{job_id}/cancel`，JSON `{}` | 请求停止仍由服务控制的 CLI 作业，返回 `202`，随后轮询真实退出状态。已结算或无法控制时返回 `409`。 |
| `GET /api/runs`，可选 `kind` | `{items:[{kind,id,label,parent_id,updated_at}]}`，列出作业登记的领域资源。 |
| `GET /api/resources/{kind}/{id}` | `{kind,id,data,artifacts}`。已知 ID 也可以打开同一仓库下由 CLI 创建的记录。 |
| `GET /api/diffs/{kind}/{id}` | `baseline,candidate,snapshot_id,diff,test_change_paths`。kind 为 `task`、`integration` 或 `snapshot`。历史审核用对应的 `snapshot`。 |
| `GET /api/snapshots/{id}/file?side=baseline&path=app%2Famount.pl` | 已冻结文件的 `side,path,oid,mode,text`。side 也可为 `candidate`。 |
| `GET /api/artifacts/{artifact_id}?offset=0&limit=4096` | 已登记日志的 `stream,text,offset,next_offset,byte_count,sha256,truncated`，摘要对应完整原始字节。 |

资源 kind 为 `project`、`consultation`、`goal`、`project-plan`、`project-stages`、`stage`、`plan`、`task`、`task-test`、`task-review`、`integration`、`review` 和 `snapshot`。其中 `project-plan` 使用顶层 `plan_id`，`project-stages` 用同一 `plan_id` 列已领取阶段，`stage` 用 `stage_run_id`，`plan` 用单卡 `planning_run_id`，`task` 用 `task_run_id`。`task-test` 使用 `task_run_id` 与查询参数 `implementation_attempt`，可加 `test_attempt`。审核使用 `round_id`。快照使用审核或规划回执中的 `snapshot_id`。支持历史的资源可加 `history=1`。`job_id` 仅用于作业轮询和取消。

Host 必须等于启动 URL 的权威地址。POST 带 Origin 时，Origin 必须等于该 URL。HTTP 参数错误使用 `{error:{code,message}}`。进入作业后，计划、阶段、测试或审核可能返回业务拒绝。检查实际 `exit_code`、`result` 和 `error`，其中退出 1 或 130 的作业也可能保留有效的领域回执。重启后，以原 `job_id` 和领域 ID 读回。失去原进程控制权的作业显示 `interrupted`，查询仍可查看 SQLite 已保存的领域状态。恢复工作使用明确的续跑动作。普通查询和环境检查无模型回合。

## 动作与 CLI 对照

角色配置使用 `{provider,model,effort}`。Codex 的 provider 为 `codex`，Claude 顾问、规划和独立审核为 `claude`，Claude 实施为 `claude-code`。`sol` 使用 `{model,effort}`。表中 `?` 表示可省略。每个动作使用下述字段，省略的角色使用对应产品入口的默认值。

| `action` | `args` | 对应入口与主要回执 |
| --- | --- | --- |
| `environment.refresh` | `provider` | `RuntimeAdapter.check_environment()`。记录该供应商的版本、登录、目录和档位。 |
| `project.consult` | `baseline,original_goal,advisors,project_id?` | `project consult`。`consultation_id`、同一 `source_snapshot_id`、席位报告、可编辑草案。 |
| `project.confirm` | `baseline,original_goal,confirmed_goal,project_id?,consultation_id?,adoption?` | `project confirm`。`goal_id,project_id`，可带 `consultation` 采纳关联。 |
| `project.plan` | `goal_id,planner` | `project plan GOAL_ID`。新 `plan_id`、`document`、`markdown` 和运行证据。 |
| `project.revise` | `plan_id,document` | `project revise PLAN_ID --document FILE`。新 `plan_id` 和 `source_plan_id`。 |
| `project.stage.start` | `plan_id,stage_id,mode?,baseline?,sol?,card_review?` | `project stage start`。`stage_run_id,execution_baseline`，直做候选或委派叶子。 |
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
