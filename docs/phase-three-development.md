# 阶段 3 开发路线

阶段 3 已通过主窗口技术验收。四张功能卡依次交付并行执行、按依赖集成、最终验证及显式接受、集成审核后的单轮返工。[T-0058](../tasks/T-0058-parallel-task-execution.json) · [T-0059](../tasks/T-0059-integrate-candidates.json) · [T-0060](../tasks/T-0060-verify-accept-integration.json) · [T-0061](../tasks/T-0061-integrated-review-rework.json)

主窗口的 GPT-6.2 Astra 负责规划、定接口、分配任务和审核交付。独立开发窗口配置 GPT-6.2 Sol、`ultra` 推理档位，负责实现和验证。工作台的运行角色由使用者按项目配置。

用户已提前授权在阶段 3 使用已登录的真实模型运行时测试。优先实测关键用户路径、协议及权限差异，并把证据绑定到产品提交。日常回归使用定向离线检查。

按可独立验收的功能拆卡。只有写入范围互不重叠且没有依赖的卡可以并行。优先复用既有逻辑，新增测试聚焦用户操作路径和已确认的回归。提交前完整运行 `scripts/verify.sh`，提交 hook 完整执行时计为同一次检查。

| 顺序 | 可验收功能 | 验收依据 |
| --- | --- | --- |
| 1 | 同仓独立任务并行执行，各自产出候选（T-0058 已验收） | 从现有 CLI 发起一个批次，两个已就绪的独立任务在各自工作区重叠执行，并能通过既有任务入口核对各自的候选提交。 |
| 2 | 按依赖顺序集成候选，记录冲突归属并固定集成候选（T-0059 已验收） | 可追溯应用顺序和发生冲突的任务，集成产物有固定提交供最终验证。 |
| 3 | 对最终候选跑完整测试、审核及显式接受（T-0060 已验收） | 测试和审核证据均绑定最终候选提交。接受时核对候选引用和目标基线，目标分支推进后用新 ID 重新验证。 |
| 4 | 在固定集成候选上完成一轮有明确归属的返工（T-0061 已验收） | 使用经核对的阻塞项和原任务约定，从审核过的 SHA 启动返工，生成新候选后按原配置完整重测复审。 |

## 并行执行入口

```sh
uv run python -m multi_agent task run-batch PLAN_RUN_ID_1 PLAN_RUN_ID_2 \
  --database /path/to/runs/workbench.db \
  --repository /path/to/project \
  --directory /path/to/runs/batch
```

批次使用同一 Git 仓库、数据库和基线提交。计划须通过审核，卡片之间的写入范围须互不相交，且没有互相依赖。工作台按卡片的冻结规则选择执行者。示例中的任务目录为数据库旁的 `batch-任务卡ID`，每个任务使用其中独立的 `workspace`。

输出的 `tasks` 按输入顺序列出各自的 `task_run_id`、阶段、候选 SHA 和验证状态。用 `task show --task TASK_RUN_ID --database /path/to/runs/workbench.db` 可重新核对。全部候选通过核对时退出码为 0，成员有未验证结果时为 1，批次拒绝时为 2，中断时为 130。

## 候选集成入口

```sh
uv run python -m multi_agent integration create TASK_RUN_ID_1 TASK_RUN_ID_2 \
  --database /path/to/runs/workbench.db \
  --repository /path/to/project \
  --target main

uv run python -m multi_agent integration show INTEGRATION_ID \
  --database /path/to/runs/workbench.db
```

`create` 核对每个任务的当前候选和原卡基线，以输入顺序挑选依赖已满足的任务。具名上游换版后，后继必须基于包含该确切候选的提交重新规划。外部依赖在成员中显示规划时的用户声明。

成功记录包含目标分支的当次基线、成员来源及 Git 候选的提交、树和私有 ref。冲突时退出 1，保留集成 ID、已应用成员及冲突任务和路径。输入或 Git 操作失败时退出 2。可用 `show` 读回固定记录并检查 `target_is_current`。目标分支前进后，重新 `create` 会按新基线生成新 ID。

## 完整测试、审核及显式接受

```sh
uv run python -m multi_agent integration test INTEGRATION_ID \
  --database /path/to/runs/workbench.db --repository /path/to/project \
  --directory /path/to/runs/integration-tests

uv run python -m multi_agent integration review INTEGRATION_ID \
  --database /path/to/runs/workbench.db --repository /path/to/project \
  --directory /path/to/runs/integration-review --seat claude MODEL EFFORT

uv run python -m multi_agent integration accept INTEGRATION_ID --review ROUND_ID \
  --database /path/to/runs/workbench.db --repository /path/to/project
```

测试按目标冻结基线的项目规则，运行全部注册命令。独立检出保留同一轮的构建产物，并归档原始日志、命令请求和退出事实。通过后才能发起审核。可指定一个席位，或重复 `--seat` 选择两个不同模型。两席共享固定差异、所有成员原卡和测试档案。`integration retry-review INTEGRATION_ID --review ROUND_ID` 默认在原快照补跑失败席位，接受相同的数据库和仓库选项。

`integration show INTEGRATION_ID --database … --history` 提供测试、审核、已签发回执和当前可接受资格。用户显式调用 `accept` 才签发固定 SHA 的历史回执。若目标推进，使用 `integration rebase INTEGRATION_ID --database … --repository …` 创建附原 ID 的新记录，再完整测试、审核和显式接受新 ID。后续返工流程通过固定审核快照、测试证据与可归属的阻塞项取得输入。

隔离样例的真机验收范围与原件记录在阶段 3 的 T60 回执。双席补跑、目标推进后按新规则重验及旧证据保留由定向离线回归覆盖。

## 同轮确认及一次集成返工

`integration review` 在初审存在已核对、原卡允许且全部归同一成员的阻塞时，默认派发一次集成返工。启动目录以原审核目录名加 `-rework`，之后的完整重测与原席复审使用 `-retest`、`-rereview`。这些目录由工作台在数据库旁创建。

若希望先人工处置或查看双席的交叉确认，初审加 `--no-auto-rework`。可用 `integration confirm INTEGRATION_ID --review ROUND_ID --database … --repository …` 在当前父轮和固定快照内确认。确认失败席可通过 `retry-review` 指定确认轮 ID 补跑。仍有实质分歧、未确认意见或归属不清时由用户处置。明确符合原卡的阻塞也可用 `integration rework INTEGRATION_ID --review ROUND_ID --database … --repository …` 显式启动。

未决确认随候选树保留。显式重建得到实际内容变化的候选后，可在新快照独立审核。同一谱系的一次返工预算仍延续。

返工以初审的固定集成提交 I 独立检出，通过 I 到 I′ 的实际路径差异核对原卡、目标规则和其他成员已交付路径。一个集成交付谱系只允许预约一次，重复审核、返工子版本和显式重建沿用该谱系。I′ 发行后单独使用原完整命令及原审核席位复验。已有修订的谱系重建时保留经核对的修订增量，遇冲突直接显示原因。最终回执依然从明确的 `integration accept` 操作获得。

## 阶段 3 技术验收

2026 年 11 月 18 日，Astra 主窗口验收产品提交 `d27e69fbb2f4863165ee8316736ef366f3622a2b`。完整 `verify` 五步通过，包括 3,375 项单元测试和 72 项离线回放。[完整检查日志](../../Multi-agent-acceptance/phase-three/T61/commit-verify.log)

- **并行与集成。** T60 实机样例中的两次 Claude 实施重叠 6.299899 秒。一个基于上游候选的后继任务采用合成实施，三个成员集成为 `029b40e`。工作台从固定候选执行 `build` 和 `test`，经真实 Claude 单审后签发样例接受回执。目标前移后，旧版本再次申请接受被拒绝，重建生成了新集成 ID。新基线上的完整重测、复审和接受另由真实 Git 配合假审核 CLI 的回归验证。[实机原件](../../Multi-agent-acceptance/phase-three/T60-native-4e305362dbd1/result.json) · [新基线回归](../tests/unit/test_integration_verification_cli.py)
- **返工与复审。** T61 用合成初始实施稳定生成了 `draft` 缺陷。真实 Claude 初审在 `frontend/status.js:1` 报告 P0，工作台从 `5dc3045` 调用 Claude 返工一次。新候选 `599f4a8` 是它的直接子提交，仅将该文件改为 `ready`。原有序 `build`、`test` 在修订前后均由真实 Codex 命令运行时执行，四次均退出 0。原 Claude 审核席在新快照上复审通过，旧审核签发新候选接受回执的请求被拒绝。实际报告模型为 `claude-sonnet-5-5`，生效推理档位未确认。[实机原件](../../Multi-agent-acceptance/phase-three/T61-native-d27e69fbb2f4/result.json) · [主窗口独立核验](../../Multi-agent-acceptance/phase-three/astra/t61-independent-readbacks.json)
