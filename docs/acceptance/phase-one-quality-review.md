# 阶段1独立模型质量裁定

当前状态：`native_sealed/plugin_pending`。2026-11-15用户授权独立窗口模型裁定替代本轮原定人工判断，本报告的判断与质量指标明确为`source=agent_reviewed`，不表示真人确认，也不改变正式scorer或历史`user_supplied`记录。

32份原生报告均已逐份审查，含8份空报告；24个finding均为有效已知问题，无额外有效问题、误报或未决。按case+mode去重后，三个原生模式各发现全部6个已知问题，双审有6次重复发现。本轮八例微型合成样本未显示双审增加有效发现，不能据此推断真实仓库中的普遍优胜或统计显著性。

| 分组 | 模式 | 有效发现 | 已知发现 | 额外有效 | 漏报 | 误报 | 重复 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| development，6例 | codex-only | 4 | 4 | 0 | 0 | 0 | 0 |
| development，6例 | claude-only | 4 | 4 | 0 | 0 | 0 | 0 |
| development，6例 | dual | 4 | 4 | 0 | 0 | 0 | 4 |
| heldout，2例 | codex-only | 2 | 2 | 0 | 0 | 0 | 0 |
| heldout，2例 | claude-only | 2 | 2 | 0 | 0 | 0 | 0 |
| heldout，2例 | dual | 2 | 2 | 0 | 0 | 0 | 2 |
| all，8例 | codex-only | 6 | 6 | 0 | 0 | 0 | 0 |
| all，8例 | claude-only | 6 | 6 | 0 | 0 | 0 | 0 |
| all，8例 | dual | 6 | 6 | 0 | 0 | 0 | 6 |

`official-plugin`八例及development/heldout/all汇总均为pending，六指标均为null；未收到规划窗口的插件封存包，不声称四模式比较完成。

## 裁定与证据

逐finding的verdict、issue ID、原finding ID、snapshot ID、规范原始report SHA256、源码引用、需求、根因、严重性和措辞备注在外部`native-001/judgments.json`。六个根因分别是B01切片少一项、B02可变默认值跨调用共享、B03集合删除重复项、B04空序列除零、B07文本前缀忽略目录边界、B08有符号解码破坏无符号值。所有引用完整行匹配；B04引用被删除的baseline guard同样有效。P1可支持为明确核心行为的验收阻塞，不推断部署规模或P0后果。

B05/B06的全部空报告均独立审查漏报。有限实数且有序边界下夹值数值等价；相等值的类型、对象身份或零符号变化没有违反给定数值合同。B06按顺序短路检查负数，空输入及零的行为符合需求。没有用旧scorer对空报告自动计分来代替质量判断。

保留报告准确性限定：R003/R019的“所有正count”过宽，实际触发为`1 <= count <= len(values)`；R013对相等上边界对象替换概述不精确；R009/R031对所有异常完全相同的泛化超出需求；R016/R030关于缺少长度校验的旁注不构成明确的非法输入拒绝要求；R024/R032的安全影响取决于实际调用用途。它们不形成单独的finding误报，也不否定已证明的核心问题。

75个输入文件大小/hash、32个规范原始报告hash和24处引用全部核对通过。38次本地微型样本观察支持六个缺陷的触发；这些验证归本评审所有，未冒充原审核席的测试证据。独立只读辅助代理`/root/blind_challenge`挑战B05/B06并审查外部计分脚本，主评审核对后承担最终裁定；该代理不是另一提供方裁判。

## 匿名封存与来源

主评审chat ID为`01a1e184-e759-7f71-8a9b-b1ec40134fad`（由本聊天环境报告）；实际模型及生效档位无独立运行时证据，均为null。原文措辞和finding ID可能透露来源；oracle可见且逐项由源码和需求支持，不能声称完美盲评或对问题答案盲化。评审者可能与被评席位属于同一模型家族。

匿名裁定于`2026-11-15T23:33:01.827529+00:00`封存，先把时间/hash发给规划窗口，再读取身份映射，读取后UTC观测为23:33:15。聊天消息及工具顺序支持先封存后揭示；不冒称操作系统访问审计。封存前未读private映射、旧模式评分或其他窗口质量意见；没有向插件执行窗口传递oracle、判断、预期数量或提示词建议。揭示后产物另存，匿名原件不改。

被测产品固定`adbf4928b057769bc62b7b0a51c5a248e0e236b7`。来源逐报告保存在`combined-native-001/report-origins.json`：Codex-only八份及dual十六份来自该产品；Claude-only八份仍归旧产品`87045fc6c138566d86d11b46e3b2744caf97b9b8`，通过T22成功路径兼容审计后复用，保留原日期、round和报告hash。32份均核对为首轮attempt=1，未以确认、取消或补跑替代。历史导出中16个Codex席实际模型未确认、16个Claude席报告`claude-opus-5-5`，所有实际effort为null；请求值单列，不能代替实际证据。

## 指标与交付定位

外部根为`/Users/owen/Desktop/Multi-agent-acceptance/T-0024/`。

| 产物 | SHA256 |
| --- | --- |
| `native-001/seal-manifest.json` | `dc0f92584b64ffd2e3083c74d0fb03b5861b01d2cfa78a6e9d254c88e4ce5c8e` |
| `native-001/judgments.json` | `f4d43435e45749c274fa4f632bd792d7384f75b19a2218b899d022d2d45e23f3` |
| `combined-native-001/seal-manifest.json` | `426ce6322c57cf9102b34b6573606ffcc224ec2a8db61125533cbcc3d0295059` |
| `combined-native-001/metrics.json` | `b48be4da273b346b88a9510a9c8ae43532501c1f2910ecb0acbdeba1cbae8e59` |

外部`score_agent_reviews.py`显式接受agent_reviewed，复用固定产品`benchmark.py:_score_case/score`六指标公式，按case+mode及根因issue ID去重。缺报告、缺报告级审查、缺finding判断或未决均传播为所属case+mode及分组pending/null。完整输入hash会在每次复算时重新验证，并与匿名封存的packet manifest hash比较。11项外部计分契约检查及结果复算通过，记录见`combined-native-001/score-contract-checks.json`和`output-validation.json`；完整命令见同目录README。源码定义副本hash为`6d85ece2ca631aacde654e267e6c3874631ceea71684ff5d6ff4c8838eb7ac5a`。

插件封存包后续只由规划窗口转交，再建立`plugin-001`和新的合并版本。增补须绑定raw output及converted report的独立hash、stable finding ID/raw location，并记录task/rescue通道及提示词/schema/权限差异。增补发生在原生身份揭示后，不能描述为严格受控的同协议实验。本阶段交规划窗口复核，不切换阶段2。
