# Multi-agent 本地开发工作台

Mac 上的个人开发工作台：复用已登录的 Codex 和 Claude Code，跨模型派发任务，冻结审核输入，把测试和审核证据绑定到具体提交。设计见 `PROJECT_PLAN.md`。本文件与计划冲突时以计划为准，并把冲突指出来。

**当前阶段为 3（并行实现与集成）。** 阶段 2 已由用户正式接收。阶段 3 按 [开发路线](docs/phase-three-development.md) 逐项交付，最终接受或合并由用户触发。

阶段 3 由本窗口的 GPT-6.2 Astra 规划、定接口、分配任务和审核交付，GPT-6.2 Sol 负责实现。按可独立验收的功能拆卡，写入范围互不重叠且没有依赖的任务才并行。优先复用既有逻辑，新增测试聚焦用户操作路径和已确认的回归。提交前完整运行 `scripts/verify.sh`，提交 hook 完整执行时计为同一次检查。

## 不变量

这些规则保护的是"证据可信"和"状态不被旧结果污染"，任何任务都适用。

- **证据只记报告值。** 实际模型、生效档位、用量、改派这些证据字段只记运行时报告的值。拿不到就记"未确认"，不用请求值、默认值或推断值顶替。
- **只有当前尝试能推进状态。** 写终态时确认该尝试仍是当前尝试，检查和写入在同一个事务里完成。晚到的结果只进历史。
- **子进程不经过 shell。** 一律以程序加参数列表启动，不拼接命令字符串。
- **防御只放在边界。** 系统边界只有两处：`adapters/`（两边 CLI 的输入输出）和 CLI 入口（用户输入）。超时、限流、认证失效、畸形输出在那里处理并归入失败分类。`core/` 和 `store/` 信任已校验的输入，出错直接抛，不吞异常，不返回兜底值。
- **不为变绿而改检查。** 不削弱、跳过或删除已有的测试、回放场景和检查配置。确实需要改时，在交付说明里单列原因。
- **受保护配置。** `.claude/`、`.agents/`、`AGENTS.md`、`CLAUDE.md`、`scripts/verify.sh`、`scripts/link-skills.sh`、`scripts/hooks/` 只有在任务卡的写入范围明确包含时才改。

## 目录约定

代码按下表组织。要调整布局，就同时改这里和引用它的 skill。

| 路径 | 内容 |
| --- | --- |
| `src/multi_agent/core/` | 状态机、冻结快照、规则解析、审核汇总 |
| `src/multi_agent/adapters/` | Codex 与 Claude 适配器，唯一直接接触两边 CLI 的代码 |
| `src/multi_agent/store/` | SQLite 持久化，迁移在 `store/migrations/` |
| `src/multi_agent/web/` | 本机回环 HTTP 服务：工作区、作业、资源读回和网页托管。产品动作经 CLI 子进程执行，`environment.refresh` 在服务进程内经 `adapters/` 执行；读取用只读连接；启动时把已有数据库升级到当前结构 |
| `frontend/` | 网页工作台（分工舞台和项目流程页面）。构建产物 `frontend/dist` 不入库 |
| `tests/unit/` | 单元测试 |
| `tests/replay/` | 合成事件回放，场景在 `tests/replay/scenarios/` |
| `tests/fixtures/runtime/` | 两边 CLI 的实机录制，按 `<cli>-<版本>/` 分目录。探测工具录制，不手改 |
| `protocol/` | 本机 CLI 的版本、帮助输出和 Codex schema 快照。脚本生成，不手改 |
| `schemas/` | 任务卡等结构化产物的 JSON Schema |
| `tasks/` | 开发期任务卡 |

## 检查

工具链是 uv、ruff、mypy、pytest。`scripts/verify.sh` 是唯一的检查入口：不带参数跑全部步骤，`scripts/verify.sh <步骤名>` 跑单步，`scripts/verify.sh --list` 列出步骤名。提交前完整跑一遍。

## Skills

真实文件在 `.agents/skills/`，Codex 直接读这里。Claude Code 读的是 `.claude/skills/` 下指向它们的符号链接，两边是同一份内容。链接由 `scripts/link-skills.sh` 创建，克隆仓库或新增 skill 之后运行一次。

| 什么时候 | 用哪个 |
| --- | --- |
| 提交前，或报告任务完成前 | `verify` |
| 写或改 `adapters/`，或用到两边 CLI 的选项、方法、事件、输出字段 | `runtime-protocols` |
| 增改状态、事件类型、失败分类或表结构 | `state-change` |
| 规划工作、把任务交给另一个模型、开始实现一张任务卡 | `task-card` |
| 做新界面或改版之前 | `frontend-design` |
| 做或改首页的分工舞台，或它读取的运行状态 | `handoff-stage` |
| 改了 `frontend/` 的样式、布局或动效，报告完成之前 | `ui-render-check` |
