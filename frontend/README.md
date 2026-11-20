# 网页工作台（frontend）

本机网页工作台的页面：首页是分工舞台，另有从目标到接受的项目流程页面。约定以 `docs/web-api/contract-2026-11-19.md` 为准，T-0069 交付后以 `docs/project-service.md` 为准。任务卡是 `tasks/T-0070-web-pages.json`。

技术栈：React 19、TypeScript、Vite 8、Vitest，包管理用 pnpm（`package.json` 的 `packageManager` 固定版本，经 corepack 取用）。字体 IBM Plex Sans 和 IBM Plex Mono 随页面打包，中文落到系统的苹方；页面不请求任何外部地址。

## 安装

需要 Node 20.19 或 22.12 以上。下面的命令都在仓库根目录的 `frontend/` 里运行。没有全局的 pnpm 时用 `corepack pnpm` 代替 `pnpm`。

```bash
cd frontend && corepack pnpm install
```

## 启动

样张（第 0 步，不需要服务）：

```bash
cd frontend && corepack pnpm specimen
```

然后打开 `http://127.0.0.1:5173/specimen.html`。默认是浅色（Owen 2026-11-20 选定），右上角可以切到暗色，或选「自动」跟随系统外观；舞台下方的按钮和刻度可以逐步查看，键盘左右键也能切步。

工作台页面，模拟模式（不需要服务，回放 `docs/web-api/samples/` 里录好的样例）：

```bash
cd frontend && corepack pnpm mock
```

打开 `http://127.0.0.1:5173/`，默认回放 `accept` 场景；加 `?mock=rework`、`?mock=dual-partial`、`?mock=cancel` 换场景，加 `&at=20` 只回放到第 20 个请求。样例不在默认位置时用环境变量 `SAMPLES_DIR` 指过去。模拟模式只回放样例，提交的作业一律按固定时间线走：排队一次轮询、运行三次轮询，然后给出样例里录到的结果；取消会先显示「已请求取消」，再以退出码 130 结束。它不实现任何业务规则，样例里没有的读取回 404。

开发模式，经由桥接服务：

```bash
cd frontend && BRIDGE_URL=http://127.0.0.1:<端口> corepack pnpm dev
```

`BRIDGE_URL` 是服务启动时打印的地址。开发服务器把 `/api` 转发过去，转发时把 Host 改成服务地址、Origin 改成服务地址，服务不为开发模式放宽检查。

## 构建

```bash
cd frontend && corepack pnpm build
```

产物在 `frontend/dist`，不入库（根目录 `.gitignore` 已有 `dist/`）。

## 检查

```bash
cd frontend && corepack pnpm run check
```

`check` 依次跑类型检查、ESLint 和组件测试，是任务卡 `proposed_tests` 里的 `frontend-check`。它不在 `scripts/verify.sh` 里，单独运行；提交钩子仍然会跑完整的 `scripts/verify.sh`，所以 worktree 要先 `uv sync`。模拟模式的回放测试在找不到样例时跳过。浏览器端到端（`frontend-e2e`）在联调分支合入 T-0069 后加入。

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/api/types.ts` | 约定里的对象形状，只列页面用到的字段；缺的键按 null |
| `src/stage/` | 分工舞台：`derive.ts` 把读回换算成步骤，`relay.ts` 画接力线，`weave.ts` 画织带，`Stage.tsx` 是页面组件 |
| `src/components/` | 状态徽标、diff、日志块、空状态、主题切换 |
| `src/diff/parse.ts` | 解析 git 的 unified diff：重命名、二进制、带转义的路径 |
| `src/styles/` | 颜色、字体和共用控件的样式，亮暗两套 |
| `src/fixtures/specimen.ts` | 样张用的手写样例，按约定的对象形状写；T-0069 合入后改用 `docs/web-api/samples/` |
| `src/specimen/` | 第 0 步样张页 |
| `src/app/` | 页面入口、路由、导航和作业条 |
| `src/api/` | 请求（只走同源 `/api`）、作业中心（轮询、找回、重复提交保护）、读取规则（作业期间不发抢锁读取） |
| `src/pages/` | 首页舞台、目标、项目、计划、阶段、实施、集成、审核、diff、冻结文件、日志、作业 |
| `src/mock/` | 模拟模式：回放录制样例和固定作业时间线 |
| `src/test/` | 组件测试和测试用的假请求 |

## 读取和作业的规则

- 作业状态有 `queued`、`running`、`cancelling`、`finished`、`interrupted`，前三个算未结束。页面打开或刷新时先读 `GET /api/jobs`，找回未结束的作业继续轮询，运行中的作业每秒读一次。
- 有 CLI 作业未结束时，不发抢锁的读取：`task`、`task-test`、`task-review`、`integration`、`stage`、`plan`，任务和集成的 diff，以及这些记录的日志。页面保留上一次读到的内容并写明读取时间；没有读过就显示「作业运行中，结束后可读」。收到 `lock_busy` 也一样处理。抢锁的读取逐个发，不并发。
- 作业期间仍可读、最多每 5 秒一次：`project`、`consultation`、`goal`、`project-plan`、`project-stages`、`review`、`snapshot`、快照文件和快照 diff、`runs`、作业和作业日志。舞台按作业的 `action` 在最后补一格「进行中」，席位用作业参数里的请求，模型一律「未确认」；作业结束后整体重读。
- 提交后到作业结束前，这个按钮显示「已提交，等作业结束」，其他提交按钮也不能点，并给出正在运行的作业链接。服务回 409 `busy` 时跳到 `active_job_id` 对应的作业。会新建记录的动作（再生成计划、创建集成）先确认一次。
- 取消按钮只在 `running` 时出现；`cancelling` 显示「已请求取消」；结束后按退出码和领域状态显示（130 为「已取消」）。退出码 2 且没有 `result` 时显示「被拒绝」和 `stderr_tail` 原文。`error` 为空不算成功。
- 需求、模型输出、日志和 diff 一律按纯文本显示。

## 按钮什么时候可以用

只看读回的领域字段（`next_action`、`phase`、`verification`、`current_eligible` 和阶段门槛），不看 `error` 是否为空。所有提交按钮还要求当前没有未结束的 CLI 作业。

| 按钮 | 页面 | 动作 | 可用条件 |
| --- | --- | --- | --- |
| 检查 Codex / Claude 环境 | 提出目标、计划 | `environment.refresh` | 该提供方还没有成功的环境观测时出现；不会在页面加载时自动发起 |
| 请顾问完善 | 提出目标 | `project.consult` | 写了目标和基线，选了一到两位顾问；两位时提供方不同 |
| 直接确认为目标 | 提出目标 | `project.confirm` | 写了目标和基线（先确认一次） |
| 原样确认 / 按改过的确认 | 需求咨询 | `project.confirm` | 咨询有草稿（`draft` 非空）；文本和草稿相同传 `accepted`，否则传 `edited` |
| 生成计划 / 再生成一版计划 | 项目 | `project.plan` | 选了规划者（Astra 或 Fable）；已有计划时先确认一次 |
| 保存为新版本 | 计划 | `project.revise` | 文档是合法 JSON 且有改动；服务再校验 |
| 领取这个阶段 | 计划 | `project.stage.start` | 计划 `status` 为 `succeeded`，该阶段在 `project-stages` 里还没有记录；卡片复核的模型不能和阶段负责人相同 |
| 恢复阶段 | 阶段 | `project.stage.resume` | 还没有拟卡尝试（带 `prepare: true`），或规划 `next_action.kind` 为 `plan_retry` |
| 开始实施 | 阶段、实施 | `task.run` | `next_action.kind` 为 `run` 且还没有实施尝试 |
| 重试实施 | 阶段、实施 | `task.retry` | `next_action.kind` 为 `run` 且已有实施尝试 |
| 领取子卡 | 阶段（分派） | `project.stage.leaf` | 子卡还没有 `planning`，`unmet_dependencies` 为空；执行者只能选 Terra 或 Sonnet |
| 实施（子卡） | 阶段（分派） | `task.run` | 子卡 `review_ready` 为真且还没有实施尝试 |
| 并行实施 N 张子卡 | 阶段（分派） | `task.run_batch` | 至少两张子卡复核通过、没有实施、没有未满足的依赖 |
| 运行测试 | 阶段、实施 | `task.test` | `next_action.kind` 为 `task_test`；命令名取任务读回里冻结的规则，默认勾选卡片的 `test_refs` |
| 开始审核 | 阶段、实施 | `task.review` | `next_action.kind` 为 `task_review` 且带 `test_attempt`；一到两席，两席模型不同；自动返工默认开，关掉才传 `auto_rework: false` |
| 补跑失败的席位 | 阶段、实施、集成 | `task.retry_review` / `integration.retry_review` | 最后一轮里有席位的最新尝试不是成功；`seat_keys` 取读回的 `seats[].key` |
| 请两席交叉确认 | 阶段、实施、集成 | `task.confirm` / `integration.confirm` | 最后一轮是双席、两席都成功、还没有确认轮、结论不是通过；集成已接受后不显示 |
| 按阻塞意见返工 | 阶段、实施 | `task.rework` | `next_action.kind` 为 `rework`，`round_id` 取 `next_action.review_round_id` |
| 创建集成 | 阶段（直接） | `integration.create` | `review_satisfied` 为真；选了目标分支（先确认一次） |
| 合并子卡 | 阶段（分派） | `project.stage.integrate` | `can_integrate` 为真且还没有集成 |
| 运行测试 / 重新测试 | 集成 | `integration.test` | 没有冲突、还没接受 |
| 开始审核 | 集成 | `integration.review` | 测试通过、还没接受、还没有审核轮 |
| 按阻塞意见返工 | 集成 | `integration.rework` | 最后一轮结论为 `needs_rework`，这条谱系还没返工过，还没接受 |
| 在新的目标上重建 | 集成 | `integration.rebase` | `target_is_current` 为假 |
| 接受 | 集成 | `integration.accept` | `current_eligible` 为真且还没接受；`round_id` 取最后一轮（先确认一次）。资格不够时服务以退出码 2 拒绝，原因在作业页按原文显示 |

## 舞台怎样从读回换算

按约定「接力线的步骤和数据来源」的 12 步表，只画已经有记录的步骤：

- 需求顾问读 `consultation.seats[]`，一到两位依次画在线上，不是盲审。
- 确认目标读 `goal`；项目计划读 `project-plan`，`origin` 为 `pm` 时持有者是你。
- 阶段按 `mode` 分两种。直接模式：拟卡、卡片复核、实施、测试、代码审核，最多一次返工，返工后接复测和复审，线下方用虚线标出「返工后回到测试」。分派模式：拆分，每张子卡的复核和实施画成并行支线，工作台合并，再测试、审核、返工和接受。
- 代码审核去掉 `metadata.kind` 为 `confirmation` 的确认轮。两席时分成两条支线，中间标「互不可见」；一席只画一条线。并行子卡的支线只标「并行」。
- 站点上的模型名只用运行时报告值（`evidence.reported_*`、`runtime.reported_*`、审核席位 `attempts[].reported_*`），为 null 时写「未确认」，请求值另外标「请求」。线的颜色跟着席位：报告值能认出时按报告值，否则按请求值，只用来区分席位，不当作模型证据。

## 织带：纵轴是模型等级

首页舞台用织带（Owen 2026-11-20 选定），接力线留作备选。织带和接力线用同一份步骤数据，只是画法不同：每个席位一条泳道，按等级从上到下排列，同一档里 GPT 在上、Claude 在下。

| 等级 | GPT | Claude |
| --- | --- | --- |
| 顾问与规划 | Astra | Fable |
| 阶段与复核 | Sol | Opus |
| 子卡实施 | Terra | Sonnet |

你和工作台在下方两条轨道上，不分等级。每一步是一列，持有者在自己的泳道上点亮一段；交接是从上一列到这一列的细线，播放时一颗光点沿线走过，交接物胶囊在底部轨道上移动。盲审的两席之间画虚线并标「互不可见」，并行子卡标「并行」。泳道和颜色跟着席位（同接力线），返工记录没有运行时报告的模型，那一格画成虚线。分派阶段返工后，阶段读回会换成子集成，前面的集成由页面另外读回后经 `integrationHistory` 传入。

## 样张确认

样张提交 `ebaabc3`。Owen 在项目线程里的原话：

- 2026-11-20：「首页选浅色吧」（默认主题改为浅色）。
- 2026-11-20：「首选用织带」（首页舞台用织带，接力线作备选）。
- 2026-11-20：「可以」（回复「样张这样可以吗？」，确认样张，可以开始做项目流程页面）。
