# 麦麦自主跨群转发插件

让 MaiBot 在 source 白名单群聊中自主判断一则合并转发是否值得分享。Planner
应先调用 `view_forward_message` 查看完整内容，再决定是否请求转发。若模型偶然
漏看并直接请求，插件会在发送前补做一次真实查看；这条路径 B 只是路径 A 的
安全 fallback，不是提供给 Planner 主动选择的常规流程。插件随后按照 target
白名单顺序发送消息，并按配置触发目标群 Planner。

当前版本为 `0.2.5`，仅面向 SnowLuma Adapter 下的 QQ 群聊进行验证。本版本
完整的目标消息 ID 与回复锚点能力基于 MaiBot Host 分支
`1.1.2-send-forward-result` 开发；该分支从 `upstream/dev@078ee34d` 创建，
需包含 Host commit `dfaf8e8a`。它属于配套 Host 分支功能，尚不能
视为当前官方 Host 的通用能力。使用未包含该修改的 Host 时，插件仍能完成
转发，但拿不到目标消息 ID，目标 Planner 会回退到无法可靠定位时保持沉默的
兼容路径。即使 Host 返回详细结果，只要其中没有最终 `message_id`，也会沿用
同一 fallback；该机制将在上游正式提供并验证稳定契约后再按 TODO 移除。

## 安装要求

- MaiBot `1.1.0` 或更高兼容版本
- maibot-plugin-sdk `2.7.1` 或更高兼容版本
- 已启用并提供 QQ 群聊能力的适配器；本插件目前仅在 SnowLuma Adapter `0.8.4` 或更高的 `0.x` 版本下完成验证

SnowLuma Adapter 不是本插件的 manifest 强依赖：插件不调用其私有 API，只通过 MaiBot 的通用 capabilities 工作。因此适配器暂未加载时不会阻止本插件加载；实际转发仍需要运行环境提供所声明的能力。

插件必须位于 MaiBot 的 `plugins/MaiBot_ForwardMessagesAuto_Plugin/` 目录。运行时配置由 MaiBot 根据 `plugin.py` 中的 `config_model` 生成到 `config.toml`，该文件不应提交。

## 当前配置

```toml
[routing]
source_groups = ["123456789"]
target_groups = ["234567890", "345678901"]

[behavior]
view_failure_fallback_threshold = 2
trigger_source_planner = false
trigger_target_planner = true
```
source 和 target 均只填写 QQ 群号字符串。`target_groups` 的列表顺序就是每次任务的发送顺序。未列入白名单的群不能触发或接收自主转发。
`trigger_source_planner` 是独立的 source 方向开关，默认关闭。启用后，source
白名单群收到真实合并转发时，插件会绕过普通回复频率抽样，强制触发一次本群
Planner；它只保证主动任务入队，不要求 Planner 一定查看、转发或回复。
`view_failure_fallback_threshold` 表示允许使用摘要或预览降级前，
`view_forward_message` 对同一 source stream 与 `msg_id` 必须连续发生的
可重试故障次数，最小值为 `1`，默认值为 `2`。可重试故障包括展开异常、
工具调用或注册能力暂时不可用；参数或消息错误、非合并转发和未知失败不计入
阈值。空内容采用固定的诊断策略：首次要求再查看一次，连续第二次仍为空才
允许降级。

配置支持 MaiBot 原生热更新。修改白名单等字段并保存合法 TOML 后，插件会
继续在线并立即刷新路由，无需手动关闭再开启。若编辑器保存过程中短暂产生
未闭合数组等无效 TOML，插件会继续使用最近一次有效配置，等待下一次合法
保存；如果文件最终仍不合法，新值不会生效，请根据日志修正语法。

## 本地调试统计

开发调试时，可在启动 MaiBot 前显式设置环境变量：

```bash
export MAIBOT_FORWARD_DEBUG_STATS=1
```

插件重载后，会统计当前插件版本内 source 白名单群观察到的唯一外部 QQ 合并
转发数，以及其中 Planner 实际发起转发请求的唯一消息数，并在 debug 日志中
以 `发起转发数/观察总数（比例）` 展示。该统计独立于
`trigger_source_planner`；重复 Hook、重复 Tool 调用和路径 B 的系统恢复调用
不会重复计数。

开关默认关闭且不进入 `config.toml`，因此不会改变配置版本。统计只写入 MaiBot
为插件分配的运行时数据目录：
`context.paths.data_dir/debug_forward_stats.local.json`。文件按插件版本分桶，
只保存聚合整数和 SHA-256 匿名去重键，不保存原始 stream、消息 ID、群号、正文
或媒体。路径落入插件源码树、文件损坏或读写失败时，插件只记录 warning 并跳过
统计，不影响正常转发，也不会在仓库中创建替代文件。

关闭时删除该环境变量并重载插件即可。真实统计文件只属于本机调试数据，不应
提交、打 tag、打包发布或上传到 GitHub。

## 工作流程

1. source 白名单群收到合并转发；启用 `trigger_source_planner` 时，插件先
   确认真实消息已经进入 Host 消息库，再强制触发一次本群 Planner。
2. source 群 Planner 必须先调用内置 `view_forward_message` 查看完整内容，
   再决定是否通过 `tool_search` 发现并调用 deferred Tool
   `request_cross_group_forward`；合并转发内部节点可以最初来自其他群。
3. 若 Planner 偶然漏看并单独调用转发 Tool，路径 B fallback 会在发送前把本轮
   调用改写成一次真实 `view_forward_message`。插件只接受精确匹配合成调用 ID 与
   `msg_id` 的 ToolResult；成功或达到既有 fallback 边界后，才在后续 Planner
   内部轮恢复原请求。可重试故障和首次空内容会按既有阈值继续查看。
4. 正式 Tool 处理器消费与真实 Planner session 和 `msg_id` 绑定的一次性凭据，
   再按可信 session 解析 source 群号，校验白名单、消息归属、消息类型和永久
   防重。模型提供的 `platform`、`group_id`、`stream_id` 等上下文字段不参与授权。
5. 插件按 `msg_id + source stream` 读取原始合并转发节点，按 target 白名单
   顺序执行投递，并等待全部目标产生真实结果后再向源群 Planner 返回。
6. 每个 target 发送时请求 Host 将真实合并转发消息同步到 Maisaka 历史，不
   重复注入 source 群已经展开的完整内容。
7. 启用 `trigger_target_planner` 时，插件强制触发目标群 Planner。当前 Host 会让
   该 Planner 保留上下文窗口内的目标群近期聊天；冷启动时能看到新消息的有界
   预览，热运行时当前通常只看到消息前缀和目标 ID。Planner 可以按目标消息自身
   展示的 `msg_id` 再调用 `view_forward_message` 查看完整内容。插件目前既不
   强制这次目标侧查看，也不把 source 查看结果关联到目标消息，因此不能保证
   完整内容已经进入目标 Planner 的本轮判断。

source 触发通过 `chat.receive.after_process` Hook 在普通回复频率判断前识别
QQ 合并转发，再等待同一 `source stream + msg_id` 可查询后调用
`maisaka.proactive.trigger`。适配器提供的 `self_id`/账号标记与已持久化的目标
消息 ID 会共同排除机器人自己的转发回声；成功入队的 source 消息键永久保存
在插件数据目录中，插件重载不会重复触发。

转发 Tool 默认位于 MaiBot 的 deferred tools 池中。各群 Planner 会看到其简要
说明，但只有通过 `tool_search` 发现后才能取得完整参数并调用。`after_response`
的 EARLY 阶段会在不执行 I/O 的情况下清除所有转发调用中的未声明参数，并为
合法调用签发一次性内部凭据；凭据绑定 Hook 提供的真实 Planner session 与
`msg_id`。输入参数若回放旧内部凭据，EARLY 只撤销该凭据并换发新值，不影响
同 session 的其他并发调用；EARLY 另生成只沿当前 Hook 分发链传递的本轮随机
标记。LATE 编排只保留与该标记、当前 session 和 `msg_id` 全部精确匹配的凭据，
绝不重新签发或改绑。正式处理器必须成功消费凭据，并根据该 session 的可信
Host 映射解析 source 群号；缺失、未知、错配或已经消费的凭据都会被拒绝。
因此 deferred discovery 不是授权边界，模型也不能通过伪造 `platform`、
`group_id`、`stream_id` 或目标群绕过校验。Source 表示读取并发起分享的群聊，
不要求合并转发内容最初由该群产生；target 始终只来自配置。

## 上下文资格与失败恢复

路径 A 中，Planner 先调用 `view_forward_message`，再直接请求转发。插件通过
`maisaka.planner.before_request` Hook 按当前聊天流同步完整结果；成功结果只要
仍在本轮 Planner 上下文中就持续合法，不设置缓存时间或 TTL，被上下文裁剪后
立即失效。成功查看后，插件会在紧接着的 Planner 续轮末尾追加一次判断提醒；
若请求被新消息打断，提醒会保留到 Planner 真正返回为止。

路径 B 是路径 A 的漏调用 fallback：Planner 未查看便单独调用转发 Tool 时，
`after_response` 编排器先完成可信预检，再把该批次替换成唯一的真实查看调用。
Planner 可见的 Tool 描述不会把这条路径作为正常选择。下一内部轮只有在精确
ToolResult 仍可见时才推进：成功或达到既有 fallback 边界时恢复原请求；可重试
故障尚未达到阈值或首次空内容时生成新的查看调用；参数、消息类型、终止或无法
安全分类的失败最终由正式处理器拒绝。混合多工具批次不会被插入、删除或重排；
其中未查看的转发调用会沿用正式处理器的拒绝语义。路径 B 的 pending 仅保存在
当前插件进程，配置更新、插件卸载或所需结果丢失都会安全清除，不会跨边界恢复
旧请求。若 LATE 编排 Hook 超时或失败，EARLY 清洗和一次性凭据仍已生效，原
调用只会降级到正式处理器拒绝并要求查看，不会跳过查看直接发送。

当前上下文没有成功查看结果且路径 B 没有完成续轮编排，或可重试故障尚未达到
配置阈值时，转发 Tool 会拒绝创建任务并要求 Planner 重新查看。只有连续可重试
故障达到 `view_failure_fallback_threshold` 或连续两次返回空内容时，才会依次
使用 Planner 提供的 `content_summary` 和原消息预览降级。参数或消息错误必须
修正；确认不是合并转发以及无法安全分类的失败不会因重复调用开放降级。由于
当前 Host 未向 Planner Hook 暴露 ToolResult 的成功状态，插件暂时依据已知
稳定失败文案进行分类；未来主程序改进方向见 [TODO](docs/TODO.md)。

每个目标群分别记录以下阶段：

```text
sent → planner_queued
```

状态按 `source stream + msg_id + target` 永久保存在 MaiBot 为插件分配的
数据目录中，不按时间清理。若发送已经成功，但 Planner 入队失败，再次发起
同一请求时会从未完成阶段继续。旧版本的 `context_appended` 状态继续按已发送
处理；路由新增 target 时只处理新群，不会重复发送已经完成的旧 target。
`v0.1.11` 的路由级状态会在加载时自动合并到新结构。新版 Host 返回的目标
消息 ID 会与 `sent` 阶段一并持久化，插件重载后仍可继续作为回复锚点。

单个 target 失败不会阻止后续 target。当前版本只保证发送和主动任务入队按白名单顺序发生；不同目标群的 Planner 可能在入队后并发推理。
转发 Tool 只有在全部 target 完成当前要求的处理阶段后才返回成功；部分失败
或全部失败会返回实际发送数、完整处理数和失败阶段，不再把任务入队视为
转发成功。

配套 Host 分支会保存真实发送消息，并可通过
`send.forward(return_details=True)` 返回平台最终目标消息 ID；插件取得 ID 后
将它随主动任务传给目标 Planner，作为唯一的 `reply` 锚点。该能力尚未成为
官方 Host 的稳定通用契约；Host 不返回最终 ID 时，插件仍完成转发，但目标
Planner 无法可靠定位刚发送的真实消息时必须保持沉默。

路径 A 和路径 B 在 source 侧完成查看后会汇入同一条 target 投递链。当前检查的
Host 对运行中的目标会话会把真实发送消息追加在原有 Maisaka 历史之后；冷启动
会话会在主动任务入队前从消息库恢复近期历史。因此两条路径都能保留目标群近期
上下文，但表现不完全相同：热运行时的普通 `SessionBackedMessage` 当前通常只向
Planner 显示消息前缀和目标 ID，冷启动恢复出的 `ComplexSessionMessage` 最多
显示前四个转发节点。即使 `send.forward` 没有把最终 `message_id` 返回给插件，
目标历史中的真实消息本身仍可展示自己的 `msg_id`；Planner 在能唯一定位时具备
再次调用 `view_forward_message` 的能力。

不过，当前插件不会把 source 侧取得的完整查看结果与目标群真实消息建立结构化
关联，也不要求目标 Planner 自动再次查看。因此不能宣称目标 Planner 在初始
主动轮已经获得完整内容，更不能据此保证准确回复；精确回复仍依赖上游提供稳定
目标 ID 和幂等内容关联能力。详见
[TODO-003](docs/TODO.md#todo-003稳定取得目标消息-id-并关联完整内容)。

## 测试

在本仓库目录执行：

```bash
PYTEST_ADDOPTS="-p no:cacheprovider" ../../.venv/bin/python -m pytest -q
RUFF_CACHE_DIR=/tmp/maibot-forward-ruff ../../.venv/bin/python -m ruff check plugin.py forward_messages_auto tests
RUFF_CACHE_DIR=/tmp/maibot-forward-ruff ../../.venv/bin/python -m ruff format --check plugin.py forward_messages_auto tests
```

人工联调应至少准备一个 source 群和两个 target 群，并验证：

- 路径 A 中，source Planner 先查看完整内容，再决定是否调用转发 Tool；
- 路径 B 只覆盖 Planner 偶然漏看后的单独请求，并会在发送前先产生真实查看
  调用，成功或达到既有 fallback 边界后才恢复原请求；
- 混合多工具响应不被重排，缺少可信凭据、非 source 会话或编排超时都不能
  绕过查看与正式处理器校验；
- 启用 `trigger_source_planner` 后，低回复频率下的 source 合并转发仍会触发
  Planner，但不会自动查看、转发或回复；
- target 按配置顺序收到合并转发；
- 两条路径的目标 Planner 都能看到上下文窗口内的目标群近期聊天；冷启动最多
  自动看到前四个节点预览，热运行时通常只有消息前缀和目标 ID。Planner 可按
  目标消息自身的 `msg_id` 再查看完整内容；当前不把自动完成目标侧查看或能依据
  source 完整内容准确回复作为验收结论；
- 非白名单群无法调用或接收；
- 同一条消息不会重复发送；
- 一个 target 失败后，后续 target 仍会继续。

详细实现约束和已知限制见 [功能设计文档](docs/forward-message-design.md)。
