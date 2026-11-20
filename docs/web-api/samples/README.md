# 离线 HTTP 样例

每个场景的 `index.json` 按请求次序列出序号、HTTP 方法、路径、原始查询字符串、请求 JSON、HTTP 状态码和响应文件名。没有查询字符串或请求体时，相应字段为 `null`。各响应体保存在同目录的 `NNN.json`。同一场景中各文件共享标识符、提交和时间的占位值。

在仓库根目录重录全部场景。

```sh
uv run python tests/unit/web_support/record_samples.py
```

用 `--scenario` 选择 `accept`、`rework`、`dual-partial` 或 `cancel`，用 `--output DIR` 将录制写到指定目录。场景的页面输入与预期结果见 [项目服务说明](../../project-service.md#离线联调)。样例由录制脚本生成。

样例中的 `byte_count` 保留 HTTP 实际返回值。重录测试在比较时统一处理含运行元数据的日志长度，包括 CLI 的 JSON 回执、运行时事件和工具协议记录。固定测试输出、日志分页和退出码逐值比较。
