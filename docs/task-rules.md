# 产品任务卡与基线规则（v1）

T28 提供输入契约；派发限制求交、工作树、状态存储、模型与测试执行器由后续卡实现。开发期任务卡继续使用 `scripts/verify.sh --list` 的五个名称，产品任务卡的 `test_refs` 则只对照所选基线 `.multi-agent/rules.yaml` 的 `commands`。

## 任务卡入口

`multi_agent.cli.task_inputs.parse_task_card(text)` 用 `schemas/task-card.schema.json` 校验 JSON，返回 `core.tasks.TaskCard`。包内 `src/multi_agent/task-card.schema.json` 是指向唯一源的相对符号链接；uv_build 的 sdist/wheel 把链接解引用成普通资源，运行时用 `importlib.resources` 读取，不依赖当前 checkout 路径。JSON Schema、PyYAML 是产品依赖，对应类型存根是开发依赖；检查规则保持原样。

JSON 重复键、未知字段、错误类型及不安全路径在 CLI 边界拒绝。所有输入限制为 1 MiB UTF-8 文本，不允许 NUL 或非法 Unicode。`proposed_tests` 只保留建议，不会添加到认可命令，也不会在此模块执行。所有数组在 core 值对象内为 tuple，`to_dict()` 返回独立 JSON 容器；`task_hash` 是规范化完整任务内容的 SHA256，包括未决问题和提议命令。

`validate_task_graph(cards, integrated_ids)` 检查重复 ID、未知依赖与环。依赖属于 cards 的 ID 或 integrated_ids 即为已知；只要卡片包含在 cards，哪怕其 ID 同时标为已集成，也参与环检查。此函数不证明任务已经集成，不验证用户声称的 integrated_ids；后续持久化层必须给出集成证据。

`validate_task_references(card, frozen_rules)` 要求卡片基线等于冻结规则基线、所有 test_refs 已命名、所有 blocks_dispatch 问题有 resolution。它不决定执行角色，也不代替后续派发前对依赖状态的核对。

## 规则格式

```yaml
version: 1
commands:
  frontend-unit:
    executable: pnpm
    args: ["--filter", frontend, test]
protected_paths:
  - ".multi-agent/**"
  - ".claude/**"
  - ".mcp.json"
implementation:
  - paths: ["frontend/**"]
    provider: claude-code
    default_model: claude-sonnet-5-5
    default_effort: medium
    allowed_models: [claude-sonnet-5-5]
default_implementation:
  provider: codex
  default_model: gpt-6.1-terra
  default_effort: medium
  allowed_models: [gpt-6.1-terra, gpt-6-luna]
task_types:
  frontend:
    provider: claude-code
    allowed_models: [claude-sonnet-5-5]
```

根字段 `version`、`commands`、`protected_paths`、`implementation` 必须存在；后两种配置可选。未知字段拒绝，不忽略拼写错误。`version` 为整数 1。提供方白名单为 `codex` 与 `claude-code`；这些是请求路由标识，不是报告过的实际模型证据。

每条命名命令只有 executable 与 args。命令名/任务类型名匹配 `[a-z][a-z0-9-]*`。executable 为非空字符串，args 为字符串数组（允许空参数），不做 shell 展开。这里仅解析和冻结，后续执行器按程序和参数列表启动；项目可自行命名命令，不硬编码本仓库 verify 的步骤。

每条 implementation 必须有非空 paths 和 provider/default_model/default_effort；allowed_models 可选，省略时仅允许 default_model。默认模型必须在可用模型集合内。default_implementation 同样采用这些配置字段，不带 paths；未给出时保留 None，不推断提供方或模型。task_types 是名称到 provider/allowed_models 的映射；任务类型由后续派发显式参数传入，不擅自改变任务卡 schema。

YAML 采用自定义 SafeLoader，拒绝重复键、非字符串键、自定义对象标签及 alias/merge。隐式布尔仅识别 true/false，on/off/yes/no 保留字符串。未知字段及类型错误在边界抛 ValueError，原解析错误作为原因保留；core/store 不吞异常或重新猜测输入。

## 路径与派发契约

路径必须相对仓库，使用 `/`，大小写保留。普通文件名、段内 `*`/`?`、独立整段 `**` 是 v1 支持的语法。`*` 匹配一个路径段内零个或多个字符，`?` 匹配一个非 `/` 字符；`**` 表示零个或多个完整路径段。路径不得包含空段、`.`/`..`、反斜杠、控制字符、尾随点或空格、任意层级的 `.git`（Unicode NFC 后不区分大小写），也不能使用字符类、brace 或 extglob。精确路径匹配自身；目录子树应显式写 `dir/**`。

这些模式可以指向将来新增的文件。后续派发必须求写入范围与规则语言的交集，不能用当前存在文件枚举，也不能把 `frontend/**` 当字面路径传给 fnmatch。各个命中路径/任务类型的 provider 与 allowed_models 限制同时成立；不兼容就交回拆卡。实现角色默认、用户覆盖、有序备选链必须满足此集合。审核角色独立选择，前端实现规则不禁止 GPT 审核。

本模块只保存保护模式，不判定实际交付。后续校验需独立检查新建、未跟踪、删除、重命名两侧及工作站大小写/Unicode 别名，不允许受保护文件变更通过。不把“提示模型不要改”当权限隔离证据。

## 冻结与证据

`freeze_project_rules(repository, baseline)` 解析指定完整提交，从 Git 对象读取规则，不读取工作树。规则必须是 mode 100644 或 100755 的普通 blob；symlink、gitlink、目录、缺失规则、超限或非 UTF-8 均拒绝。Git 读取复用阶段1的受限 argv helper，不启用替换对象、外部 diff、hooks 或 lazy fetch。

返回 `FrozenProjectRules`：Git common-dir 仓库标识、完整 baseline_commit、blob_oid、raw_text 和不可变 ProjectRules。`rules_hash` 为原始 UTF-8 字节 SHA256，注释、换行和空白变化都改变 hash。任务重试/返工应复用已持久化的同一个冻结对象，不能重新读取候选或脏工作树规则。存储与恢复将在后续卡落地。

这些配置都是请求/约束，不记录为运行时实际模型或 effort。产品实际配置与用量仍仅根据运行时报告保存；缺少报告时标为未确认。
