# 运行时 JSON 原始证据边界

规划来源校验需要知道模型实际输出了什么。若运行时先按普通 JSON 解码覆盖重复字段，后面的 schema 校验和再次序列化无法恢复这些原始字段。T35 在实际传输与结构化结果两层拒绝这种不明确的输入，不改变正常会话权限或调用协议。

## 解码合同

`adapters.runtime_decode.strict_json_loads(text)` 接收字符串，先完成 JSON 语法解析，再检查语义。普通语法错误仍抛 `json.JSONDecodeError`；重复 key、NaN/Infinity、溢出的非有限浮点、孤立 surrogate、超深容器及解析器拒绝值使用 `RuntimeJSONError(ValueError)`。错误消息是固定短分类，不包含原 payload、字段名或原异常 repr，cause 只保留在内存异常链。

JSON 对象/数组最多 128 层，根容器算第一层；标量和字符串不增加容器深度。合法 Unicode 和转义 surrogate pair 保留；孤立 surrogate 拒绝。数组顺序、对象字段内容与合法数值保持原语义。没有增加全局传输字节上限；128 层限制不适用于普通非 JSON 文本的字符内容。

`result_value(structured=True)` 对结果字符串使用同一 loader，覆盖 Codex final agent message 的 JSON 文本和 Claude result 字符串。已经解码的 JSON 值也检查字符串/键的 Unicode、有限数和深度。无 schema 的普通结果文本不会因“看起来像 JSON”而被重新解释。

## stdout、stderr 与日志

`Process._line` 对 stdout 字节严格解码 UTF-8。任何坏 UTF-8，包括普通文本、大行或未换行尾部，都失败。合法 UTF-8 的普通非 JSON 文本、非对象 JSON、超过 1 MiB 的普通文本和尾部仍按原 text 形态记录；产品 RPC/流边界继续拒绝把它们当成合法对象。

语义坏 JSON 或坏 UTF-8 在解析成 message、提取 id 或写入原文前停止。日志仅写：

```text
kind=decode_error
proc=<host process label>
stream=stdout
error=invalid_json | invalid_utf8
byte_count=<raw line bytes>
sha256=<raw line bytes hash>
```

字节数/hash 对应按 LF 分行后的原字节，可能包含尾部 CR；没有 payload、message、text、异常对象或 cause。配置读取仍保持 pending，既有 Recorder 继续屏蔽相关诊断，不能由一个不明确的 id 提前解除屏蔽。

stderr 一律作为诊断文本，即使本身是合法 JSON，也不当作 RPC message。否则 stderr 中伪装的同 id 响应可能消费 pending config/read 状态。坏 UTF-8 的 stderr 保留替换后的诊断文本，并标 `encoding_replaced=true`；它不进入模型结果队列。正常 stderr 文本不变。

Claude 环境检查在合并完整 auth status stdout 后再次使用严格 loader，因而多行 pretty JSON 中重复的 loggedIn 也不能按最后一个值变成已登录成功。

## 错误与清理

畸形输出和不明确 auth 不推断为认证过期或模型不可用。它们沿用 `Error(failure_category=None)`；环境预检由 `RuntimeFailure` 携带错误。模型成功终态与实际 `ProcessExited` 分别记录，没有成功产物就不能接受 Completed。预检失败不虚构模型回合或退出事件。

`read()` / `wait()` 持续报告原 `RuntimeJSONError`。`terminate()` 只在实际子进程退出、已有 owned-group 停止标记、watcher 与所有 readers 完成后，允许完成对这个已知读取错误的清理；它不吞其他 reader/Recorder I/O 错误。停止本身出错时，该错误优先于重复等待解析错误。

OS 退出不是 stdout EOF。队列暂空且子进程已退出时，`read()` 等待 watcher 的既有有限尾部收集：合法尾部先返回再 EOF，坏 JSON 尾部仍失败。退出码先到不能跳过未解析的字节，也不能把之前观察到的 Completed 直接当最终成功。

新回归暴露了 macOS 的短暂信号拒绝：进程退出、进程组消失前，killpg 可返回 EPERM。现在只包装 killpg 本身的 PermissionError，与日志写入错误分开。仅在有限等待后确认实际退出且进程组明确不存在，才忽略该信号拒绝；组仍存在、探测仍 EPERM 或退出未知时仍失败。Recorder 的 PermissionError 不能走这个例外。

停止失败后，收集 watcher 也只能有限等待；真实进程仍活着时返回停止错误，保留 observer/reader 和所有权，不伪造退出或 EOF。活进程加信号拒绝的回归验证此分支，测试最终恢复真实信号并清场。

这沿用现有进程组所有权边界；不增加对主动脱离该组的后代的停止承诺，也不把 app-server 退出当成任意后代均停止的证据。

## 验证与后续

测试使用真实原始字节，覆盖外层 RPC/Claude stream、内层结构化 JSON、auth 多行输出、坏编码、深度边界、敏感配置遮蔽与进程/reader 收集。新 fake 模式显式启用，原有效输出、长行、非对象 JSON、尾部及旧权限精确断言保留。

本机版本已重新核对为 Codex 0.169.0、Claude Code 2.1.325，与快照一致；没有刷新或修改协议与真实录制，没有运行模型或权限探测。T36 将在这些严格原始数据之上接入只读规划 runner。
