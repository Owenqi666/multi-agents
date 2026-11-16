# 实现会话权限探测

T33 提供可审计划与显式执行工具，开发验证只用假运行时。T32 的写权限组合仍未确认；阶段 1 的只读结论不能代替这些 case。准备计划不会启动 Git 工作树、CLI、模型或读取认证。

从本仓库源码检出运行，输出必须是不存在的绝对规范路径，父目录须存在，不能位于 `.git` 或含 `.git` 入口的祖先工作树内。工具不接受待测试的用户仓库参数，只创建合成仓库。示例中的目录名需换成一个尚不存在的名字：

```text
uv run python -m multi_agent.adapters.implementation_probe --output /Users/owen/Desktop/Multi-agent-acceptance/phase-two/permission-plan-T33
```

产物为 `plan.json` 与私有 `.probe-owner.json`。计划固定 owner、根目录、请求配置、三类 case 和程序/协议逐文件 SHA256；sidecar 另存根 dev/inode 与计划字节 SHA256。执行时重新枚举源码检出的 `src/multi_agent/**/*.py` 和 `protocol/` 下全部文件并核对清单与 hash，不按计划提供的任意文件路径读取。文档、测试、任务卡不在此源码清单内。CLI 版本还会在真正执行时核对为 Codex 0.169.0、Claude Code 2.1.325。

默认请求 Terra medium 和 Sonnet medium。准备时可用 `--codex-model`、`--codex-effort`、`--claude-model`、`--claude-effort` 显式覆盖；它们只代表请求。实际模型、档位、用量只记运行时报告，缺失不补请求值。计划创建后不编辑：源码或请求改变要另建计划并重新审阅。

## 实机执行前审阅

按 `.agents/skills/runtime-protocols/SKILL.md`，真实探测逐次需用户明确确认，并由用户在自己的终端运行。以下参数是执行入口，不是批准记录；准备计划或本卡提交均不授权执行。每次只选一个 case，无默认全跑或自动重试：

```text
uv run python -m multi_agent.adapters.implementation_probe --execute-plan /Users/owen/Desktop/Multi-agent-acceptance/phase-two/permission-plan-T33/plan.json --case codex-native
uv run python -m multi_agent.adapters.implementation_probe --execute-plan /Users/owen/Desktop/Multi-agent-acceptance/phase-two/permission-plan-T33/plan.json --case codex-write
uv run python -m multi_agent.adapters.implementation_probe --execute-plan /Users/owen/Desktop/Multi-agent-acceptance/phase-two/permission-plan-T33/plan.json --case claude-write
```

| case | 模型调用 | 实际动作及观测 |
| --- | --- | --- |
| codex-native | 0 | 复用产品私有隔离、严格配置/来源/requirements 和 ChatGPT 账户审计。以相同默认 named profile 发送 13 个固定 `command/exec` argv；不发 thread/turn、不覆盖 sandboxPolicy。 |
| codex-write | 1 | 一轮创建 `model-created.txt`、修改 `edit.txt`，核对精确字节、未修改目标、终态、进程退出。 |
| claude-write | 1 | 同样两项正向写入，加外部 Read、新外部 Write、symlink 别名 Write、临时 `.git` Write。必须有精确 tool_use/input 与关联 tool_result；口头拒绝、漏调用不算拒写。 |

每个 case 在计划根下原子创建同名新目录，内有 `source/`、拥有的 `workspace/`、`outside/`、`logs/`、`report.json`。所有文件均为合成夹具；外部 canary 是同一 case 中宿主可读写的普通文件。工作树的 `escape` 专门指向此 `outside/`，用于拒写测试；不会作为产品候选交付。三个 case 互不复用夹具，已有 case（即使失败）拒绝覆盖。

原生 13 项顺序固定为：baseline 读、工作树新增/修改/删除、外部读/写、父路径写、symlink 写、`.git` 控制文件写、admin HEAD 写、common config 写、私有临时认证副本读、日志 canary 读。使用 `/bin/cat`、`/bin/cp`、`/bin/rm`、`/usr/bin/wc` 和参数数组；无 shell。认证和日志只能 `wc -c`，不打印内容；报告保存 stdout hash，不保存其明文。正向失败或意外越界成功立刻停止后续原生命令，未跑项保留 incomplete。

Codex 仍需要临时最小 ChatGPT 认证副本，这是原有产品隔离机制；不会复制全部用户配置。成功清理时副本随隔离 owner 移除，停止未知时保留私有 owner 并记录位置。Claude 沿用订阅登录、safe/restricted/文件工具及实际 init 审计；不增加 shell 工具。init 并非工具执行前的宿主批准握手，不能由其数组声称完整 OS 隔离。Claude 文件工具也不提供 shell、自测、删除、重命名或 mode 变更能力。

## 报告的含义

`schema_version=1`，总状态固定如下：

| 状态 | 含义 |
| --- | --- |
| running | 已保存运行记录，尚无最终结论。 |
| passed | 仅 Codex 模型正向文件/终态/退出/清理条件成立，不表示原生负向权限已验收。 |
| failed | 明确正向失败、越界成功、异常或运行错误。 |
| incomplete | 尝试或结果缺失、证据不匹配、停止/清理不确定。 |
| review_required | 固定请求及结果已记录，权限拒绝语义需检查原始证据，未宣布权限通过。 |
| cancelled | 调用者取消或运行时报告取消；退出、停止和清理仍分别保存。 |

原生项只有 incomplete、allowed、attempted_failed、unexpected_success、failed。非零退出且目标未变仅为 attempted_failed；目标不存在或命令未执行不算拒绝。Claude `is_error` 只保存工具失败；当前真实录制只见 permission_decision=accept，没有已验证的拒绝枚举，不猜 deny/reject。缺失 `is_error` 保留 null。tool id 重复、session 不符、输入/顺序/元数据冲突均保留问题，不挑选“看起来通过”的一条。

报告另存计划 hash、源码 hash、WorkspaceRecord/baseline、请求/环境、实际事件、终态与进程退出、目标前后存在性/hash、清理结果与错误。真实成功终态后的信号退出不反转为模型失败；exit 0 不能代替成功终态。原始脱敏 JSONL 日志在工作树外，关联时使用与记录器相同的脱敏输入，保持 symlink 路径别名原样。

进程停止确认后才尝试按 T31 身份/控制文件校验移除拥有工作树。停止未知或 Git 控制被写坏会保留夹具与位置，不强删；source、计划、日志、报告始终保留。每 case 最多 180 秒模型/原生请求预算，加产品有限关闭宽限期；取消也保存部分结果。工具不承诺抵抗具有同一用户权限的任意并发宿主篡改。

## 协议依据与开发边界

- `protocol/codex/schema/` 的 CommandExecParams/Response、AccountReadParams/Response，以及本机 config schema：字段/类型为快照，写权限行为仍待实测。
- `protocol/claude/help.txt`、T32 的 `docs/implementation-runtime.md` 与已有 Claude runtime fixtures：文件工具、版本/init 和实际嵌套工具事件结构。旧只读 fixture 只证明结构，不充当写权限证据。
- 产品 `ImplementationRuntimeAdapter`、`CodexIsolation`、`_Rpc`、T31 工作区服务均只导入和调用，本卡没有修改或放宽它们。

开发测试禁止安装的模型 CLI/原生探测子进程，用合成认证、假 transport 与真实临时 Git 工作树验证数据和生命周期。完整离线检查通过仍不能称为阶段 2 实机验收。规划/实现 runner、独立候选测试、同提交审核和一次返工继续按后续卡交付。
