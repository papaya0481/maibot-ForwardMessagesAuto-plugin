# 自主跨群转发功能设计

## 1. 目标与范围

本插件让 MaiBot 在 QQ 群聊中自主判断一则合并转发是否值得分享，并将其发送
到预先配置的其他群聊。源群 Planner 负责判断“是否值得分享”，插件负责权限、
路由、发送和防重；目标群 Planner 只有在可靠定位真实目标消息且上下文足够时
才决定是否发表一句自然的整体看法，否则保持沉默。将 source 完整内容与目标
消息关联仍依赖上游能力。

当前仅面向 SnowLuma Adapter 下的 QQ 群聊验证，不修改 MaiBot 主程序。Source
和 target 都必须显式加入白名单；Planner 不能临时指定目标群，也不负责选择
目标群。

本文先说明当前已经实现的行为，再记录尚未实现的未来展望。未来展望不代表
当前版本已经支持，也不应据此修改现有配置。

## 2. 当前已实现功能

### 2.1 当前流程

1. Source 白名单群收到一条合并转发消息；启用
   `trigger_source_planner` 时，插件强制触发一次 source Planner。
2. Source Planner 必须先调用 `view_forward_message(msg_id)` 查看完整内容，再
   决定是否调用 deferred Tool `request_cross_group_forward`；没有合适行动时
   可以保持沉默。
3. `maisaka.planner.after_response` 的 EARLY 阶段对所有转发调用执行无 I/O
   清洗，只保留公开参数，并签发绑定真实 Planner session 与 `msg_id` 的一次性
   内部凭据。
4. 若 Planner 偶然漏看并单独调用转发 Tool，路径 B fallback 的 LATE 阶段先按
   真实 session 完成无副作用预检，再把该批次改写为唯一的真实
   `view_forward_message`。插件在后续内部轮精确捕获对应 ToolResult，成功或
   达到既有 fallback 边界后恢复原请求；此时尚未发生物理发送。
5. 正式 Tool 处理器必须消费一次性凭据，并按凭据绑定的 session 解析可信
   source 群号，再校验 QQ 群聊、source 白名单、消息归属、消息类型和永久防重，
   从源消息读取原始转发节点。
6. 插件按照 `target_groups` 的配置顺序逐个发送，不并行投递。
7. 每个 target 发送成功后，插件按配置决定是否强制触发该目标群 Planner；
   Planner 只有在可靠定位真实目标消息且当前上下文足够时才选择评论，否则
   保持沉默；当前不保证它已经取得 source 完整内容。
8. 插件等待所有 target 得到真实处理结果，再向 source Planner 返回成功、
   部分失败或全部失败，任务入队本身不算转发成功。

一个任务只对应一个 source 聊天流和一条 source 消息，但可以有多个 target。
Source 表示发起分享的群，不要求合并转发中的内容最初产生于该群。

### 2.2 Source Planner 强制触发

`behavior.trigger_source_planner` 是独立且默认关闭的 source 方向开关。启用
后，插件通过 `chat.receive.after_process` Hook 在普通回复频率判断前识别 QQ
群聊中的合并转发；仅当群号位于当前 source 白名单时，才异步等待同一
`source stream + msg_id` 可由消息能力查询，然后调用
`maisaka.proactive.trigger` 强制触发该聊天流的 Planner。

这项能力只保证主动任务成功入队，不会自动调用 `view_forward_message`，也不
代表一定转发或回复。Planner 仍结合本群上下文和人设自主决定后续行为。Hook
采用观察模式，不改写消息，也不等待落库或入队，避免阻塞入站主链。

插件使用两层回声保护：优先比较入站发送者与适配器提供的 `self_id`、平台
账号标记；标记缺失时，再比较既有转发状态中保存的 target 最终消息 ID。
成功入队的消息按 `source stream + msg_id` 生成匿名稳定键，永久保存在插件
数据目录的 `source_planner_trigger_state.json`；重复 Hook、热重载和进程重启
都不会再次触发同一 source 消息。

### 2.3 两条查看路径与降级

#### 路径 A：Planner 已经查看

Planner 先调用 `view_forward_message`，再请求转发。插件通过 Planner Hook
读取当前上下文中的工具调用和结果；只要完整结果仍在当前上下文中，查看资格
就持续有效，不使用 TTL。结果被上下文裁剪后，Planner 必须重新查看。路径 A
直接进入正式处理器，不重复展开，也不重复进行媒体分析。

#### 路径 B：Planner 漏看后的 fallback

Planner 可见的 Tool 描述仍要求先完成路径 A，不把路径 B 提供为可主动选择的
常规流程。只有模型偶然漏看后单独出现的 `request_cross_group_forward` 才会
进入自动编排。LATE
`after_response` Hook 先使用真实 session、消息归属、消息类型、当前 target
配置以及活动/完成状态做预检；通过后将原调用替换为一个真实
`view_forward_message`。`before_request` Hook 必须同时匹配系统生成的
`tool_call_id` 与原始 `msg_id`，才会承认该 ToolResult 属于当前 pending。

系统查看成功或已经达到下述 fallback 边界后，插件在下一 Planner 内部轮恢复
原始转发请求；发送只会在恢复调用进入正式处理器之后发生。可重试故障尚未达到
配置阈值时，系统会生成新的真实查看调用；首次空内容也会续轮再查看一次。参数
错误、消息不存在、确认不是合并转发、终止失败或无法安全分类的失败不会开放
fallback，恢复后的请求由正式处理器返回明确拒绝，不产生投递。

混合多工具批次不会被路径 B 删除、插入或重排；其中所有转发调用仍会经过
EARLY 清洗和凭据签发，但未查看的调用由正式处理器按既有规则拒绝。每个
session 最多保存一条尚未发送的路径 B pending，且该状态仅存在于当前插件
进程。精确查看结果缺失、配置更新或插件卸载都会清除 pending 和未消费凭据；
不会把旧分享意图持久化并在未来普通轮次复活。

只有以下情况允许使用 `content_summary`、消息预览或固定占位文本降级，且
降级只用于 source 侧的分享判断：

- 同一消息连续发生的可重试查看故障达到
  `view_failure_fallback_threshold`；
- 同一消息连续两次返回空内容。

参数错误、消息不存在、确认不是合并转发或无法安全分类的失败不会通过反复
调用获得降级资格。当前 Host 尚未向 Planner Hook 提供结构化 ToolResult
状态，插件暂时兼容已知失败文案；上游改进见 [TODO-001](TODO.md#todo-001由-host-向-planner-hook-暴露结构化-toolresult-状态)。

### 2.4 可信调用边界

模型生成的 Tool 参数不是调用上下文。插件使用两个分离的边界避免
`after_response` 编排中的 I/O 或超时把未清洗参数直接交给正式处理器：

1. EARLY blocking Hook 不执行 capability I/O。它遍历整批调用，删除
   `platform`、`group_id`、`stream_id`、`chat_id`、target 等所有未声明字段，
   只保留 `msg_id`、`sharing_reason` 和 `content_summary`；每个新响应轮先撤销
   同一 session 尚未消费的旧凭据，再签发不可预测的一次性内部凭据。
2. 凭据绑定 EARLY Hook 提供的真实 Planner session 与规范化 `msg_id`，不能
   由模型指定、跨 session 复用、改绑消息或重复消费。
3. 正式 Tool handler 忽略模型可写的上下文字段；只有成功消费凭据后，才使用
   凭据中的真实 session 通过 Host 的可信会话映射解析平台和 source 群号。
4. 凭据缺失、未知、错配或已消费时立即拒绝，不读取源消息、不创建投递任务。
5. LATE 编排 Hook 只验证 EARLY 凭据与当前 session、`msg_id` 的精确绑定，绝不
   签发或改绑；两个 Hook 之间若 session 被改写或配置更新清空授权，调用会失去
   凭据并由正式处理器拒绝。
6. LATE 可以执行消息预检和路径 B 改写。它若超时或失败，Host 仍只会把 EARLY
   已清洗并带有效凭据的原调用交给正式处理器；处理器因没有查看资格而拒绝并
   要求查看，不会安全降级成“直接发送”。

这个凭据只覆盖一次 Planner Tool 调用授权，不替代当前上下文查看资格，也不
替代永久防重。配置更新和插件卸载会清除全部未消费凭据。

### 2.5 顺序投递与恢复

每个 target 独立推进以下阶段：

```text
pending -> sent -> planner_queued
```

- 物理发送失败时，不触发该 target 的 Planner，但继续处理后续 target。
- 发送成功、Planner 入队失败时，保留 `sent`，下次只重试入队，不重复发送。
- 状态按 `source stream + msg_id + target` 持久化到
  `forward_state.json`，插件重载、上下文裁剪或路由变化不会清除已完成记录。
- 新增 target 后只处理新目标，不会向已经完成的旧目标重发。
- 旧状态中的 `context_appended` 仅作兼容读取，新任务不再写入该阶段。

投递任务会在 Tool RPC 内等待全部 target 的真实结果，并屏蔽调用方取消，避免
Planner 中断或 RPC 超时连带取消已经开始的发送。

### 2.6 目标群上下文与 Planner

发送时开启 Maisaka 历史同步。已启动的目标群运行时会直接取得真实发送消息；
冷启动运行时可从消息库恢复最近消息。插件不再使用
`maisaka.context.append` 重复写入原始节点或 source 群的完整展开文本，避免
同一内容在目标 prompt 中重复出现。

路径 A 和路径 B 在完成 source 侧资格检查后使用完全相同的投递服务，因此
target 侧没有路径差异。在当前检查的 Host 中，目标 Planner 的初始主动请求会
保留上下文窗口内的目标群近期聊天，但转发内容的表示取决于 runtime 状态：运行
中的 runtime 直接追加普通 `SessionBackedMessage`，其 LLM 转换当前通常只显示
消息前缀和目标 ID；冷启动 runtime 从消息库恢复为 `ComplexSessionMessage`，
最多显示前四个转发节点。真实目标消息自身在历史里携带 `msg_id`；即使
`send.forward` 没有把该 ID 返回给插件，Planner 在能唯一定位该消息时仍可自行
调用 `view_forward_message`，并在下一内部轮得到完整展开结果。

插件优先通过 `send.forward(return_details=True)` 取得平台最终目标消息 ID，
并将它与 `sent` 阶段一起保存，作为目标 Planner 回复这条真实消息的唯一锚点。
该详细结果目前依赖配套 Host 分支，尚不是官方 Host 的稳定通用契约；Host 未
返回最终 ID 时仍可完成转发，但 Planner 无法可靠定位消息时必须保持沉默。

无论路径 A 还是路径 B，source 侧完整查看结果当前只用于分享资格和 fallback
判断。插件不会通过 `maisaka.context.append` 把它重复注入 target，也尚不能把
它与目标真实消息建立结构化、幂等的关联；主动任务也没有强制目标 Planner 再次
查看。因此当前版本只能确认目标 Planner 具备查看能力，不能保证它已经取得完整
内容或能够准确评论。即使 Host 返回目标消息 ID，也只解决回复锚点，不等于解决
完整内容关联。上游边界见
[TODO-003](TODO.md#todo-003稳定取得目标消息-id-并关联完整内容)。

当前只保证“发送”和“主动任务入队”按 target 配置顺序发生。SDK 不能等待
目标 Planner 完整执行结束，因此不同目标群的 Planner 可能并发推理。

### 2.7 当前配置

```toml
[routing]
source_groups = ["123456789"]
target_groups = ["234567890", "345678901"]

[behavior]
view_failure_fallback_threshold = 2
trigger_source_planner = false
trigger_target_planner = true
```

- 群号使用字符串；`target_groups` 的顺序就是投递顺序。
- `trigger_source_planner` 只控制收到合并转发后是否强制触发 source Planner，
  默认关闭；不自动查看、转发或回复。
- `trigger_target_planner` 只控制发送成功后是否主动触发目标群 Planner。
- 配置支持热更新；非法 TOML 不会覆盖最近一次有效配置。
- 插件版本和配置版本独立维护。只有配置发生变化时才修改配置版本，并直接
  同步到承载该变更的项目版本号；后续没有配置变化的版本继续沿用该值。

## 3. 未来展望（尚未实现）

### 3.1 将 source 完整内容关联到目标真实消息

路径 A 和路径 B 都已经能在物理发送前取得 source 侧完整查看结果，或按既有
边界得到 fallback 内容。尚未解决的是把这份内容与每个 target 中真实发送的
合并转发建立结构化、幂等关联，并保证目标 Planner 在一次请求内同时看到真实
消息、可靠回复锚点和对应完整内容。

这项能力依赖上游同时提供两个稳定契约：

1. `send.forward(return_details=True)` 在平台成功回执后返回最终目标消息 ID，
   并进入官方 Host 版本；配套分支中的实现不能被当作通用能力。
2. Host 提供按目标 session 与最终 `message_id` 绑定扩展内容的安全接口，或在
   目标 Planner 请求构造阶段提供等价的结构化关联；关联必须携带 source 任务
   身份，不能依靠一段无来源的提示文本。

上游能力可用后，插件应复用 source 已有结果，不在 target 再次调用
`view_forward_message`，也不重复进行媒体分析。完整内容或 fallback 内容只能
关联一次，不得复制真实消息、原始转发节点或媒体二进制；同一任务的多个 target
分别绑定各自最终消息 ID。发送成功而关联或 Planner 触发失败时，只补做缺失
阶段，不能回滚或重复物理发送。

目标 Planner 只有在真实目标消息、最终 ID 和关联内容都能可靠对应时，才能被
提示基于完整内容评论。Host 没有返回最终 ID 时继续使用当前兼容路径：转发
保持成功，但无法可靠定位刚发送消息时必须沉默。即使已经取得 ID，只要完整
内容关联尚未完成，也不能宣称目标 Planner 能准确回复。

### 3.2 支持更多消息类型

未来支持转发合并转发以外的更多消息类型。具体类型、权限边界、上下文表示和
降级方式留到实施前再设计，当前不展开，也不改变现有合并转发校验。

### 3.3 可选的调试统计

未来可增加一个仅供开发调试的统计开关，用来观察当前插件版本对合并转发的
实际触发情况。该开关默认关闭；优先不写入普通用户生成的配置文件。若 SDK
暂不支持隐藏配置，则允许以显式但默认关闭的字段提供，不把“隐藏”作为功能
落地的前置条件。

统计口径应清晰且可复核：

- 分母：本插件版本内，source 白名单群观察到的唯一合并转发消息数；
- 分子：这些消息中，Planner 实际发起转发请求的唯一消息数；
- 展示：`发起转发数 / 观察总数（比例）`，例如 `13/25（52%）`；
- 去重：同一 `source stream + msg_id` 在同一版本只计一次，重试和重复 Tool
  调用不得抬高数量；
- 分版本：升级插件后新建版本桶，旧版本数据保留，不混算比例。

统计结果必须只保存在本机，不能进入 Git，也不能随 commit、tag、release 或
其他方式上传到 GitHub。统计逻辑和空的数据结构可以作为开源代码接受审查，
但真实计数属于本地调试数据，不是插件源码或发布产物的一部分。

建议写入 MaiBot 为插件分配的本地运行时数据目录，例如
`context.paths.data_dir / "debug_forward_stats.local.json"`。实现时必须确认实际
路径位于插件源码工作树之外，不得写入根目录、`docs/`、配置模板、测试资源或
其他受 Git 跟踪的开源路径。如果运行环境无法保证统计文件与源码仓库隔离，
应跳过统计并记录调试警告，不能退而在仓库内创建文件。

文件不要包含日志全文、消息正文、媒体数据、群号或其他不必要的敏感内容。
下面只展示格式，不代表应在仓库中创建示例文件：

```json
{
  "0.2.0": {
    "observed": 25,
    "requested": 13
  },
  "0.2.1": {
    "observed": 8,
    "requested": 3
  }
}
```

比例应在读取或展示时根据整数计数计算，不必重复持久化。统计文件损坏、不可写、
路径落入源码工作树或统计逻辑异常时，只记录调试警告并跳过统计，不得影响正常
转发，也不得改动任何应当公开和受版本控制的文件。

## 4. 安全与实现约束

- EARLY Hook 必须无 I/O 地清洗全部转发调用，并以 Host 提供的真实 Planner
  session 和 `msg_id` 每轮签发新的一次性凭据；LATE 只能验证，不能签发或改绑，
  编排失败不能撤销该安全边界。
- Tool 处理器必须消费一次性凭据，以其绑定的 session 解析可信 source 群号，
  再实时校验 QQ 平台、source 白名单、当前聊天流和消息归属；模型参数和
  deferred discovery 都不是授权边界。
- Target 只能来自配置，不能由 Planner 通过 Tool 参数指定。
- 实际发送始终使用原始消息节点；展开文本只用于 Planner 理解，不替代真实
  消息，也不保存媒体 Base64。
- 路径 B 的 pending 和未消费凭据不持久化；配置更新、卸载或精确结果丢失时
  必须安全清除，不能在未来无关轮次恢复旧请求。
- 混合多工具批次不得为路径 B 重排；没有查看资格的转发调用由正式处理器拒绝。
- 单个 target 失败不回滚已成功目标，也不默认阻止后续目标。
- 所有新阶段都要可持久化、可恢复并保持幂等，不能把排队等同于成功。
- 新增配置时单独评估配置版本；仅修改实现、文档或插件版本不修改配置版本。
- 需要 Host 或 SDK 新能力的部分先记录上游边界，不在未获许可时修改 MaiBot
  主程序。

## 5. 测试与人工验收

当前实现至少应持续覆盖：

- 路径 A 复用当前上下文的成功查看，不重复展开；
- 路径 B 在发送前生成真实查看调用，按精确调用 ID 捕获结果，在成功或既有
  fallback 边界后恢复原请求；
- 可重试故障和空内容按既有阈值续轮查看，不可降级失败由正式处理器拒绝；
- EARLY 清洗和逐轮换新、LATE 不改绑、一次性凭据消费、真实 session 到 source
  群的可信解析，以及 LATE Hook 超时后的安全拒绝；
- 混合多工具批次不重排，pending 在结果缺失、配置更新和卸载时释放；
- 白名单与消息归属、消息类型、多 target 顺序、永久防重、阶段恢复、真实发送
  结果、目标消息 ID 兼容和单 target 失败后继续。

未来功能落地时还需增加：

- source 完整内容按最终目标消息 ID 只关联一次，关联或主动触发重试不会重复
  物理发送；
- 无目标 ID 或关联失败时目标 Planner 保守沉默，不声称已经准确理解完整内容；
- 调试统计默认关闭、按唯一消息去重、按插件版本分桶，并且统计失败不影响
  转发；
- 非合并转发类型在正式支持前仍被拒绝。

人工联调至少使用一个 source 群和两个 target 群，分别验证路径 A 与路径 B，
确认系统查看发生在发送之前、target 按配置顺序收到消息、非白名单群无法触发
或接收，以及同一 source 消息不会重复发送。目标群只能在可靠取得真实目标 ID
并具有足够上下文时评论，否则应保持沉默；当前验收不宣称完整内容关联或准确
回复已经解决。
