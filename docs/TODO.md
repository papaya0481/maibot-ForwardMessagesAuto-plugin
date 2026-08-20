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
| `TODO-003` | 完善目标消息 ID 与完整内容的正式关联契约 | 等待上游 | MaiBot Host / SDK | 低 |
| `TODO-004` | 保留发送失败在 Host、SDK 与插件之间的结构化原因 | 等待上游 | MaiBot Host / SDK | 高 |
| `TODO-005` | `view_forward_message` 等待图片识别完成 | 等待上游 | MaiBot Host | 高 |
| `TODO-006` | 单次调用 `view_forward_message` 递归展开嵌套合并转发 | 等待上游 | MaiBot Host | 低 |
| `TODO-008` | Host 让现有 `send.forward` 能按原消息 ID 转发 | 待处理 | MaiBot Host | 中 |
| `TODO-009` | SnowLuma Adapter 映射并验证原生引用节点 | 待处理 | TODO-008 / SnowLuma Adapter / QQ-NapCat | 中 |
| `TODO-010` | 插件按 target 使用引用节点并持久化投递结果 | 待处理 | TODO-008 / TODO-009 | 中 |
| `TODO-011` | Host 为已发送的合并转发保留可读的目标 Planner 上下文 | 等待上游 | MaiBot Host | 高 |
| `TODO-012` | 解析嵌套合并转发中的 QQ XML 聊天记录卡片 | 等待上游 | MaiBot-Napcat-Adapter / MaiBot Host / QQ-NapCat | 中 |

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

### TODO-003：完善目标消息 ID 与完整内容的正式关联契约

- 状态：等待上游
- 依赖：MaiBot Host / SDK
- 优先级：低
- 当前实现：当前 `dev` 已通过兼容路径覆盖实际投递、回复锚点与安全降级

#### 当前已实现的等效路径

当前 Host 在 `send.forward(return_details=True)` 的原始 capability 结果中提供
`sent` 和平台最终 `message_id`。插件优先经受限的 `cap.call` 调用取得该
原始结果，避开普通 SDK 对顶层 `success` 的布尔归一化；随后按 target 持久化
最终消息 ID、同步真实消息历史，并把该 ID 作为目标 Planner 的唯一回复锚点。

平台未返回最终 ID、旧 Host 只返回布尔成功，或原始调用入口不可用时，插件仍会
记录已发送阶段而不重复物理发送；目标 Planner 只能从真实目标历史中可靠定位该
消息，否则保持沉默。source 的完整查看结果继续只用于分享资格，目标侧收到的是
真实合并转发消息，可按该消息自身的 `msg_id` 再次调用
`view_forward_message`；因此不会再以重复注入 source 内容换取回复能力。

这条路径已经覆盖“目标群能够围绕真实发送消息安全判断和回复”的实际效果。本 TODO
保留的是去除低层兼容调用、让普通 SDK 调用方也能获得同一详细结果，以及提供可选
的 Host 级结构化内容关联，不是当前投递链的功能阻塞。

#### 剩余的正式契约

插件把 source 群中的合并转发发送到 target 群后，目标平台会分配新的消息
ID。MaiBot 发送服务会先用平台成功回执更新最终 `SessionMessage.message_id`。
MaiBot `dev` 的 `7a2bdc26a` 已让 `send.forward(return_details=True)` 返回
`success`、`sent` 和最终 `message_id`；但当前 SDK 会因顶层 `success` 字段将该
详细结果归一化为 `bool`，所以普通 `ctx.send.forward` 代理仍会丢失最终 ID。
插件仅对这个已声明的详细发送请求使用 SDK 允许的原始 `cap.call` 通道保留结果，
再按 target 持久化最终 ID；这是一项兼容层，不能替代正式 SDK 契约和跨适配器验证。

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

#### 当前实现

插件 `0.1.14` 起开启 `send.forward` 的 Maisaka 历史同步，并从主动任务
metadata 中移除 source 消息 ID。Planner 仅被允许选择目标群上下文中刚刚
真实发送的合并转发消息，不得使用 source ID。

插件 `0.1.16` 起进一步移除重复注入原始转发段和源群完整展开内容的
`maisaka.context.append`。在取得可靠的目标消息 ID 前，目标 Planner 只能尝试
从当前真实历史中定位刚发送的合并转发；无法可靠定位时必须保持沉默。

插件 `0.1.18` 起请求 `send.forward(return_details=True)`，取得目标消息 ID
时将它与 target 的已发送阶段一并持久化，并作为主动任务唯一的回复锚点。针对
MaiBot `dev@7a2bdc26a`，插件会通过 SDK 允许的原始 `cap.call` 调用保留 Host
返回的详细结果，避免当前普通 SDK 代理将其压缩成布尔值；旧 Host 或没有该原始
调用入口的 SDK 继续使用上述保守定位规则。

插件 `0.1.19` 明确保留无目标 ID 的 fallback。无论 Host 返回旧布尔成功结果，
还是返回 `sent=True` 但 `message_id=None` 的详细结果，插件都不会重新发送，
而是保留已发送阶段并沿用从目标真实历史定位消息的旧方法；无法可靠定位时
要求 Planner 保持沉默。

当前 source 路径 B 会在物理发送前通过真实 `view_forward_message` 取得完整
ToolResult，成功或达到既有 fallback 边界后才恢复转发请求；路径 A 则复用
Planner 已经查看的结果。路径 B 只处理 Planner 偶然漏掉路径 A 的情况，不是
Planner 可主动选择的常规流程。这些内容目前只用于 source 分享资格，不会注入
target 或声称已经与目标消息关联。

两条 source 路径最终共用同一 target 投递链。运行中的目标会话会在原有历史后
追加真实发送消息，冷启动会话则从消息库恢复近期群聊和该消息；目标 Planner
因此能看到近期本群上下文，但热运行时通常只显示该消息的前缀和目标 ID，冷启动
最多显示前四个节点预览。Planner 可使用真实消息自身展示的 `msg_id` 再次调用
`view_forward_message`；但插件没有强制该调用，也没有把 source 完整结果绑定
过去，所以“具备查看能力”不能等同于“初始主动轮已经获得完整内容”。

#### 低优先级上游完善

以下改进用于以正式 SDK/Host 契约替换现有兼容路径，不作为当前投递、回复或查看
能力的上线前置条件。

1. 保持现有 `send.forward` 的可选参数 `return_details`，默认值为 `False`；默认
   调用继续返回 `bool`，显式传入 `return_details=True` 时返回至少包含 `sent` 和
   最终目标 `message_id` 的结构化结果。Host 当前已实现这一部分；SDK 仍应在
   详细模式保留完整字典，或由 Host 避免给详细结果写入会触发布尔归一化的顶层
   `success`，使调用方不再依赖原始 `cap.call` 兼容层。
2. 详细结果必须在平台成功回执更新 `SessionMessage.message_id` 之后生成；
   平台没有返回最终 ID 时使用 `None`，不能返回发送前的 `send_api_*` 临时
   ID。
3. 复用 SDK `SendCapability.forward(..., **kwargs)` 已有的参数透传能力，并让
   `return_details=True` 的结果绕过布尔成功能力的归一化；不新增 SDK 方法，
   也不得另建平行的转发 capability 名称。
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

#### 保持现有安全边界

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

#### 低优先级验收条件

以下为替换兼容路径后的正式契约验收，不影响当前已经覆盖的实际效果。

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

### TODO-005：`view_forward_message` 等待图片识别完成

- 状态：等待上游
- 依赖：MaiBot Host
- 优先级：高
- 目标版本：待定

#### 背景

当前入站消息先以 `enable_heavy_media_analysis=False` 处理。`SessionMessage`
会将尚未命中缓存的图片识别安排到后台任务，并在识别完成前保留空内容或识别中
占位。`view_forward_message` 的完整内容路径虽然会尝试以
`enable_heavy_media_analysis=True` 补处理，但只在组件仍保有图片二进制数据时进入
该分支，未统一等待同一图片哈希已经存在的后台识别任务，也未在返回前重新确认
缓存描述已经写回。因而部分工具结果仍可能出现 `[图片，识别中.....]` 或空内容，
使 Planner 把未完成的图片识别误当成消息原文。

#### 目标

`view_forward_message` 返回成功的完整内容前，应等待本次转发消息及其嵌套节点中
可等待的图片识别完成，并使用最终缓存描述构造结果。正常入站处理仍可保持非阻塞；
只有明确请求查看完整内容的工具调用进入等待路径。未配置 VLM、识别失败或达到
等待上限时，必须返回稳定且可区分的降级结果，不得把识别中占位当作已完成内容。

#### 上游实施方向

1. MaiBot Host 为 `view_forward_message` 建立按图片哈希去重的等待流程：优先
   汇合已有后台识别任务，任务不存在时才按需启动一次同步识别，并在任务完成后
   从缓存重新读取描述。
2. 等待范围必须递归覆盖合并转发节点内的图片；等待结束后重新读取组件状态，
   再构造完整 ToolResult，不能使用等待前生成的文本快照。
3. 为等待设置明确的取消、超时和识别失败边界。失败分类、未配置 VLM 和超时
   的结果应保持现有安全语义，并通过已复核的稳定 ToolResult 状态让调用方知道
   内容是否完整；不得伪造图片描述或静默宣称已完成识别。
4. 保持普通消息入站、非查看场景和多模态 Prompt 的现有非阻塞行为；仅调整
   `view_forward_message` 的完整查看链路，并补充 Host 层单元测试和集成回归测试。

#### 验收条件

- 当图片识别已经在后台进行时，`view_forward_message` 会等待同一任务并返回已
  写入缓存的图片描述，而不是返回 `[图片，识别中.....]`；
- 当查看调用首次触发识别时，返回结果在识别完成后才生成，且同一图片不会重复
  发起并行识别请求；
- 转发节点内的多张图片都完成等待或被明确分类为失败、未配置或超时，不出现
  等待遗漏；
- 识别失败、超时和无 VLM 配置不会被误报为成功完整内容，普通入站处理仍不会
  被该等待路径阻塞；
- 原始消息路径和 Maisaka 上下文消息路径具有一致的等待与结果语义，并有对应
  回归测试。

### TODO-006：单次调用 `view_forward_message` 递归展开嵌套合并转发

- 状态：等待上游
- 依赖：MaiBot Host
- 优先级：低
- 目标版本：待定

#### 当前已实现的等效路径

当前 Host 的完整查看结果会保留当前层的发送者、顺序和组件内容；遇到内部
`ForwardNodeComponent` 时给出精确 `path`。Planner 可以携带该 `path`
再次调用 `view_forward_message`，逐层取得每个嵌套转发的实际内容。插件会区分
已完整查看和仍有嵌套的结果，让 source 决策在需要时继续查看，而不把当前层
误报为完整内容。

因此嵌套内容已经可被安全、逐层地查看，普通上下文中的前四条预览限制也保持不变。
本 TODO 不再是内容不可达问题；它只保留为减少额外工具调用和轮次的一次性递归
渲染优化。

#### 目标

上游可在单次“完整内容”查看中递归展开内部合并转发，减少 Planner 为进入下一层
而追加的调用次数。现有 `path` 逐层查看必须继续可用，并且普通消息上下文的短
预览仍保持既有条数限制。

#### 低优先级上游实施方向

1. 为完整查看结果提供统一的递归渲染器，使顶层和每个嵌套
   `ForwardNodeComponent` 使用相同的文本、图片、表情、回复和复杂组件渲染规则，
   并与原始消息路径、Maisaka 合成上下文路径以及现有 `path` 查看结果保持一致。
2. 为递归设置明确的最大深度、节点数或输出大小边界；达到边界时使用已复核的
   稳定截断状态，不得因异常嵌套造成无限递归、超大 ToolResult 或阻塞 Planner。
   如数据结构允许循环引用，还应按节点身份或路径去重并安全终止。
3. 递归展开应与图片识别等待、ToolResult 完整性状态协同：先完成需要等待的
   内容准备，再按原始节点顺序渲染；不得重复复制媒体二进制或改变普通预览的
   `FORWARD_PREVIEW_LIMIT` 语义。
4. 在 Host 中补充一层、两层和多层嵌套、混合媒体、深度/大小边界及异常结构的
   回归测试，并验证工具失败时仍保持现有错误处理语义。

#### 低优先级验收条件

- 单次 `view_forward_message` 调用在启用该优化后包含内部节点的实际内容，不再
  要求 Planner 继续通过 `path` 进入下一层；
- 每层发送者和节点顺序保持稳定，文本、图片识别结果、表情及其他组件不会因
  递归而丢失或重排；
- 原始消息和上下文合成消息两条查看路径的递归结果一致，并覆盖对应回归测试；
- 超过最大深度、节点数或输出大小时能稳定截断并明确标记，既不无限递归也不
  伪造完整内容；
- 现有普通转发预览的前四条限制和 Planner 可见 Tool 描述不因本项被扩大或改写。

### TODO-008：Host 让现有 `send.forward` 能按原消息 ID 转发

- 状态：待处理
- 依赖：MaiBot Host
- 优先级：中
- 目标版本：待定

#### 可行性结论

只修改 Host 是可行的，但本事项只能完成 Host 这一层，不能单独让当前 SnowLuma
Adapter 成功发送嵌套转发。

现有 `send.forward` 已经把 `messages` 字典列表和其他可选参数交给 Host，因此 Host
可以直接在当前方法中识别一种新的节点格式，不需要增加新的 SDK 方法。Host 也已经
能查到目标群实际使用的 Adapter，并能读取 Adapter 注册时附带的说明信息，所以可以
在发送前检查 Adapter 是否声明支持按原消息 ID 转发。

当前 SnowLuma Adapter 只会把发送者和消息内容重新拼成新节点，尚不能把 Host 的
引用节点转换成 NapCat 的 `node{id}`。因此 TODO-008 完成后，Host 遇到当前 SnowLuma
时应在调用 Adapter 前返回 `not_sent`，而不是尝试发送。真正的 QQ/NapCat 成功投递
属于 TODO-009。

SDK 可能把详细结果缩减成 `True` 或 `False`，这个问题继续由 TODO-004 负责。插件
当前通过 `call_host_method("cap.call")` 取得 Host 原始详细结果的兼容方式可以暂时
保留；TODO-008 不要求修改或发布 maibot-plugin-sdk。

#### 背景

嵌套合并转发不能靠“把内层内容再编码一遍”可靠保留。正确做法是让 QQ 复用原始
消息记录：例如 source 中的 `A、X、D`，其中 `X` 本身是合并转发时，target 发送的
外层合并转发应引用这三条原始消息。QQ 便能自行把 `X` 显示为可继续打开的内层
合并转发。

NapCat 的合并转发发送接口接收的是 `node` 列表。一个节点要么携带现有消息的
`id`，要么携带重新构造的 `content`，两者不能混用。本事项中的“引用节点”指的是
`{"type": "node", "data": {"id": "<真实消息 ID>"}}`，不是普通消息段中的
`forward` 字典。

#### 本端引起的问题

当前 Host 的 `ForwardNodeComponent` 只会保存每个节点的发送者和消息内容，相当于
把原消息重新拼成一条新消息。它不能保存“这里直接使用某条已有 QQ 消息”的意思。
Host 把消息转换成字典、写入存储或重新读出时，也始终把节点当成一份待重建的内容
列表，因此原消息 ID 无法沿正常发送链路保留下来。

此外，Adapter 返回发送结果后，Host 会把失败及其原因缩减成一个 `None`；
`send.forward` 最终只能给出简单的成功或失败。插件因而无法分清“还没有发送就发现
不支持”和“请求已经发出，但因为超时而不知道是否真的发送成功”。

#### 原因

原来的 `send.forward` 假定每个节点都由“发送者信息 + 要重新发送的内容”组成，没有
地方表示“直接引用这条已有 QQ 消息”。Host 在发送前也只知道目标群有可用的
Adapter，不知道这个 Adapter 能否保留嵌套转发；即使 Adapter 给出了具体错误，原因
也会在 Host 的发送服务与 `send.forward` 返回处之间丢失。

#### 目标

让现有 `send.forward` 同时支持两种输入：一种继续传入发送者和消息内容，另一种只
传入经过验证的原消息 ID。两种方式都必须经过 Host 现有的消息存储、目标上下文同步
和最终消息 ID 处理。不得另外增加一个新的发送方法。Host 必须先确认目标群实际使用
的所有 Adapter 都支持按原消息 ID 转发；不能证明支持时，不发送任何外层节点。

#### 最小修复方案

1. 在 Host 内部增加一种专门表示“引用已有消息”的节点，并让
   `ForwardNodeComponent` 同时保存现有的“发送者 + 内容”节点和这种新节点。新节点
   只保存经过验证的 QQ/NapCat 原消息 ID，不复制或伪造内层内容。
2. 更新负责消息转换和恢复的 `MessageSequence`、`PluginMessageUtils` 以及消息存储
   代码。无论机器人一直运行还是重启后从存储恢复，“引用原消息”的信息都不能变成
   一份普通的 `forward` 内容列表。
3. 只扩展 Host 中现有的 `send.forward` 处理器。`messages` 中每一项都要明确说明
   自己是现有的“发送者 + 内容”格式，还是下面这种引用格式：

   ```json
   {
     "node_type": "reference",
     "message_id": "<QQ/NapCat 原消息 ID>"
   }
   ```

   插件还要随同一次 `send.forward` 调用传入 `source_stream_id` 和
   `source_message_id`，分别表示来源聊天会话和收到的外层合并转发消息。插件不能
   直接传入 OneBot 原始 `node` 字典。
4. Host 根据 `source_stream_id + source_message_id` 读取原始 source 消息，确认它是
   合并转发，并逐项核对引用 ID 确实来自该消息且顺序一致。缺少消息、ID 不属于该
   source、顺序不一致或列表同时混入两种节点格式时，整次调用在发送前失败。
5. 没有 `node_type=reference` 的现有调用继续按“发送者 + 内容”处理，参数和行为均不
   改变。这样其他插件无需配合升级，也不会被误判为正在请求按原消息 ID 转发。
6. Host 读取 Adapter 注册信息中的 `forward.reference.v1`，用它判断 Adapter 是否
   完整支持按原消息 ID 转发。TODO-008 只实现 Host 侧的读取和检查，不修改任何
   Adapter；SnowLuma 的声明由 TODO-009 增加。没有可用 Adapter，或其中任何一个
   没有声明支持时，Host 直接返回 `not_sent`，也不调用任何 Adapter 发消息。
7. Host 要一直保留 Adapter 返回的原始发送结果，并在 `return_details=True` 时返回
   一份带明确字段的字典，至少包含 `delivery_state`、`error_code`、`error`、`sent`
   和 `message_id`。其中：`sent` 表示已确认发送；`not_sent` 表示在本地检查阶段就
   取消；`rejected` 表示链路明确拒绝；`unknown` 表示超时或无法确认。只有确认发送
   后才允许返回最终消息 ID。

#### 修复后插件如何调用

调用的方法名、目标 `stream_id`、历史同步参数和 `return_details=True` 都不变。变化
只在 `messages` 的内容，以及新增的 source 身份参数。

修复前，插件把每个节点的发送者和内容都交给 Host，由 Host 和 Adapter 重新拼装：

```python
await ctx.send.forward(
    [
        {
            "user_id": "10001",
            "nickname": "甲",
            "message_id": "A",
            "segments": [{"type": "text", "data": "消息 A"}],
        },
        {
            "user_id": "10002",
            "nickname": "乙",
            "message_id": "X",
            "segments": [{"type": "forward", "data": [...] }],
        },
    ],
    target_stream_id,
    return_details=True,
    storage_message=True,
    sync_to_maisaka_history=True,
)
```

修复后，只要列表中存在嵌套转发，插件就把整个 `A、X、D` 列表改为原消息 ID 引用，
不再把内层内容交给下游重新拼装：

```python
await ctx.send.forward(
    [
        {"node_type": "reference", "message_id": "A"},
        {"node_type": "reference", "message_id": "X"},
        {"node_type": "reference", "message_id": "D"},
    ],
    target_stream_id,
    source_stream_id=job.source_stream_id,
    source_message_id=job.source_message_id,
    return_details=True,
    storage_message=True,
    sync_to_maisaka_history=True,
)
```

两次调用使用的仍是同一个 `send.forward`。主要区别如下：

| 对比项 | 修复前 | 修复后遇到嵌套转发时 |
| --- | --- | --- |
| 节点内容 | 传发送者和全部消息段 | 只传真实原消息 ID |
| 内层转发 | 下游尝试重新拼装，可能被跳过 | QQ/NapCat 复用原消息记录 |
| source 身份 | 不传 | 传来源会话和外层消息 ID，供 Host 核对 |
| 缺少真实 ID | 可能继续使用占位 ID | 不调用 Adapter，整次失败 |
| Adapter 不支持 | 可能在转换时丢内容 | Host 在物理发送前返回 `not_sent` |
| 普通无嵌套转发 | 使用现有格式 | 保持现有格式，不受影响 |

当前插件为避免 SDK 丢失详细结果，会优先把同一组参数通过
`call_host_method("cap.call")` 交给 Host，并指定 capability 为 `send.forward`。
TODO-008 完成后仍可沿用这种传输方式；它只是取得同一个 `send.forward` 的原始返回
值，不是新增发送方法。

#### 验收条件

- Host 能将 `A、X、D` 的三个真实消息 ID 保留为三个引用节点，而不展开或重组 `X`。
- Host 能按来源会话与外层消息核对 `A、X、D`；缺少真实 ID、跨平台、来源不可信、
  顺序不一致或首期排除的卡片时，在调用 Adapter 前拒绝整个列表。
- 目标群实际使用的任一 Adapter 没有声明支持时，不产生任何物理发送；有多个
  Adapter 时，也不会出现只向其中一部分先发送的情况。
- Host 原始结果能区分 `not_sent`、`rejected`、`unknown` 和 `sent`；成功结果才返回
  最终目标消息 ID。SDK 是否保留这份详细结果不作为 TODO-008 的验收条件。
- Host 仍只提供现有 `send.forward`；已有插件继续传发送者和消息内容即可，不需要
  修改。按原消息 ID 转发时，也不能绕过同一套存储、历史同步与最终消息 ID 处理。
- 引用节点转换成字典、写入消息存储、重启后重新读出，以及已有普通合并转发测试
  均不回归。
- 在 TODO-009 尚未完成、SnowLuma 尚未声明支持时，Host 测试应确认返回 `not_sent`
  且没有调用 Adapter；真实 QQ/NapCat 成功展示不属于 TODO-008 的验收范围。

### TODO-009：SnowLuma Adapter 映射并验证原生引用节点

- 状态：待处理
- 依赖：TODO-008 / SnowLuma Adapter / QQ-NapCat
- 优先级：中
- 目标版本：待定

#### 背景

SnowLuma Adapter 是 Host 引用节点在 QQ/NapCat 上的最后一段实现。它不负责决定是否
分享，也不负责绕过 Host 直接调用 NapCat；它只负责把 Host 已验证的引用节点准确地
编码为 NapCat 能接收的 `node{id}`，并返回可判定的结果。

#### 本端引起的问题

当前入站处理会展开 `forward` 以便查看内容，但没有单独保存可用于引用的真实节点
消息 ID。出站时，Adapter 会把外层节点内容交给普通消息段转换器；该转换器没有
处理 `forward` 或 `node`，因此会静默跳过内层转发。混合节点会缺少一部分内容，纯
内层转发节点还可能完全消失。

#### 原因

当前实现走的是“重新构造带 `content` 的自定义节点”路径，而引用节点属于
`send_group_forward_msg`/`send_private_forward_msg` 的最外层 `messages` 列表。把它
误当作普通消息段处理，既会放错层级，也会被未知段的兜底逻辑丢弃。

#### 目标

在已具备 TODO-008 契约时，让 SnowLuma Adapter 保留并发送原生引用节点；在没有可
验证 ID 或没有真实验证的平台能力时，明确报告不支持，绝不发送删减后的外层内容。

#### 最小修复方案

1. 入站解析时把“展示用节点 ID”和“可引用的真实外部消息 ID”分开保存。只有来源
   明确的 `message_id` 才可成为引用 ID；不能用用户 ID、内部转发 ID、空值或插件
   占位值替代。
2. 在出站合并转发转换的最外层识别 Host 的引用节点，直接生成
   `{"type": "node", "data": {"id": "<真实消息 ID>"}}`。不要把该对象送入普通
   段转换器，也不要改写成 `forward` 段或带 `content` 的伪造节点。
3. 在调用 NapCat 前再次校验引用 ID 和节点结构。校验失败时返回 Host 可识别的
   `not_sent`；请求已经发出后未获确认时返回 `unknown`，不能冒充“不支持”。
4. 仅在单元测试和真实 QQ/NapCat 联调均通过后声明 `forward.reference.v1`。在此之前
   不声明该能力，交由 Host 在发送前拒绝。

#### 验收条件

- Adapter 对 `A、X、D` 输出的 NapCat `messages` 恰好是三个有序的 `node{id}`；不产生
  `content` 重编码，也不改变节点顺序。
- 缺少有效 ID、夹带未支持卡片或结构非法时，Adapter 不发出任何 `send_*_forward_msg`
  action。
- 一层、多处并列和多层嵌套均覆盖“文字前后夹有内层转发”和“节点仅含内层转发”
  场景；没有节点被静默跳过。
- 在真实 QQ/NapCat 中验证 target 外层与内层均可打开，`X` 仍是嵌套转发而不是展开后
  的文本或重组节点；同时验证明确拒绝与超时的不同回执。

### TODO-010：插件按 target 使用引用节点并持久化投递结果

- 状态：待处理
- 依赖：TODO-008 / TODO-009
- 优先级：中
- 目标版本：待定

#### 背景

插件已经能从 source 合并转发中读取节点，但当前只是浅拷贝字典后调用普通
`send.forward`。这不是“原样保留”的证明：下游仍可能重新编码、丢弃或把嵌套转发
展开。当前缺少消息 ID 时还会生成占位 ID，失败时也缺少“结果未知”的持久化状态。

#### 本端引起的问题

插件无法判断某个 target 的实际 Adapter 是否支持原生引用节点，且当前通过
`cap.call` 保留详细结果属于 SDK 兼容绕过。若网关超时或回复丢失，已有状态只能看到
普通失败；之后再次处理同一 source 时，存在重复物理发送的风险。

#### 原因

插件仍使用为自定义节点设计的旧发送能力，并把“解析时可见的 ID”与“已验证的外部
消息 ID”混在一起。状态机只重点持久化成功发送和后续 Planner 阶段，不能表达一次
投递已进入未知状态。

#### 目标

在不改变 source Planner 流程、Tool 描述、Prompt、主动任务或 target 上下文注入的
前提下，按 target 选择正式引用节点能力，并让任何无法证明完整性的投递安全结束。

#### 最小修复方案

1. 为解析层增加引用计划：发现嵌套转发后，按原顺序收集整个外层列表的真实 source
   消息 ID，例如 `A、X、D`。任一项没有可验证 ID、跨平台、来源不可信或包含首期
   排除的卡片时，不构造部分计划。
2. 对含嵌套转发的任务继续调用现有 `send.forward`，但改用 TODO-008 定义的引用
   参数格式；不直接调用 Adapter。TODO-004 解决 SDK 详细结果问题后，再移除
   `cap.call` 兼容方式。普通、无嵌套的合并转发继续使用现有参数，缩小行为改变
   范围。
3. 按每个 target 的结构化结果推进状态：`sent` 才记录最终消息 ID 并允许目标 Planner
   入队；`not_sent`、`rejected` 和 `unknown` 都不触发目标 Planner，也不把删减内容
   作为降级版本重发。
4. 在持久化状态中增加 target 级的终态。`unknown` 必须禁止之后的自动重发；
   `not_sent` 和 `rejected` 也只标记当前任务失败并继续后续 target。后续升级能力后
   如需再次尝试，必须由新的、明确发起的任务进行。
5. 新增失败原因若会进入 Planner 可见的 ToolResult，必须先单独提交完整文本和注入
   位置供人工复核；本事项不得自行改写已通过复核的 Planner 可见内容。

#### 验收条件

- 插件夹具覆盖一层、多处并列和多层嵌套，固定验证引用计划中的 ID、顺序、发送者和
  媒体字段；解析结果不允许使用合成占位 ID。
- 一个 target 在发送前确认不支持时只失败该 target，后续 target 仍按配置顺序处理；
  任何 target 都不会收到删减后的部分内容。
- `unknown` 在重载后仍能阻止自动重发；`sent` 后 Planner 入队失败时仍沿用现有规则，
  只补做后续阶段而不重复物理发送。
- 成功发送后继续持久化最终目标消息 ID；目标历史同步、source 永久防重和 target
  Planner 的既有静默安全边界不回归。
- 不改动 Tool 名称、描述、Prompt、提醒、主动任务或 source 完整内容向 target 的
  注入方式。

### TODO-011：Host 为已发送的合并转发保留可读的目标 Planner 上下文

- 状态：等待上游
- 依赖：MaiBot Host
- 优先级：高
- 目标版本：待定

#### 背景

任意插件调用 `send.forward(..., sync_to_maisaka_history=True)` 后，真实合并转发
可以成功发送到 target 群。但当 target 的 Maisaka 已热运行时，Host 将这条已发送
消息按普通历史项写入 Planner 上下文，顶层合并转发组件没有被渲染。

此时真实目标消息 ID 和 `<message ...>` 消息头仍会存在，但正文、转发预览和图片
占位都缺失，Planner 实际上看到的是一条空内容消息。普通入站合并转发会识别复杂
消息并生成预览；冷启动后从消息库恢复的消息也会重新走该路径。因此同一条真实消息
在热运行和冷启动恢复后的 Planner 上下文语义不一致。

#### 目标

让所有使用 `send.forward` 并请求同步 Maisaka 历史的插件，在发送顶层合并转发后，
都能让 target Planner 获得非空、可理解的消息上下文，同时保留真实目标消息 ID。

该能力只补齐 Host 的真实消息历史渲染，不把 source 群完整查看结果、原始转发节点
或媒体二进制重复写入 target 上下文。

#### 实施方向

1. Host 在已发送消息同步 Maisaka 历史时识别顶层 `ForwardNodeComponent`，并复用
   普通入站消息的复杂消息渲染，或建立语义等价的专用历史项。
2. 统一热运行同步与冷启动恢复的转发预览语义，避免一个路径只有消息头、另一个路径
   有转发预览。
3. 保留发送成功后的真实 `SessionMessage`、最终目标消息 ID 和回复锚点，使 Planner
   可按目标消息自身的 ID 调用 `view_forward_message` 查看完整内容。
4. 预览只提供必要的转发标记或既有受限节点预览；不得通过复制 source 完整内容、
   媒体 Base64 或合成重复消息来规避渲染缺失。
5. 普通文本、图片、回复和已有非转发发送历史同步路径保持原有行为。

#### 验收条件

- 文字、图片和图文混合的合并转发成功发送并同步历史后，target Planner 的实际
  `generation_attempts[].wire_request.input` 不再退化为只有 `<message ...>` 的消息头；
- Planner 至少能看到稳定的合并转发标记或既有受限预览，并保留真实目标消息 ID；
- Planner 可按该目标消息 ID 调用 `view_forward_message` 查看完整内容；
- 热运行同步与冷启动后从消息库恢复时，Planner 得到等价的转发上下文语义；
- 不重复注入 source 完整内容、原始转发节点或媒体二进制；
- 普通消息发送、既有历史同步和回复锚定行为不回归；
- Host 回归测试直接断言实际 Provider wire input，而不是只检查监控或 WebUI 投影。

### TODO-012：解析嵌套合并转发中的 QQ XML 聊天记录卡片

- 状态：等待上游
- 依赖：MaiBot-Napcat-Adapter / MaiBot Host / QQ-NapCat
- 优先级：中
- 目标版本：待定

#### 背景

QQ/NapCat 会把部分“合并转发中的合并转发”表示为 XML 聊天记录卡片，而不是
OneBot `forward` 消息段。生产环境已观察到该卡片含有
`brief="[聊天记录]"`、`action="viewMultiMsg"`、`m_fileName="MultiMsg_..."`
和 `tSum` 等字段；外层合并转发能够入站，但对应内层节点没有稳定的
`message_id`。

当前入站结果将该节点保留为 `DictComponent(type="xml")`。因此
`view_forward_message` 只能渲染 `[xml消息]`：其嵌套路径解析只收集
`ForwardNodeComponent`，不会把 XML 卡片当作可继续展开的转发节点。该问题与
TODO-006 不同；TODO-006 处理已经结构化为 `ForwardNodeComponent` 的嵌套转发，
本项处理的是结构尚未在 Adapter/Host 入站边界恢复的情况。

#### 目标

仅在能够取得并验证真实内层转发数据时，将 QQ XML `viewMultiMsg` 卡片转换为
嵌套 `ForwardNodeComponent`，使现有 `view_forward_message` 路径机制可以继续
展开。无法安全解析或查询时，保留其为不透明卡片；不得伪造转发 ID、猜测查询参数、
重发消息，或宣称已经取得内层完整内容。

该能力属于平台 Adapter 与 Host 的入站语义恢复，不由本插件直接调用 Adapter、
旁路 Host 存储，或把 source 完整内容重复注入 target Planner 上下文。

#### 实施方向

1. MaiBot-Napcat-Adapter 在转发节点内容中识别 `action="viewMultiMsg"` 的 XML
   卡片，并以不执行 XML 外部实体、不信任卡片展示文本的方式提取最小必要元数据。
2. 先依据 NapCat 官方接口和真实返回样本，确认从该卡片或外层转发详情取得内层
   `get_forward_msg` 查询句柄的正式方法。`m_fileName`、`serviceID` 和 `resid`
   不得仅凭名称或数值相同就当作 API 的 `message_id` 或 `id` 使用。
3. 只有取得已验证的查询句柄后，才调用 `get_forward_msg`，归一化其版本差异并递归
   构造 `ForwardNodeComponent`。实现应限制递归深度、节点数量和查询时间，并检测
   循环引用或重复节点。
4. 查询句柄缺失、接口失败、结果格式未知或达到安全边界时，保留原始
   `DictComponent`；不得将普通 XML、分享卡片或失败结果误分类为转发。若需要新增
   Planner 可见的失败说明或结构，须先单独提交完整文本和注入位置供人工复核。
5. 保持已有结构化嵌套转发、普通 XML/JSON 卡片、消息落库、source 防重、target
   投递顺序和真实消息 ID 语义不变。

#### 验收条件

- Adapter 回归夹具覆盖生产样式的 `viewMultiMsg` XML 卡片及至少一种字段变体；在
  存在正式查询句柄时，内层结果被表示为 `ForwardNodeComponent`，不再是
  `DictComponent(type="xml")`。
- Host 的 `view_forward_message` 首次查看该外层消息时给出既有的嵌套路径信息；
  使用该路径后能看到内层节点的顺序、发送者与内容，且不把其他兄弟节点重排或丢失。
- 缺少已验证句柄、查询超时、返回空数据、循环引用和超过资源上限时均安全保留为
  不透明卡片；不会猜测 ID、重复调用物理发送或把不完整内容标为完整。
- 普通顶层合并转发、已有 `ForwardNodeComponent` 嵌套转发、普通 XML/JSON/分享
  卡片和非转发消息保持原有解析及发送行为。
- 使用真实 QQ/NapCat 环境验证至少一个嵌套转发样本，记录原始 OneBot 载荷、正式
  查询参数和 `get_forward_msg` 返回结构；仅有单元测试或模拟 Adapter 不视为平台
  能力已经验证。

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
