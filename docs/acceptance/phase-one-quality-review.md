# 阶段1独立模型质量裁定

状态：`combined_done`。用户授权本独立窗口的模型判断替代本轮原定人工判断；全部新判断和质量指标为`source=agent_reviewed`，不是真人确认，未改正式scorer或历史user_supplied记录。

四种模式在八例中各发现六个已知问题，无已知漏报或额外有效发现；dual重复六项。**插件的一项误报依赖有限实数的数值合同解释**：B05合法输入的正负零差异真实可观察，主评审认为需求未保证符号位保持；未看原生判断的辅助评审认为“returned exactly”可含表示保持读法，建议unresolved。分歧完整保留，不能宣称一致共识；若另有表示保持要求，这项裁定应改变。小样本及不同调用条件不支持推断插件普遍较差或任何模式普遍更优，不作模式、速度或成本排名。

| all，八例 | 有效发现 | 已知发现 | 额外有效 | 漏报 | 误报 | 重复 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| codex-only | 6 | 6 | 0 | 0 | 0 | 0 |
| claude-only | 6 | 6 | 0 | 0 | 0 | 0 |
| dual | 6 | 6 | 0 | 0 | 0 | 6 |
| official-plugin，channel=task | 6 | 6 | 0 | 0 | 1（上述合同解释） | 0 |

development六例各模式有效/已知发现4，dual重复4，插件误报1；heldout两例各模式有效/已知发现2，dual重复2，所有模式误报0。各组其他指标均0。完整32格及三个分组见`combined-003/metrics.json`。主评审无未决标签；辅助B05的未决建议没有被伪写为同意误报。

## 裁定依据

原生32份报告（含8空报告）和插件8份报告（含1空报告）全部逐份阅读。原生24个finding均known；插件7个finding中6个known、1个false_positive。已知根因分别是B01切片少一项、B02默认可变列表跨调用共享、B03集合删除重复项、B04空序列除零、B07文本前缀忽略目录边界、B08有符号解码破坏无符号值。31处完整行引用全部匹配；B04对删除的baseline guard引用有效。六类问题P1可支持为明确核心行为的验收阻塞，不推断部署规模或P0影响。

插件B05 `P25-B05-F001`（原ID `clamp-equal-bounds-upper-return`）的复现成立：`clamp(1.0,-0.0,0.0)`在baseline返回+0.0、candidate返回-0.0，copysign可区分，且输入合法。本裁定没有以域外或空oracle否定它。主评审将明确的有限实数夹值及“精确边界”解释为数值保证，两者都等于实数边界0；未明示的IEEE符号位、类型或对象身份保持保证不能补入，故记`FP-B05-SIGNED-ZERO-REPRESENTATION`。另一位辅助评审建议unresolved的不同合同读法，以及本窗口实算结果，分别保留在`plugin-001/auxiliary-quality-review.json`、`judgments.json`和`B05-reproduction.json`。

所有空报告有summary已读、finding计数和漏报审查；B05在数值合同下夹值等价，B06的比较顺序、短路、空输入和零行为符合合同。未沿用旧scorer的空报告自动计分代替质量判断。原生措辞限定仍在匿名封存中：R003/R019对所有正count泛化过宽，实际为`1<=count<=len(values)`；R013对相等上边界对象替换概述不精确；R009/R031对所有异常相同的泛化超出合同；R016/R030缺少长度校验旁注没有明确非法输入拒绝保证；R024/R032的安全后果取决于调用用途。这些不虚增finding误报或掩盖根因。

## 顺序、来源与比较限制

主评审chat为`01a1e184-e759-7f71-8a9b-b1ec40134fad`，实际模型/档位无独立运行时报告，均null。原生匿名裁定于`2026-11-15T23:33:01.827529+00:00`封存，先向规划窗口发时间/hash，再读取身份映射；读取后UTC观测23:33:15。原文文风可能透露提供方，oracle可见但根因逐项由源码/需求支持，不能称完美盲评。插件增补在原生身份揭示后进行，评审者可能与被评席同模型家族；只读辅助代理不是跨提供方真人裁判。

被测产品固定`adbf4928b057769bc62b7b0a51c5a248e0e236b7`。原生Codex-only八份及dual十六份来自该产品；Claude-only八份仍归旧产品`87045fc6c138566d86d11b46e3b2744caf97b9b8`，经T22兼容审计复用，原日期、round和hash保留。原生32份均首轮attempt=1；插件八例均唯一attempt-001，未用确认、取消或补跑替代。

插件只读规划窗口转交的`/Users/owen/Desktop/Multi-agent-acceptance/T-0025/handoff/plugin-001/`，manifest SHA256为`534415e6b61a406dc01bf6e0161992228011e9f43e69154a5261da76f46ca40d`。264个payload文件大小/hash和精确库存匹配，40冻结输入文件与原生逐字节相同。八个raw文本hash与converted规范报告hash分别核对，七个finding的原ID/稳定ID、UTF-8半开字节跨度、行号及raw对象hash一致；转换除ID外内容未改。绝对raw路径只保留来源文字，未追读T25其他目录。

记录支持外层Claude加载codex插件1.0.6并各调用一次固定gate的官方task，channel=task，非rescue/native-review。官方获取记录指向`openai/codex-plugin-cc`提交`db52e28f4d9ded852ab3942cea316258ae4ef346`；包未含整树源码/clean-status或gate源码，不能声称本窗口独立逐字节证明官方整树原版未改或完整权限对抗审计。共享prompt为完整前缀，实际追加可见schema，RPC outputSchema=null；普通readOnly宽读不继承T21命名隔离，事件不是完整文件访问审计。因此不是严格同协议、同权限实验。

插件内层实际模型/档位未确认；外层终态报告claude-opus-5-5，实际档位未确认。原生16个Codex席实际模型未确认、16个Claude席报告claude-opus-5-5，全部实际effort为null。请求值另存不冒充实际证据；provider用量与嵌套wall time保留原值分别记录、不混加；估计成本不是订阅账单。完整来源在`combined-003/report-origins.json`。质量结果只交规划窗口，未向执行窗口发送oracle、标签、预期数量或提示词建议。

## 指标与封存

外部根：`/Users/owen/Desktop/Multi-agent-acceptance/T-0024/`。旧版封存全部原样保留。

| 产物 | SHA256 |
| --- | --- |
| `native-001/seal-manifest.json` | `dc0f92584b64ffd2e3083c74d0fb03b5861b01d2cfa78a6e9d254c88e4ce5c8e` |
| `native-001/judgments.json` | `f4d43435e45749c274fa4f632bd792d7384f75b19a2218b899d022d2d45e23f3` |
| `combined-native-001/seal-manifest.json` | `426ce6322c57cf9102b34b6573606ffcc224ec2a8db61125533cbcc3d0295059` |
| `combined-native-002/seal-manifest.json` | `98b4a4f4f3c54412d8a8b42032b74811daa9c86a9b27f738aefa6092db7e0184` |
| `plugin-001/seal-manifest.json` | `aadd7c2cdf1e8634d906af0157c2d2730b7298e2b813ad61b96e7fbe51f50fb1` |
| `plugin-001/judgments.json` | `33e12a68ec783861be6d99e29c9a320a8e3df4ddb260c2ac831ace299d68fc7c` |
| `combined-003/seal-manifest.json` | `594850a305bb15b394c6c8135a7453044fc69ca53b16492d1788ae3f237ec8e7` |
| `combined-003/metrics.json` | `18939d179ef92e7bd723cb0c080da5056c80ac433064f2691b6c188c186ec234` |

六指标沿固定产品`benchmark.py:_score_case/score`定义，按case+mode的根因issue ID去重。缺报告、缺报告级审查、缺finding判断或未决均传播为所属case+mode及split/all的pending/null。002补强complete但显式未审的情况：summary_read严格True、finding_count为匹配的严格整数、漏报说明非空非空白，空报告明确已审且非空报告空标志为False。41项检查含29显式不完整反例及旧漏洞复现，原生真实指标不变；历史11项检查及seal保留，不再扩大旧计分器检查。

003继承002守卫，只新增插件导入绑定及合并。完整复算与保存结果相同，24原生case/mode行保持不变，32格和分组均与封存判断相符。命令、验证及边界见`combined-003/README.md`和`output-validation.json`，无需重跑模型。原生首次开工HEAD为`2d77ab161157297a0a73461b4e64b4ca563a0b24`；守卫及插件增补基线/开工HEAD为`4328ed51314aef88094568f2c9fe4aa5fdaec780`。交规划窗口复核，不自行进入阶段2。
