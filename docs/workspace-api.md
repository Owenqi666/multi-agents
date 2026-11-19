# 本地工作区 API

服务绑定一个 Git 仓库和仓库外的私有数据目录。已有数据目录须由当前用户拥有并关闭组及其他用户访问。使用绝对路径启动，终端会打印 `http://127.0.0.1:PORT`。

```sh
uv run python -m multi_agent.web --repository /abs/repo --data-dir /abs/runs
```

`--port` 可指定回环端口，默认由系统分配。`--codex-bin` 和 `--claude-bin` 支持从受信的启动参数指定可执行文件，例如本地验收夹具。HTTP 客户端用打印地址中的 `127.0.0.1:PORT` 作为 `Host`。原生客户端可省略 `Origin`。写请求携带 `Origin` 时须与打印地址完全一致。

## HTTP 接口

| 请求 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /api/workspace` | 无 | 工作区、配置数据和各供应商最近一次成功的环境观测 |
| `POST /api/jobs` | `application/json`，字段为 `action` 和 `args` | HTTP 202，`{job_id,state}` |
| `GET /api/jobs/JOB_ID` | 无 | `{job_id,action,state,exit_code,result,error,resources}` |

作业动作是 `environment.refresh`，其参数为 `{"provider":"codex"}` 或 `{"provider":"claude"}`。服务启动和 GET 只读 Git 与已有记录。每次提交调用所选供应商的环境检查。使用新的 job ID 记录每次作业。同一供应商正在检查时，重复提交返回 HTTP 409。两家供应商可分别提交。

接受时状态为 `queued`，环境检查期间为 `running`。结束后为 `finished`，由 `error` 区分正常观测和失败。环境作业执行适配器方法，回执的 `exit_code` 为 null，`resources` 为空列表。HTTP 作业和环境观测保存在数据目录的 `web-environment/state.json`。每个数据目录绑定原来的仓库路径，由单个服务持有。服务关闭期间继续持有数据目录，直到环境检查收尾。服务重启后，已完成的作业可按原 ID 读取。失去进程控制的 `queued`、`running` 或 `cancelling` 记录恢复为 `interrupted`，由客户端显式重新提交。

| HTTP 状态 | 含义 |
| --- | --- |
| 400 | JSON、动作、供应商或输入字段不符合契约 |
| 403 | Host 或写请求的 Origin 不符合回环服务地址 |
| 404 | 端点或作业 ID 不存在 |
| 409 | 同一供应商已有环境作业占用运行权 |
| 500 | 工作区无法读取，或服务无法登记作业 |

错误响应为 `{"error":{"code":"…","message":"…"}}`。已受理的环境失败通过作业结果读取，HTTP 状态仍为 200。

## 数据和来源

工作区响应字段为 `repository`、`target_branch`、`head`、`read_only`、`presets` 和 `environments`。分离 HEAD 时 `target_branch` 为 null。仓库还未提交时 `head` 为 null。当前入口使用 `read_only: false`。

- `environments.codex` 和 `environments.claude` 分别保存最近的成功观测，包括 `provider`、`observed_at`、`source: "runtime_environment_check"`、`cli_version`、`logged_in`、`models`、`model_efforts` 和 `error`。尚无观测的供应商或运行时未提供的数据用 null。最近的失败回执保存在对应作业，之前的成功观测仍可读取。
- `presets.models` 列出六个工作台预设，外层 `source` 为 `workbench_model_presets`。`presets.roles[].candidates` 从预设与当前观测生成候选。供应商有可见目录时，每个允许家族取其中数字版本最新的具体型号，并采用该型号报告的档位。例如 `gpt-6.10-sol` 晚于 `gpt-6.9-sol`。目录支持 `gpt-版本-家族` 和 `claude-家族-版本` 的具体型号。Claude 无目录时使用标为 `workbench_preset` 的建议值。候选记录含 `family`、`model`、`provider`、`efforts`、`source` 和 `effort_source`。当目录已报告但未提供某型号的档位时，对应 `efforts` 和 `effort_source` 为 null。
- `presets.rules` 投影当前 HEAD 提交中的 `.multi-agent/rules.yaml`。它包含 `baseline_commit`、`rules_hash`、`source`、`protected_paths`、`implementation`、`default_implementation`、`task_types`、`applies_to` 和 `error`。`status` 可为 `available`、`unavailable` 或 `error`。未取得有效规则时，`protected_paths` 为空列表。每组实施规则含声明的路径、供应商、允许型号、默认型号和档位，以及 `effective` 给出的匹配路径、有效型号和冲突。规则同时返回目录使用的 `provider` 和派发使用的 `implementation_provider`，其中 Claude 派发名为 `claude-code`。根据实际写入范围和任务类型选择实施或备选时使用此规则。独立审核候选按角色和目录产生。

| 角色 ID | 候选家族 |
| --- | --- |
| `advisor`、`planner` | Astra、Fable |
| `stage_lead` | Sol |
| `implementation`、`fallback` | Sol、Terra、Sonnet |
| `review` | Astra、Sol、Terra、Fable、Opus、Sonnet |

静态预设 ID 依次为 `gpt-6.2-astra`、`gpt-6.2-sol`、`gpt-6.1-terra`、`claude-fable-5-2`、`claude-opus-5-5` 和 `claude-sonnet-5-5`。目录可见性、登录观测和规则分别保留各自的来源。候选供客户端构造显式请求配置，实际连接及生效的模型和档位由之后的模型调用单独报告。

## HTTP 验收

将 `BASE` 设置为启动命令打印出的地址。下例使用 curl 和 jq。

```sh
BASE=http://127.0.0.1:PORT
curl -fsS "$BASE/api/workspace" | jq .
ACK=$(curl -fsS "$BASE/api/jobs" \
  -H 'Content-Type: application/json' \
  -H "Origin: $BASE" \
  -d '{"action":"environment.refresh","args":{"provider":"codex"}}')
printf '%s\n' "$ACK"
JOB_ID=$(printf '%s\n' "$ACK" | jq -r .job_id)
curl -fsS "$BASE/api/jobs/$JOB_ID" | jq .
```

重复最后一条命令直到 `finished`，再读取工作区。使用 `provider: "claude"` 提交第二个作业，比较两家观测和各角色候选。用相同仓库和数据目录重启服务，并用新的回环地址读取原 job ID，核对回执和环境观测。

离线回归使用真实的回环 HTTP 客户端、临时 Git 仓库和合成运行时。项目检查入口为 `scripts/verify.sh`。
