# 产品任务卡与基线规则（v1）

T28 提供输入契约，T29 提供符号路径判定和单执行者派发解析。工作树、状态存储、模型与测试执行器由后续卡实现。开发期任务卡继续使用 `scripts/verify.sh --list` 的五个名称，产品任务卡的 `test_refs` 则只对照所选基线 `.multi-agent/rules.yaml` 的 `commands`。

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

这些模式可以指向将来新增的文件。派发求写入范围与规则语言的交集，不能用当前存在文件枚举，也不能把 `frontend/**` 当字面路径传给 fnmatch。各个命中路径/任务类型的 provider 与 allowed_models 限制同时成立；不兼容就交回拆卡。实现角色默认、用户覆盖、有序备选链必须满足此集合。审核角色独立选择，前端实现规则不禁止 GPT 审核。

输入模块只保存保护模式；T29 的派发解析先拒绝与保护模式相交的声明范围，但仍不判定实际交付。后续校验需独立检查新建、未跟踪、删除、重命名两侧及工作站大小写/Unicode 别名，不允许受保护文件变更通过。不把“提示模型不要改”当权限隔离证据。

## 冻结与证据

`freeze_project_rules(repository, baseline)` 解析指定完整提交，从 Git 对象读取规则，不读取工作树。规则必须是 mode 100644 或 100755 的普通 blob；symlink、gitlink、目录、缺失规则、超限或非 UTF-8 均拒绝。Git 读取复用阶段1的受限 argv helper，不启用替换对象、外部 diff、hooks 或 lazy fetch。

返回 `FrozenProjectRules`：Git common-dir 仓库标识、完整 baseline_commit、blob_oid、raw_text 和不可变 ProjectRules。`rules_hash` 为原始 UTF-8 字节 SHA256，注释、换行和空白变化都改变 hash。任务重试/返工应复用已持久化的同一个冻结对象，不能重新读取候选或脏工作树规则。存储与恢复将在后续卡落地。

这些配置都是请求/约束，不记录为运行时实际模型或 effort。产品实际配置与用量仍仅根据运行时报告保存；缺少报告时标为未确认。


## T29：符号判定与单执行者派发

`core.path_patterns` 提供 `normalize_path`、`path_matches`、`patterns_intersect` 与 `patterns_cover`。模式和实际路径均先 NFC+casefold，再解释 glob；大小写/Unicode 别名视为同一个路径。`?` 指一个规范化后的码点，例如 `ß` 规范化为 `ss`，故 `?` 不允许这个路径而 `??` 允许；`ﬁ` 和 NFD 组合也按同一规则处理。后续实际交付必须调用相同判定，不能先允许原字形再按另一套语义做保护检查。

符号算法在所有非空、无空段的路径语言上判断相交或覆盖，不依赖文件是否已存在。覆盖取规则模式并集；只有确实存在未覆盖路径时才启用 default_implementation。由于别名同路径，规则 `frontend/**` 同时覆盖范围 `FRONTEND/**`，不会额外套用默认提供方。

每次相交/覆盖搜索最多出队 `DEFAULT_STATE_BUDGET=100000` 个状态，自动机最多 `MAX_NFA_STATES=4096` 个状态。搜索公开参数 `state_budget` 可用于确定性测试；超限抛 `PatternBudgetExceeded`，没有近似允许结果。此错误直接向调用边界传播。

`resolve_dispatch(card, frozen, *, task_type=None, requested=None, fallbacks=(), unavailable=frozenset())` 处理已校验并通过引用检查的任务和规则：

1. 任一声明范围与 protected_paths 相交就抛 `ProtectedScopeError`，要求缩小写入范围；实际交付检查仍不可省略。
2. 所有相交 implementation 规则按索引顺序约束同一个执行者。存在未覆盖范围时，将 default_implementation 加在末尾；没有默认则抛 `RuleConflict`。
3. provider 必须一致，allowed_models 求交；显式 task_type 必须已配置，并继续约束 provider/模型。跨前后端不同提供方或空模型交集要求拆卡。任务类型不提供默认配置。
4. 显式 requested 必须在交集内，否则立即抛 `RuleConflict`。无显式选择时，按上述顺序取第一个处于公共模型集合内的 default_model；若全部默认都不合，按首条配置的 allowed_models 顺序取首个公共模型，使用该配置的 default_effort，并记录此原因。
5. 自动主选这一步不排除 unavailable。主选确已由调用方报告不可用后，只依次尝试用户传入的 fallbacks；不擅自遍历公共模型集。备选违反规则或已知不可用会被跳过并留记录。未知可用性不触发备选。全部耗尽抛 `NoImplementationAvailable`，其 selection_history 保留每次原因。

ImplementationChoice 的 provider/model/effort 都是请求值。规则没有 effort 白名单，显式档位是否受运行时支持由后续执行边界核验。DispatchDecision 保存选择、原因、历史、命中规则索引、是否使用默认、task_type、原 requested、传入的 unavailable_models，以及任务/规则 hash；这些字段不声称已经调用过模型，不能作为 reported_model/reported_effort。实际运行仍需环境检查。

这套规则只解析实现及返工角色，不修改审核席位、只读权限或审核模型选择。前端被派给 Claude 后，仍可将相同候选交给 GPT 单审或既有双审。
