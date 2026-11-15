# 阶段 1 真实复验记录（T-0022）

2026-11-15，固定修复产品上的受影响原生审核和功能复验已完成：重新采集 codex-only、dual 各八个主回合，共 24 个首轮席位，全部成功；真实双席取消与仅失败席补跑、一次交叉确认均取得证据。T19 的八份 Claude-only 成功报告通过复用审计，仍保留旧产品、时间及轮次来源。

官方插件八格仍阻塞，人工质量标签仍为空。此记录支持本卡的原生功能复验结果，不宣称完整四模式质量比较已完成，也不给单审/双审优胜结论；阶段 1 整体是否通过仍交原窗口审核。

## 候选、授权与证据位置

| 项目 | 固定值 |
| --- | --- |
| 任务卡 | `tasks/T-0022-phase-one-live-recheck.json`，依赖 T20、T21、T23 |
| 本次开工 HEAD | `7a8019cbac003d004cf1030ed0225b55e89c2102` |
| 被测产品 SHA | `adbf4928b057769bc62b7b0a51c5a248e0e236b7` |
| T20 测试同步修复 | `97aa33586215180bad948667b308c4053a8a69a9` |
| T21 Codex 隔离修复 | `875e17da3888deb388ae3ad316e5baffa843f4de` |
| T23 预检清理证据修复 | `adbf4928b057769bc62b7b0a51c5a248e0e236b7` |
| 旧 Claude-only 执行产品 | `87045fc6c138566d86d11b46e3b2744caf97b9b8` |
| CLI 实际版本 | Codex `codex-cli 0.169.0`；Claude `2.1.325 (Claude Code)` |
| 请求配置 | Codex `gpt-6.1-sol/high`；Claude `claude-opus-5-5/high`；timeout 180 秒、grace 2 秒 |

用户已授权修复后补跑真实验收。执行器记录 argv、UTC、退出码、产品 SHA 与输入/输出哈希；子进程使用参数数组，无 shell 拼接。模型复验期间没有修改产品源码、提示词或目标程序，也没有对真实进程注入清理故障。

以下相对证据路径均以新的仓库外目录为根：

`/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/`

主要入口是 [run-index.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/run-index.json)、[functional-index.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/functional-index.json)、[runtime-audit.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/analysis/runtime-audit.json) 和 [matrix-summary.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/analysis/export-001/matrix-summary.json)。它们定位对应 command、正式 show/history、原始流、执行时间和资源观测。原始材料保留在本机外部证据目录，不复制认证内容进仓库。

最终稳定数据库保存到 [final-db/reviews.sqlite3](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/final-db/reviews.sqlite3)，备份绑定见 [backup.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/final-db/backup.json)。T22 总封存清单位于 [T-0022/evidence-manifest.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/evidence-manifest.json)，覆盖旧 `875e17da` 准备与新候选材料，排除清单自身；其最终文件数和自身哈希由收尾回报绑定，不在这里预填。产品与文档提交 SHA 由候选目录的 [completion.json](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/completion.json) 记录。

早先绑定 `875e17da` 的 T22 准备记录、gate 和 source-audit 保持原样；该阶段模型请求数为 0。最终候选另建目录、重新执行 gate 和复用审计，没有把旧准备证据改标为新产品执行。

## 冻结输入与旧 Claude 报告复用

八例继续使用 T19 的同一 manifest、snapshot IDs、两侧源码、需求和验收条件；六例 development、两例 heldout，没有根据结果调提示词。初审模型输入不含 oracle、期望发现数或另一席报告。确认轮次按既有协议只追加对方指定阻塞项，功能回合不混入首轮基准。

| 绑定项 | SHA-256 |
| --- | --- |
| `benchmark-inputs/manifest.json` | `a4da5df0fb2209dfa6e97cfac4733dfbbe75618e904d98054fb01b0dbf1c91f9` |
| 初审 prompt（`review-v1`） | `8af7a9e4bca3d1451e784355c9f5adf9146d00fbd15380bfb00ed0422cc30135` |
| 确认 prompt（`confirmation-v1`） | `5838f23c52018a8cd5b94012b78c8d48acddd33e58bca9f8649049754b39c27d` |

[最终复用审计](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/preparation/source-audit.json) 以 T19 的 `final-db/native.sqlite3` 作只读稳定来源，逐例核对 canonical snapshot、可见输入 hash、bundle 字节、prompt/schema、请求配置、原始解码结果、报告哈希及 show/history/provenance。八轮旧预检均是版本与登录检查退出 0，没有取消、错误或未知清理证据。

T23 改变异常启动、收尾、取消和重试门槛。正常 `_collect` 的启动/读取/返回/终止语句、Claude 请求与报告处理，以及 runner 的冻结输入生成和模型启动后的处理保持等价。T21 新增的 secret-only 业务处理也逐份验证不改变这些旧成功报告。因此八份可复用；它们仍归属 `87045fc6…`、原 2026-11-15 时间与原 round ID，并非在 `adbf4928…` 上新执行。T19 受污染的 Codex 参与格没有被复用。

## Codex 原生隔离门槛与真实运行

[新候选 gate](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/isolation-gate/summary.json) 于 `2026-11-15T22:43:22Z` 启动，模型请求为 0，14 项均符合预期：

| 实际原生读取场景 | 结果 |
| --- | --- |
| 冻结文件；切到外部 cwd 后仍按绝对路径读取冻结文件 | 2 项可读，退出 0 |
| 真实用户目录合成哨兵；相邻目录；其他席位报告/日志；数据库/答案哨兵；隔离认证材料 | 拒读 |
| 越界 symlink、指向用户目录的 symlink、父目录；改变 cwd 后读外部或用户目录 | 拒读 |
| 上述拒读项合计 | 12 项退出 1，实际报告 permission denied |

gate 记录生效配置与来源、`requirements:null`、实际 `account.type=chatgpt`、退出和资源删除；版本与 App Server 的三个已记录进程均退出 0。合成哨兵和隔离目录均已移除。只记录合成内容及认证文件的路径/权限/存在性，不打印实际秘密。

随后先完成 B01 Codex 单审和双审冒烟，再扩展八例。最终 runtime 审计覆盖 19 份索引导出，19 pass、0 fail、0 unknown；对应 18 个不同 round、29 个不同 attempt，28 成功、1 取消。19 份导出包含取消前后两个时间点，不能当作 19 个不同 round。

所有 Codex 回合先完成配置审计再 `turn/start`；实际策略与 gate 匹配，`thread/start` 顶层报告 `instructionSources=[]`，原始流未出现 MCP server 活动。独立审核、失败席补跑及确认入口均包含在审计中。38 个不同 Codex 预检/审核隔离 owner 有真实权限采样（目录 0700、认证/配置文件 0600）、移除记录及最终不存在观测；没有以模型终态代替 OS 退出或资源清理。

这里的隔离结论针对 Codex 的已配置审核边界。原生系统运行目录仍是 `:minimal` 的明确例外；本次 `command/exec` 拒读证据不外推为所有宿主 RPC 都有相同权限。日志未出现某次外读也不单独证明它会被拒绝。Claude 初始化仍列出 builtin skills/plugins，不能声称 Claude 全部隐式上下文均被隔离。

## 首轮矩阵与评分状态

16 个新主回合的 24 个席位均 succeeded，原始终态与独立退出证据一致。加上八个旧 Claude-only 格，原生导入为 24 completed；官方插件八格 collection status 为 blocked，导入及原生 scorer 中为 not_run。

下表数字是原始 finding 条目数，不是人工确认的有效问题数；dual 的两条也不代表两个不同缺陷。

| 样本 | Codex-only 新执行 | Claude-only 旧报告复用 | Dual 新执行 | 官方插件 | 三种原生模式评分 |
| --- | --- | --- | --- | --- | --- |
| B01 | 成功 / 1 | 成功 / 1 | 两席成功 / 2 | blocked | pending |
| B02 | 成功 / 1 | 成功 / 1 | 两席成功 / 2 | blocked | pending |
| B03 | 成功 / 1 | 成功 / 1 | 两席成功 / 2 | blocked | pending |
| B04 | 成功 / 1 | 成功 / 1 | 两席成功 / 2 | blocked | pending |
| B05 | 成功 / 0 | 成功 / 0 | 两席成功 / 0 | blocked | scored，空报告自动计数 |
| B06 | 成功 / 0 | 成功 / 0 | 两席成功 / 0 | blocked | scored，空报告自动计数 |
| B07（heldout） | 成功 / 1 | 成功 / 1 | 两席成功 / 2 | blocked | pending |
| B08（heldout） | 成功 / 1 | 成功 / 1 | 两席成功 / 2 | blocked | pending |

| 样本 | 新 Codex-only round | 新 dual round |
| --- | --- | --- |
| B01 | `c5d7b960580843278c9f138221e6d231` | `8339749297374b14a86c4e07b97abad9` |
| B02 | `2a7b033c63eb40a69437f40b6f87ef9f` | `c0bf8bd4410a4e60afda1d2a8fcd735c` |
| B03 | `3cd1a9666f72470d9f40dfe09d28aa9d` | `8a39127a59be4b999fe9f5128e3e768f` |
| B04 | `54e31d6c5f7c49118b8d2145d2a58d94` | `b3a6daf112e648ecbbee5ec1d0884444` |
| B05 | `ecfe382b744f4722b3b9691b8563dc4b` | `4116d19fb5524870b67ab85b253458ef` |
| B06 | `b84c557347954c109dc42c66eea4f3ad` | `a33e2a7c66a2414f9159930848756f85` |
| B07 | `58df1832397741b18c2a1f38152a53cc` | `0c6724494a0144d1adef23136b118df2` |
| B08 | `295ba99bcac34b76aebf55697e962037` | `80bcadec6b5e4a728f209aa9331e5bea` |

八个旧 Claude round、全部 snapshot IDs、原产品/日期及 source paths 保存在复用审计和 `analysis/export-001/provenance.json`，不重新分配执行身份。

正式 scorer 输出为 **18 pending、6 scored、8 not_run**；`judgments.empty.json` 没有人工标签。六个 scored 格仅来自 B05/B06 三种原生模式下的空报告，不能说人工已确认质量。有 finding 的十八格仍待与具体报告 hash、snapshot 绑定的人工判断，模式整体质量指标 pending，recommendation 为 null。结构引用校验或两模型 agree 均不代替这种判断。

[插件只读复查](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/preparation/plugin-availability.json) 的已安装列表为空、命令退出 0，本次没有安装插件或改永久设置。这里缺少的是待比较的官方插件运行条件；不与 Claude init 报告的 builtin 上下文混为一谈。

## 时间、用量和实际报告值

[用量汇总](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/analysis/usage-summary.json) 只统计不同首轮席位最后一条 Usage，功能性取消、补跑、确认不参与。先按模式/provider/字段分组，再跨不同首轮求和；不累加重复累计通知，也不将 `total`、`last`、缓存分项和父项互相相加。

| 模式 | 整条首轮命令 wall time 合计 | 已报告用量 | 评分边界 |
| --- | ---: | --- | --- |
| Codex-only | 133.852 秒 | 8/8 席非空 counters；`total.totalTokens` 合计 283258 | 6 pending / 2 空报告 scored |
| Claude-only（旧来源） | 91.741 秒 | 8/8 席非空 counters；input/output/cache-creation/cache-read 为 48/7653/17808/136589 | 6 pending / 2 空报告 scored |
| Dual | 127.382 秒 | 16/16 席非空 counters；Codex totalTokens 265738；Claude 四项为 48/7511/17930/136589 | 6 pending / 2 空报告 scored |
| 官方插件 | null | 未运行，缺失值不填 0 | 8 not_run |

矩阵的 16 个 Codex 席实际模型均未确认；16 个 Claude 席（含八个旧席）均实际报告 `claude-opus-5-5`。32 个原生席的实际 effort 均未确认，请求的 high 没有代填。provider 的用量字段口径不同，运行时客户端费用估计也不是核验后的账单。上表保留旧/新执行来源，不能据这些时间或 counters 单独推导模式优劣。

`export-001` 的首次外部导出因脚本把席位 execution 时间误与整条命令 wrapper 时间比较而失败，原记录保留。修正外部汇总脚本的比较口径后，`export-001-execution-2.json` 记录退出 0；没有改产品、报告或旧证据。输出核验记录确认五份导出文件哈希一致、32 个评分条目及零人工标签。

## 真实并发、取消与仅失败席补跑

八个主 dual 回合均有原始模型启动、session/terminal 时间区间重叠及同时 running 的观察记录。**运行中没有捕获正式 `show.assessment`**：当时保存的是 attempt 状态；“齐全前 assessment 应为 incomplete”是基于这些状态和既有状态逻辑的推导，不记作实测 show 结果。

[取消/补跑核对](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/analysis/functional-cancel-retry.json) 对应独立功能 round `17770ddaeeee4fe0ad753309eaabb1f8`，第一次场景尝试即命中，无第二次尝试：

1. `2026-11-15T22:54:42.950831Z`，只读 DB 观测到 Claude `reviewer-2#1` 已 succeeded、exit 0，Codex `reviewer-1#1` 仍 running、exit 未知；仅向自有控制器 PID 35957 发送一次 SIGINT。
2. 正式取消后 show/history 实际报告 round 和 assessment 均 **incomplete**。Codex attempt 1 为 failed/cancelled、exit 0；Claude attempt 1 的已保存报告与信号前逐字相同，没有被重新取消或覆盖。
3. 既有 retry 只为 Codex 新建 attempt 2，在同 snapshot、模型/effort、prompt 下 succeeded、exit 0。Claude 的 attempt 数仍是 1，报告及全部记录不变；Codex 旧 attempt 不变，旧 history 是补跑后 history 的完整前缀。
4. 补跑后正式 show 为 ready_for_assessment，assessment 为 user_decision。新生 Codex 隔离 owner 的权限与清理再次取得记录。

原始 SIGINT 观察、取消后的 show、补跑命令及补跑后 show 分别在 `commands/functional-dual-cancel-1/`、`functional-dual-cancel-1-history/`、`functional-dual-cancel-retry/`、`functional-dual-cancel-after-retry/`。这组功能数据不替换 B01 主矩阵首轮。

## 一次真实交叉确认

[确认核对](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/analysis/functional-confirm.json) 使用主矩阵 B01 双审父轮 `8339749297374b14a86c4e07b97abad9`，创建子轮 `36113918706242c59a454d49de200ddf`。两席各有一个对方 P1 目标，双方均成功、exit 0，分别返回 agree；这只是模型确认结果，不是人工有效性标签。

确认过程中由自有控制器 PID 36237 的 PID/PPID 子孙关系定位 PID 36244、36260，再以 `lsof` 读取各自当前 cwd。两份真实 `confirmation-targets.json` 在核对 snapshot、冻结文件及 cwd 前后归属后只读复制，不是根据脱敏日志里的 `<WORKDIR>` 猜路径。

[cwd 采集记录](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/commands/functional-confirm/owned-cwd-captures/c9fe37c2056d4d33a43eaf8f814f1270/capture.json) 保存两侧 bundle 和 targets 哈希。两份 targets 分别精确匹配对方原始 P1 条目，没有自己的意见或其他目标。输入提供是否正确由这些文件及哈希判定，不以模型关于文件是否存在的自述代替。

父轮的报告、attempt、history、execution 均保持；父子 snapshot 相同且关联正确。子轮正式 show 为 ready_for_assessment，其独立 assessment 字段为 null，不能描述为自动批准父轮。再次 confirm 真实退出 2，错误为已存在确认轮，数据库 round/attempt 数未增加。本次没有失败确认席，因此没有执行确认重试。

## 完整检查与封存保持

[最终检查命令](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/commands/final-verify/command.json) 记录 `scripts/verify.sh` 于 `2026-11-15T22:59:22.893351Z` 至 `23:00:22.022031Z` 执行，退出 0。前后 HEAD 均为 `7a8019cb…`，产品绑定 `adbf4928…`，stderr 为空。

| 检查 | 实际结果 |
| --- | --- |
| lint | 通过 |
| format | 81 文件已符合格式 |
| typecheck | 33 个源文件无问题 |
| unit | 702 passed，58.45 秒 |
| replay | 16 passed，0.37 秒 |

完整 unit 包含 T20 的慢启动、错误 PID 和终止取消门闩回归，T21 的隔离/认证/脱敏契约，以及 T23 的预检异常、重复外部取消、未知收尾阻止普通 retry/--recover、创建中取消和部分认证资源删除失败回归。代表命名测试包括 `test_cancellation_during_termination_after_slow_start_still_kills_and_reaps`、`test_invalid_ready_pid_is_not_masked_by_cleanup_cancellation`、`test_repeated_external_cancel_during_preflight_waits_for_unique_cleanup_outcome`、`test_real_cli_preserves_unconfirmed_preflight_and_rejects_all_retries`、`test_environment_creation_cancel_waits_for_real_process_start_cleanup`、`test_partial_auth_creation_and_removal_failure_keeps_unknown_cleanup`。这些故障分支是合成回归证据，没有对真实模型进程故障注入。

[收尾保存审计](/Users/owen/Desktop/Multi-agent-acceptance/T-0022/candidate-adbf4928/analysis/source-preservation-final.json) 确认 T19 的 389/389 个已封存文件大小及 SHA-256 匹配，原清单自身哈希仍是 `ed204a4907f5e26edaf15e30f2219744d07481938efce42ac5ff7037bf4af9c6`。旧 `875e17da` 准备索引中的 17/17 文件匹配，旧 gate 的十份文件清单也无缺失或额外文件；旧 source-audit 另与新审计保存的原哈希锚点一致。旧准备索引本身仅记录当前 digest，没有独立历史 digest，因此不把该索引自身描述为另有历史封存锚点。

| 本次关键产物 | SHA-256 |
| --- | --- |
| `isolation-gate/summary.json` | `41f8f6659c352e1d60546b80589340c9affb9c4ab51028354417a054410bd904` |
| `analysis/runtime-audit.json` | `c212b947a6f0336e33afd08d48fc7969519f16f92cb9f7a99927a674cd3a5adf` |
| `analysis/export-001/matrix-summary.json` | `7a1ac2e45c830cea1250862053ea0d7ecff715e5153c8ad41fe6f04e58ea342f` |
| `analysis/functional-cancel-retry.json` | `2b031248008993c62796af9d47785ce09596d14423fbe553648e69a5551e1f06` |
| `analysis/functional-confirm.json` | `69bec9da8db4917fcd2287aa49fbb949192a8208a2128abb5f39ad00d9879f0e` |
| `analysis/source-preservation-final.json` | `b6efe54c593cbd760528a5a7e9cd44f362cdf003ad35320e6cbbea9d88dfac3f` |
| `commands/final-verify/stdout.txt` | `67061fba4672278609a36ca235f3e357b0fa277e3e8aaef96bd28e7fc04497e3` |

## 任务卡八项验收对应

| 验收项 | 结果及可复核证据 |
| --- | --- |
| 1. 修复提交、固定候选与准备隔离 | T20/T21/T23 独立修复已集成；本次命令绑定 adbf4928，新旧准备目录与产品身份分开；最终完整检查通过。 |
| 2. 原生权限 gate 与冒烟 | 新 gate 14/14、B01 两种模式成功；后续配置/指令来源/MCP/owner 审计通过。结论限于已测 Codex 边界。 |
| 3. 八例受影响模式及旧 Claude 来源 | codex-only/dual 各八例全部重新采集；旧 Claude 八例复用审计通过并保留 87045fc 原身份；plugin 八格明确 blocked/not_run。 |
| 4. 真实取消与失败席补跑 | 首次即命中 Claude 已成功/Codex 仍运行；一次自有控制器 SIGINT，仅 Codex attempt 2 补跑，旧报告/尝试/history 保持。 |
| 5. 真实确认与唯一性 | 父子两席成功、同 snapshot、实际 cwd 的两份 targets 精确匹配对方 P1，父不变，第二次 confirm 退出 2 且计数不变。 |
| 6. 实际证据、用量和结论边界 | 未报告模型/effort 保持未知；首轮用量不重复累计；人工判断为空、官方插件未运行，未给优胜结论。运行中 incomplete 明确为推导，取消后 incomplete 是实测。 |
| 7. 完整 verify 与原失败记录 | 五步通过，702 unit/16 replay，退出 0；T19/T20/T21/T23 原失败材料保留，未调整或削弱检查。 |
| 8. 范围、封存与回报 | 外部索引/审计/哈希可定位，T19 389 与旧准备 17 文件保持；本次仓库仅任务卡授权文档更新，文档提交 SHA 由整合提交后回报，接受原窗口独立审阅。 |

本报告和离线审计由同提供方代理分工核对，不称为跨提供方人工质量评审。官方插件条件和人工判断仍是阶段 1 整体比较的明确未完成项。
