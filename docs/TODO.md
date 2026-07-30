# TODO

本文档集中记录尚未实现的功能、依赖上游的改进和后续技术清理。每个待办事项
使用独立编号和固定结构，避免单个事项占据整份文档，也便于后续继续增加、
更新状态和归档。

## 维护约定

- 编号使用 `TODO-<三位数字>`，创建后不重复使用。
- 状态统一使用：`待处理`、`进行中`、`等待上游`、`已阻塞`。
- 每个事项至少说明背景、目标、实施方向和验收条件。
- 新增或改变事项状态时同步维护“待办索引”。
- 完成的事项从“待办事项”移动到“已完成”，保留原编号和完成版本。

## 待办索引

| 编号 | 标题 | 状态 | 依赖 | 优先级 |
| --- | --- | --- | --- | --- |
| `TODO-001` | 由 Host 向 Planner Hook 暴露结构化 ToolResult 状态 | 等待上游 | MaiBot Host | 中 |
| `TODO-002` | 将未消费的成功查看结果保留至当前决策结束 | 等待上游 | MaiBot Host | 低 |
| `TODO-003` | 稳定取得目标消息 ID 并关联完整内容 | 实现中，等待上游合并 | MaiBot Host | 高 |
| `TODO-004` | 保留发送失败在 Host、SDK 与插件之间的结构化原因 | 等待上游 | MaiBot Host / SDK | 高 |

## 待办事项

### TODO-001：由 Host 向 Planner Hook 暴露结构化 ToolResult 状态

- 状态：实现中，等待上游合并
- 依赖：MaiBot Host
- 优先级：中
- 临时兼容版本：插件 `0.1.9` 起

#### 背景

当前插件通过 `maisaka.planner.before_request` Hook 配对
`view_forward_message` 的 assistant tool call 与 tool result，从而复用源群
已经展开的完整内容。

Hook 当前收到的 tool 消息只有 `role`、`content` 和 `tool_call_id`，没有
ToolResult 的 `tool_name`、`success`、`metadata` 或 `structured_content`。
因此插件无法直接、可靠地区分成功内容和失败文本。

#### 当前临时方案

插件 `0.1.9` 起匹配当前内置工具与统一工具注册表的稳定失败前缀，并按
`tool_call_id` 去重观察结果。这是兼容性方案，不应成为长期协议。

#### 目标

让第三方插件通过稳定的结构化字段判断 ToolResult 是否成功，不再解析面向
Planner 的错误文案，也不依赖主程序内部数据库。

#### 上游实施方向

优先考虑以下两种方案之一：

1. 在不改变实际 LLM 消息内容的前提下，为
   `maisaka.planner.before_request` Hook 的序列化 tool 消息补充
   `tool_name`、`success` 和必要的结构化状态。
2. 新增工具执行完成后的只读命名 Hook，例如
   `maisaka.tool.after_execute`，提供 `session_id`、`tool_call_id`、
   `tool_name`、调用参数、`success`、错误类别和结果元数据。

结构化字段应只服务 Hook 载荷；若字段会被 Hook 修改后重新反序列化为 LLM
消息，主程序必须明确哪些字段不会进入模型上下文。

#### 插件迁移计划

上游能力可用后：

1. 使用结构化 `success` 判断查看结果，不再匹配失败文本；
2. 使用 `tool_name` 校验结果归属，保留 `tool_call_id + msg_id` 配对；
3. 保持当前上下文查看资格、分类失败状态、可重试故障与空内容的独立连续
   计数及 Planner 续轮提醒语义，不引入时间过期；
4. 删除失败前缀兼容表及对应 Host 文案耦合测试；
5. 增加新旧 Host 兼容测试，并在最低 Host 版本允许时移除旧兼容分支。

#### 验收条件

- 成功和失败的 `view_forward_message` 结果都带有可靠的结构化状态；
- Hook 能区分工具业务失败、工具注册失败和执行异常；
- 结构化字段不会意外污染实际发送给 Planner 的消息；
- Planner 请求被打断或重试时，同一 `tool_call_id` 保持稳定；
- 相关 Hook 载荷、兼容范围和安全边界进入官方插件文档。

### TODO-002：将未消费的成功查看结果保留至当前决策结束

- 状态：等待上游
- 依赖：MaiBot Host
- 优先级：低
- 目标版本：待定

#### 背景

插件在成功取得 `view_forward_message` 完整结果后，会提醒 Planner 在紧接续轮
判断是否值得分享。Planner 请求若被新消息打断，提醒会保留到重试；但查看
资格每轮严格根据实际 Planner 上下文重建。一旦原始 assistant tool call 或
ToolResult 被上下文窗口真正裁剪，查看资格就会立即失效。

该情况需要连续中断并跨越上下文边界，发生概率较低。当前版本不改变既有安全
语义：已经被裁剪的完整结果不能仅凭进程内状态继续授权转发，Planner 必须
重新查看。

#### 目标

对于已经成功查看、但尚未完成分享判断的消息，尽量将对应 assistant tool call
与完整 ToolResult 保留到当前决策正常结束，使 Planner 在中断恢复后仍有一次
基于完整内容完成判断的机会，同时不把摘要或过期缓存误当作成功查看资格。

#### 上游实施方向

优先由 MaiBot Host 将尚未消费的成功 tool call 与 ToolResult 作为协议原子段
临时固定到 Planner 上下文，保留到下一次正常 Planner 响应完成。实现需要：

1. 明确定义“已消费”和“当前决策结束”的生命周期；
2. 对固定内容设置上下文预算和过大内容处理策略，不能无限突破模型窗口；
3. 保持 `session_id`、`tool_call_id` 和 `msg_id` 的来源隔离；
4. 请求中断时继续保留，正常响应后立即释放；
5. 无法完整保留时安全退回重新调用 `view_forward_message`，不得自动使用
   `content_summary` 获得成功查看资格。

若未来只能在插件侧实现，需要先验证 Hook 能否安全重建协议完整的 assistant
tool call 与 ToolResult；不得只注入一段声称“已经查看”的提醒文本。

#### 验收条件

- 成功查看后的 Planner 请求被新消息中断并跨越普通上下文边界时，完整查看
  结果仍能进入恢复后的当前决策；
- 对应 Planner 正常响应完成后，临时固定状态立即释放；
- 不同聊天流和不同 `msg_id` 之间不会共享查看资格；
- 内容超过模型预算时明确要求重新查看，不静默使用摘要降级；
- 现有“结果不在实际上下文中即无转发资格”的安全边界不被绕过。

### TODO-003：稳定取得目标消息 ID 并关联完整内容

- 状态：实现中，等待上游合并
- 依赖：MaiBot Host
- 优先级：高
- 临时兼容版本：插件 `0.1.14` 起

#### 背景

插件把 source 群中的合并转发发送到 target 群后，目标平台会分配新的消息
ID。MaiBot 发送服务会先用平台成功回执更新最终 `SessionMessage.message_id`，
但当前官方 Host 的 `send.forward` capability 只返回发送成功布尔值；SDK 又把
该响应归一化为 `bool`，因此正式上游契约仍会在 Host 与 SDK 边界丢失最终 ID。
配套 Host 分支 `1.1.2-send-forward-result` 已能返回详细结果，插件也能取得并
持久化其中的最终 ID，但这不能视为官方 Host 的通用能力。

目标群 Planner 的 `reply` 工具只能按当前 Maisaka 历史中的消息 ID 查找真实
`original_message`。旧版插件通过 `maisaka.context.append` 写入的展开内容
属于合成上下文，即使指定稳定 `message_id` 也没有 `original_message`，不能
作为回复目标，而且会在目标 prompt 中重复占用大量 token。

当前 MaiBot 已支持从消息库恢复最近上下文。本插件使用
`storage_message=True`，平台最终消息 ID 会在落库前回填；目标会话尚未创建
Maisaka 心流实例时，后续主动任务创建实例并启动后，可以从消息库恢复这条
真实消息。恢复得到的历史项保留 `original_message`，因此“发送时没有心流
实例就一定遗漏真实消息”不再是本项的主要问题。

在未包含配套修改的 Host 上，调用方仍无法知道历史中那条目标消息的最终 ID。
插件只能让 Planner 从目标上下文中保守定位刚发送的消息，无法提供唯一回复
锚点；无法可靠定位时必须保持沉默。配套分支解决了这部分取值和持久化，但尚待
合并、发布和跨适配器验证。

此外，路径 A 和路径 B 虽然都能在 source 侧取得完整查看结果或符合既有边界的
fallback 内容，Host 目前没有为第三方插件提供一种结构化、幂等的方式，把这份
内容关联到 target 中刚发送的真实 `SessionMessage`。重复使用
`maisaka.context.append` 只会制造没有 `original_message` 的合成消息，既不能
作为回复目标，也会重复占用 token，因此不能作为关联方案。

目标消息 ID 只解决“回复哪一条”，完整内容关联解决“目标 Planner 是否已经
可靠理解这条消息”。两者缺一时都不能宣称目标 Planner 可以准确回复：Host
没有最终 ID 时沿用保持沉默的兼容路径；只有 ID、没有关联内容时，也只能依据
目标群当前实际可见内容谨慎判断。

#### 当前临时方案

插件 `0.1.14` 起开启 `send.forward` 的 Maisaka 历史同步，并从主动任务
metadata 中移除 source 消息 ID。Planner 仅被允许选择目标群上下文中刚刚
真实发送的合并转发消息，不得使用 source ID。

插件 `0.1.16` 起进一步移除重复注入原始转发段和源群完整展开内容的
`maisaka.context.append`。在取得可靠的目标消息 ID 前，目标 Planner 只能尝试
从当前真实历史中定位刚发送的合并转发；无法可靠定位时必须保持沉默。

插件 `0.1.18` 起请求 `send.forward(return_details=True)`，取得目标消息 ID
时将它与 target 的已发送阶段一并持久化，并作为主动任务唯一的回复锚点。
完整能力依赖配套 Host 分支 `1.1.2-send-forward-result`；旧 Host 仍只返回
布尔结果时继续使用上述保守定位规则。

插件 `0.1.19` 明确保留无目标 ID 的 fallback。无论 Host 返回旧布尔成功结果，
还是返回 `sent=True` 但 `message_id=None` 的详细结果，插件都不会重新发送，
而是保留已发送阶段并沿用从目标真实历史定位消息的旧方法；无法可靠定位时
要求 Planner 保持沉默。

当前 source 路径 B 会在物理发送前通过真实 `view_forward_message` 取得完整
ToolResult，成功或达到既有 fallback 边界后才恢复转发请求；路径 A 则复用
Planner 已经查看的结果。这些内容目前只用于 source 分享资格，不会注入 target
或声称已经与目标消息关联。

#### 上游实施方向

1. 直接为现有 `send.forward` 增加可选参数 `return_details`，默认值为
   `False`；默认调用继续返回 `bool`，显式传入 `return_details=True` 时返回
   至少包含 `sent` 和最终目标 `message_id` 的结构化结果。详细结果不使用
   顶层 `success`，避免被现有 SDK 的布尔能力兼容逻辑压缩。
2. 详细结果必须在平台成功回执更新 `SessionMessage.message_id` 之后生成；
   平台没有返回最终 ID 时使用 `None`，不能返回发送前的 `send_api_*` 临时
   ID。
3. 复用 SDK `SendCapability.forward(..., **kwargs)` 已有的参数透传能力和
   “无顶层 `success` 的字典保持原样”语义，不新增 SDK 方法、不修改 SDK
   结果归一化逻辑，也不得另建平行的转发 capability 名称。
4. 调用方取得 `message_id` 后可把它作为后续回复、关联记录或审计的稳定锚点；
   本插件按 target 持久化该 ID，重载恢复时不得退回 source ID。
5. Host 提供按目标 session 和最终 `message_id` 关联扩展内容的安全接口，或在
   目标 Planner 请求构造阶段提供等价结构化载荷。关联记录至少要绑定 source
   任务、目标 session、目标最终 ID 和内容类型（完整结果或 fallback），不能
   仅注入一段声称“已经查看”的无来源文本。
6. 关联不得复制目标真实消息、原始转发节点或媒体二进制；同一 source 结果可
   复用于多个 target，但每个 target 只建立一次幂等关联。目标 Planner 启动时
   应能同时看到真实消息、唯一回复锚点和与之对应的内容。
7. 发送成功而关联或 Planner 入队失败时，插件只补做未完成阶段，不得为取得
   ID、补关联或重试提示而重复物理发送。Host 无最终 ID 时不得创建猜测关联，
   继续沿用无法可靠定位即沉默的 fallback。
8. 发送失败不得返回虚假目标 ID；插件应保留已发送 target 的持久化阶段，
   避免后续触发或提示失败导致重复物理发送。
9. 为旧 Host 保留能力探测和兼容分支，直到插件最低 Host 版本允许
   移除。现有消息落库、心流实例启动恢复和真实历史构造路径应保留回归测试，
   但不再要求为了本项预先创建心流实例。

#### 旧兼容路径移除条件

当前不得删除无 `message_id` fallback。只有同时满足以下条件后，才能在后续
维护版本中移除：

1. `send.forward(return_details=True)` 的详细结果契约已经合并到 MaiBot
   上游 `dev` 和 `main`，并进入正式 Host 版本，而不是只存在于配套分支；
2. 插件 manifest 的最低 Host 版本已经提高到首个包含该契约的正式版本；
3. 插件支持的适配器已经验证会在发送成功后稳定返回平台最终消息 ID，不会
   把 `send_api_*` 临时 ID 或空值当作最终 ID；
4. 已明确处理旧状态文件中 `SENT` 但没有 `target_message_id` 的任务。不得为
   补取 ID 重发消息；必要时应继续为这些遗留任务保留只读恢复 fallback；
5. 上游已经提供并验证目标完整内容关联契约；仅有最终 ID 仍不足以移除“内容
   不足时保持沉默”的保守边界；
6. 移除时同步删除无 ID 的 Planner 意图分支及对应兼容测试，并更新 README、
   设计文档和 Changelog。

#### 验收条件

- `send.forward(..., return_details=True)` 能够取得平台最终确认的目标消息
  ID，且该 ID 与 Maisaka 真实历史项完全一致；平台未返回最终 ID 时不得
  暴露 `send_api_*` 临时 ID；
- 现有只读取布尔结果的 `send.forward` 调用方无需修改且行为不变；
- SDK 能透传 `return_details`，并原样保留不含顶层 `success` 的详细结果，
  无需新增 SDK 方法；
- 目标 Planner 使用该 ID 调用 `reply` 时可以正常生成并发送回复；
- source 的完整查看结果或明确 fallback 能按目标最终 ID 建立一次结构化关联，
  目标 Planner 能区分完整内容与降级内容；
- 多 target 复用 source 结果时不会重复展开、复制真实消息或保存媒体 Base64；
- 关联、主动触发或重载恢复失败后重试不会重复物理发送；
- 无目标 ID、关联缺失或内容不足时，目标 Planner 保持沉默，不把 source ID、
  临时 ID 或无来源文本当作可靠依据；
- source 与 target 消息 ID 在接口、日志和主动任务 metadata 中语义明确，
  不会跨聊天流混用；
- 发送失败时不返回虚假目标 ID，重试也不会重复物理发送；
- 插件现有转发测试覆盖详细结果、旧 Host 布尔结果和重载恢复路径；Host
  上游后续在既有测试组织中补充 capability 契约回归，不为本项单独新建测试
  文件。

### TODO-004：保留发送失败在 Host、SDK 与插件之间的结构化原因

- 状态：等待上游
- 依赖：MaiBot Host / maibot-plugin-sdk
- 优先级：高
- 目标版本：待定

#### 背景

生产环境中，SnowLuma 网关可能在等待发送 ack 时超时。当前错误经过以下
链路后会丢失具体原因：

1. `send_service` 在 Platform IO 投递失败后返回 `None`；
2. Host `send.forward` capability 将结果包装为 `{"success": None}`，没有
   携带底层回执中的超时错误；
3. SDK 把 `send.forward` 列为布尔成功能力，将 Host 字典归一化为
   `False`；
4. 插件只能把非字典的 `False` 解释为“返回格式错误”。

因此 Planner 即使已经等待真实投递完成，也只能知道发送失败，无法判断是
网关 ack 超时、路由缺失、驱动不可用还是其他原因。本插件当前保持既有
`CapabilityResult` 兼容行为，不在缺少证据时根据耗时猜测具体根因。

#### 目标

让发送失败的稳定错误码和可读原因从 Platform IO 回执完整传递到第三方
插件，并由插件原样纳入目标结果和 ToolResult；成功路径继续兼容现有
`send.* -> bool` 调用方式。

#### 上游实施方向

1. `send_service` 失败时返回结构化结果，至少包含 `success`、`code`、
   `error`、`driver_id` 和必要的回执元数据，不再用无原因的 `None` 表示
   所有失败。
2. Host capability 保留底层失败信息；异常和正常业务失败使用稳定且可区分
   的错误码，例如 `gateway_ack_timeout`、`route_not_found` 和
   `driver_unavailable`。
3. SDK 不应把带 `error` 的失败字典压缩成 `False`。可为发送能力新增详细
   结果模式，或只对成功响应返回布尔值并保留失败字典。
4. 上游接口可用后，插件统一解析当前 SDK 布尔值、旧版 Host 字典和新版
   结构化发送结果；明确错误原样保留，缺失时使用“Host 未提供失败原因”，
   不再把合法的布尔失败描述成返回格式错误。
5. 插件最终只向 Planner 暴露必要的错误类别和简短中文原因，不输出完整
   媒体数据、敏感路由元数据或底层堆栈。

#### 验收条件

- SnowLuma 发送 ack 超时时，插件能取得稳定错误码和明确超时原因；
- Host 日志、SDK 返回值、插件日志和 ToolResult 使用同一错误类别；
- 路由缺失、驱动不可用、网关拒绝和 ack 超时可以可靠区分；
- 旧版布尔成功响应和旧版失败字典仍有兼容测试；
- 失败信息不会泄露消息正文、媒体 Base64、凭据或敏感路由数据。

## 新增 TODO 模板

复制以下结构到“待办事项”，并在“待办索引”中增加对应记录：

```markdown
### TODO-XXX：标题

- 状态：待处理
- 依赖：无
- 优先级：低 / 中 / 高
- 目标版本：待定

#### 背景

说明现状、问题和触发场景。

#### 目标

说明期望达到的结果和明确边界。

#### 实施方向

列出候选方案、依赖和迁移步骤。

#### 验收条件

- 列出可验证的完成标准。
```

## 已完成

暂无。完成事项应保留原编号、完成版本和简短结果说明。
