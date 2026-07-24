# TODO：未来功能与上游改进

## 由 Host 向 Planner Hook 暴露结构化 ToolResult 状态

### 背景

当前插件通过 `maisaka.planner.before_request` Hook 配对
`view_forward_message` 的 assistant tool call 与 tool result，从而复用源群
已经展开的完整内容。

Hook 当前收到的 tool 消息只有 `role`、`content` 和 `tool_call_id`，没有
ToolResult 的 `tool_name`、`success`、`metadata` 或 `structured_content`。
因此插件无法直接、可靠地区分成功内容和失败文本。`0.1.9` 起暂时匹配当前
内置工具与统一工具注册表的稳定失败前缀，这属于兼容性方案，不应成为长期
协议。

### 期望的主程序能力

优先考虑以下两种上游方案之一：

1. 在不改变实际 LLM 消息内容的前提下，为
   `maisaka.planner.before_request` Hook 的序列化 tool 消息补充
   `tool_name`、`success` 和必要的结构化状态。
2. 新增工具执行完成后的只读命名 Hook，例如
   `maisaka.tool.after_execute`，提供 `session_id`、`tool_call_id`、
   `tool_name`、调用参数、`success`、错误类别和结果元数据。

不建议要求第三方插件查询主程序内部数据库，也不建议让插件继续解析面向
Planner 的错误文案。结构化字段应只服务 Hook 载荷；若字段会被 Hook 修改后
重新反序列化为 LLM 消息，主程序必须明确哪些字段不会进入模型上下文。

### 插件迁移计划

上游能力可用后：

1. 使用结构化 `success` 判断查看结果，不再匹配失败文本；
2. 使用 `tool_name` 校验结果归属，保留 `tool_call_id + msg_id` 配对；
3. 保持成功缓存、连续失败计数、TTL 与 Planner 续轮提醒的现有语义；
4. 删除失败前缀兼容表及对应 Host 文案耦合测试；
5. 增加新旧 Host 兼容测试，并在最低 Host 版本允许时移除旧兼容分支。

### 上游验收条件

- 成功和失败的 `view_forward_message` 结果都带有可靠的结构化状态；
- Hook 能区分工具业务失败、工具注册失败和执行异常；
- 结构化字段不会意外污染实际发送给 Planner 的消息；
- Planner 请求被打断或重试时，同一 `tool_call_id` 保持稳定；
- 相关 Hook 载荷、兼容范围和安全边界进入官方插件文档。
