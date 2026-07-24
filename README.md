# 麦麦自主跨群转发插件

让 MaiBot 在 source 白名单群聊中看完一则合并转发消息后，自主判断是否值得分享。插件按照 target 白名单顺序发送消息，并在每个目标群中触发 Planner，自主决定是否补充一句看法。

当前版本为 `0.1.11`，基于 MaiBot `1.1.0` 开发，仅面向 SnowLuma Adapter 下的 QQ 群聊进行验证。

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
view_cache_ttl_seconds = 1800
view_failure_fallback_threshold = 2
dedupe_ttl_seconds = 604800
trigger_target_planner = true
```
source 和 target 均只填写 QQ 群号字符串。`target_groups` 的列表顺序就是每次任务的发送顺序。未列入白名单的群不能触发或接收自主转发。
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

## 工作流程

1. source 群 Planner 调用内置 `view_forward_message` 查看当前聊天流中的一则合并转发；其内部节点可以最初来自其他群。
2. Planner 认为内容值得分享时，通过 `tool_search` 发现并调用 deferred Tool `request_cross_group_forward`。
3. 插件按 `msg_id + source stream` 读取原始合并转发节点，并按 target 白名单顺序创建后台投递。
4. 每个 target 发送成功后，插件显式写入目标群 Maisaka 上下文。
5. 启用 `trigger_target_planner` 时，插件强制触发目标群 Planner；Planner 自主决定回复一句看法或保持沉默。

转发 Tool 默认位于 MaiBot 的 deferred tools 池中。各群 Planner 会看到其简要说明，但只有通过 `tool_search` 发现后才能取得完整参数并调用。处理器会实时校验 QQ 平台、当前调用群的 source 白名单权限，以及消息是否属于当前 source stream；因此 deferred discovery 不是授权边界，非 source 群即使尝试调用也会被拒绝。Source 表示读取并发起分享的群聊，不要求合并转发内容最初由该群产生。Planner 不能通过 Tool 参数指定目标群。

## 缓存与失败恢复

插件通过 `maisaka.planner.before_request` Hook 按当前聊天流缓存
`view_forward_message` 的完整结果，并按 `tool_call_id` 保证同一次查看不会
反复刷新缓存 TTL。成功查看后，插件会在紧接着的 Planner 续轮末尾追加
一次判断提醒；若请求被新消息打断，提醒会保留到 Planner 真正返回为止。

普通缓存缺失或可重试故障尚未达到配置阈值时，转发 Tool 会拒绝创建任务并
要求 Planner 重新查看。只有连续可重试故障达到
`view_failure_fallback_threshold`、连续两次返回空内容，或曾经成功缓存但
已经确认过期时，才会依次使用 Planner 提供的 `content_summary` 和原消息
预览降级。参数或消息错误必须修正；确认不是合并转发以及无法安全分类的失败
不会因重复调用开放降级。由于当前 Host 未向 Planner Hook 暴露 ToolResult
的成功状态，插件暂时依据已知稳定失败文案进行分类；未来主程序改进方向见
[TODO](docs/TODO.md)。

每个目标群分别记录以下阶段：

```text
sent → context_appended → planner_queued
```

状态保存在 MaiBot 为插件分配的数据目录中。若发送已经成功，但上下文写入或 Planner 入队失败，再次发起同一请求时会从未完成阶段继续，不会重复发送已经成功的合并转发。

单个 target 失败不会阻止后续 target。当前版本只保证发送和主动任务入队按白名单顺序发生；不同目标群的 Planner 可能在入队后并发推理。

## 测试

在本仓库目录执行：

```bash
PYTEST_ADDOPTS="-p no:cacheprovider" ../../.venv/bin/python -m pytest -q
RUFF_CACHE_DIR=/tmp/maibot-forward-ruff ../../.venv/bin/python -m ruff check plugin.py forward_messages_auto tests
RUFF_CACHE_DIR=/tmp/maibot-forward-ruff ../../.venv/bin/python -m ruff format --check plugin.py forward_messages_auto tests
```

人工联调应至少准备一个 source 群和两个 target 群，并验证：

- source Planner 先查看完整内容，再决定是否调用转发 Tool；
- target 按配置顺序收到合并转发；
- 目标群 Planner 可以选择评论或沉默；
- 非白名单群无法调用或接收；
- 同一条消息不会重复发送；
- 一个 target 失败后，后续 target 仍会继续。

详细实现约束和已知限制见 [功能设计文档](docs/forward-message-design.md)。
