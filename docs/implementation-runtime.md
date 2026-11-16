# 独立写模式适配器（离线合同）

`ImplementationRuntimeAdapter`构造时绑定T31的不可变WorkspaceRecord，只接受WORKSPACE_WRITE会话。默认RuntimeAdapter仍只接受READ_ONLY。两者共用传输、原始日志脱敏、规范化事件、终态/退出、取消和清理；本模块不创建工作树、持有实现锁或推进任务状态，上层必须保持T31 lease并核对完整所有权。

start_session只登记会话，不启动CLI。cwd必须解析为记录root，root必须是规范化的真实目录，dev/inode必须等于T31的root_identity，不能在start时重新捕获替代基准。打开日志前及Codex版本/配置/账号等待之后、服务启动与thread/turn提交前再次核对；日志不与工作树重叠且不在Git管理目录内。默认check_environment仍在独立临时目录使用原只读配置；它提供CLI/账号/模型目录证据，不能当写权限验收。

## Codex

固定profile为`multi-agent-owned-implementation`。文件规则只有`:minimal=read`、owned root为write、root/.git/admin/common-dir为deny；network=false，不继承宽权限profile。common/admin都是存在的规范化目录，`.git`必须是普通控制文件，其deny键保留原入口而不跟随symlink。禁止Git元数据意味着模型不能使用Git读写，提交由宿主生成。

复用原临时HOME/CODEX_HOME、最小官方ChatGPT auth、禁用用户指令/skills/MCP/插件发现、--strict-config、实际config layers/effective配置/requirements严格null/账号type=chatgpt审计。已知optional-null字段按宿主期望的固定profile映射，不能根据回包选择更宽规则；未知额外字段及非空额外权限仍拒绝。thread/start和turn/start不传legacy sandbox字段，避免覆盖named policy。

新增路径策略在分配私有隔离目录前校验，失败不遗留owner；隔离身份、进程退出和资源清理继续沿用已存在的生命周期。日志profile取实际选用配置，不硬记只读名。配置审计通过只说明回包符合所求配置，新的write/deny行为尚未实测。

## Claude

写入口固定增加restricted、strict-mcp-config、acceptEdits，工具名单为Read/Grep/Glob/Edit/Write；保留safe-mode、permission-prompts none、无会话持久化、显式model/effort及stream-json。环境继续移除ANTHROPIC_API_KEY与ANTHROPIC_AUTH_TOKEN，不改用户登录。没有额外目录、Bash、WebFetch、settings、permission handler或bypass。

首次实际消息必须为唯一system/init，在转成SessionStarted前核对cwd、tools集合、permissionMode、版本2.1.325、空mcp_servers、空additional_directories以及apiKeySource=none。无schema时恰好五工具；结构化结果时还必须有StructuredOutput，恰好六个。重复/缺失init或任何核对不符均产生adapter.implementation_policy错误；EOF无init也不接受。记录中的implementation_policy_reported只保存运行时报告值。既有safe-mode录制仍有内置plugins/skills，所以不假设它们全部为空。

只有文件工具意味着可以创建/编辑文件；此路径不提供shell、自测命令、删除、rename、权限位或二进制构造能力。不承诺这些任务都能在当前工具范围内完成，后续runner应如实报告阻塞。项目正式验收测试由独立测试执行器承担，不能因缺少Bash就当作已测试。

init审计发生在CLI启动之后，不是宿主与CLI之间的权限批准握手，不能证明审计失败前没有工具活动；真实约束依赖受限模式本身和后续实机证据。当前完整acceptEdits工具组合尚无本机录制，未知报告先拒绝，不为通过而猜值。

## 字段依据和证据状态

| 用法 | 原始依据 | 状态 |
| --- | --- | --- |
| Codex named filesystem read/write/deny、network、default_permissions | protocol/codex/config.schema.json：FileSystemAccessMode/PermissionProfileToml等；config-schema-source.json绑定官方rust-v0.169.0 | 结构来自同版本官方schema，写行为未确认 |
| config/read、configRequirements/read、account/read、thread/start、turn/start | protocol/codex/schema/v2对应Params/Response；沿用既有审核审计 | 既有只读行为已录制，写profile组合待验证 |
| restricted、strict-mcp-config、permission-mode acceptEdits、tools、safe-mode | protocol/claude/help.txt，2.1.325；本次核对本机版本与快照相同 | 快照，不是写行为实测 |
| Read/Grep/Glob/permissionMode/cwd/version/MCP/额外目录/auth来源 | tests/fixtures/runtime/claude-2.1.325/claude-readonly.jsonl | 既有只读init已报告；acceptEdits组合未确认 |
| StructuredOutput | 既有claude-structured.jsonl与runtime-protocols事实表 | 既有只读结构化结果录制；写组合未确认 |
| Edit/Write工具名和创建/覆盖行为 | 本机help示例含Edit；[官方工具参考](https://code.claude.com/docs/en/tools-reference#write-tool-behavior)明确Write | 文档，写组合待本机验证 |

[官方CLI参考](https://code.claude.com/docs/en/cli-reference)还说明EndConversation可能不因tools列表省略而移除。本机既有2.1.325只读init只报告三工具，不能据此断言隐藏工具不存在；新入口核对报告中的集合，异常扩展在T33探测时核实，不能据init数组单独宣称完整工具沙箱。

Codex实际模型仍未确认；Claude实际模型来自结果modelUsage；双方实际effort未确认。请求配置、运行报告、模型终态、实际进程退出分别保存。模型成功后服务清理产生信号退出不自动抹去成功终态，但这也不是任务验收通过。

T32只含离线假CLI测试，尚未连接产品执行CLI。随后T33提供默认只输出计划、实际执行需明确选择的探测工具，复用同一产品policy与审计路径。未完成该证据前，不能用阶段1只读记录或假CLI绿灯宣布写隔离通过。
