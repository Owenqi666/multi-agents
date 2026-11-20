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

`check` 依次跑类型检查和组件测试，是任务卡 `proposed_tests` 里的 `frontend-check`。它不在 `scripts/verify.sh` 里，单独运行；提交钩子仍然会跑完整的 `scripts/verify.sh`，所以 worktree 要先 `uv sync`。lint 和浏览器端到端（`frontend-e2e`）随后续页面加入。

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

待 Owen 确认后，在这里记下确认原话和日期。
