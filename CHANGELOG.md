# Changelog

本项目的重要变更都会记录在此文件中。

本文档格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.18] - 2026-07-29

### Changed

- 请求 `send.forward` 返回平台最终目标消息 ID，将其随 target 已发送阶段
  持久化，并作为目标 Planner 唯一的回复锚点；旧 Host 布尔结果继续兼容。
- 本版本的完整目标消息 ID 能力基于配套 MaiBot Host 分支
  `1.1.2-send-forward-result` 开发和验证。该分支从
  `upstream/dev@078ee34d` 创建，并包含 Host commit `dfaf8e8a`；
  在该 Host 修改合并进入上游前，本版本具有明确的分支依赖性质。
- 未包含上述 Host 修改的 MaiBot 仍可执行实际转发，但 `send.forward` 只会
  返回旧布尔结果。此时插件不会伪造目标消息 ID，目标 Planner 只能使用保守
  定位和无法可靠定位时保持沉默的兼容路径。
- maibot-plugin-sdk 已具备 `**kwargs` 参数透传和详细字典保留能力，本版本不
  依赖 SDK 分支或 SDK 修改。

## [0.1.17] - 2026-07-28

### Changed

- 按配置、解析、查看上下文、运行时和转发投递职责拆分插件测试，并将共享
  测试替身集中到支持模块。
- 精简仅覆盖通用群号清洗和已淘汰 TTL 字段移除的低价值配置测试，保留配置
  恢复与重置的关键回归边界。

## [0.1.16] - 2026-07-26

### Changed

- 目标投递流程改为 `send → proactive.trigger`，不再向目标 Maisaka 上下文
  重复注入源群已经展开的完整内容和原始转发段。
- 目标 Planner 无法可靠定位刚发送的真实合并转发消息时必须保持沉默；待
  Host / SDK 返回目标消息 ID 后，再恢复带前四条预览的轻量提示。
- 旧版 `context_appended` 持久化阶段继续按已发送状态兼容。

## [0.1.15] - 2026-07-25

### Added

- 新增发送失败结构化原因透传 TODO，记录 Host、SDK 与插件各层的错误保真
  目标及向后兼容方案。

### Changed

- 跨群转发 Tool 改为等待全部 target 的真实顺序处理结果，并返回实际发送、
  完整处理和失败目标统计；单个 target 失败仍不阻止后续 target。

### Fixed

- 修复任务仅进入 `asyncio` 事件循环就向源群 Planner 报告成功、后台发送
  失败却无法反映在本次 ToolResult 中的问题。

## [0.1.14] - 2026-07-24

### Added

- 新增目标消息 ID 与冷启动 Maisaka 历史同步 TODO，记录 Host / SDK 后续兼容增强方案。

### Changed

- 合并转发发送成功后请求 Host 将带目标群消息 ID 的真实消息同步至 Maisaka 历史，供目标 Planner 的 `reply` 工具定位。
- 目标 Planner 意图明确只可回复本群刚发送的合并转发消息，不得选择源群消息 ID 或插件合成上下文 ID。

### Fixed

- 修复主动任务 metadata 暴露 source 群 `msg_id`、导致目标 Planner 使用跨聊天流消息 ID 调用 `reply` 的问题。

## [0.1.13] - 2026-07-24

### Added

- 新增上下文裁剪边界 TODO，跟踪将尚未消费的成功查看结果保留至当前决策结束的上游方案。

### Changed

- 未查看、可重试故障未达阈值和首次空内容的拒绝结果，会明确要求 Planner 在收到 ToolResult 后的紧接续轮重新查看，不等待新的聊天消息或使用摘要绕过。
- 查看调用的新鲜度改为按聊天流保存当前上下文快照，不再使用跨所有聊天流共享的全局 LRU。

### Fixed

- 修复高活跃聊天流可能驱逐安静聊天流的查看调用记录、导致重复注入分享判断提醒的问题。
- 修复已配对的空字符串或纯空白查看结果被忽略、无法累计空内容降级次数的问题。

## [0.1.12] - 2026-07-24

### Added

- 加载状态时自动将 `v0.1.11` 按 target 路由拆分的旧任务合并为新的 source 消息级永久状态，并保留每个 target 的最高完成阶段。

### Changed

- 查看资格改为每轮根据当前 Planner 上下文全量同步；成功结果仍在上下文时始终有效，被裁剪后立即失效，不再按时间过期或降级。
- 防重键改为只由 source stream 与 `msg_id` 生成，各 target 阶段永久保存在同一任务下；新增 target 时不再重发已经完成的旧 target。
- 移除 `behavior.view_cache_ttl_seconds` 和 `behavior.dedupe_ttl_seconds` 配置，配置版本提升至 `0.1.4`。

### Fixed

- 修复 target 路由发生变化时因任务键变化而可能重复发送旧 target 的问题。
- 修复已离开 Planner 上下文的历史查看结果仍可通过进程缓存继续取得转发资格的问题。

## [0.1.11] - 2026-07-24

### Changed

- 将 `view_forward_message` 失败区分为可重试、空内容、可修正、终止和未知类型，仅连续可重试故障受配置阈值控制。
- 空内容改为独立的一次诊断重试策略；连续第二次仍为空时才允许摘要或预览降级。
- `behavior.view_failure_fallback_threshold` 语义收窄为“连续可重试故障次数”，配置版本提升至 `0.1.3`。

### Fixed

- 阻止无效参数、消息不存在、非合并转发和未知失败通过重复调用错误获得降级资格。
- 不同失败类型会中断此前的连续计数，避免不相关故障被拼接为降级条件。

## [0.1.10] - 2026-07-24

### Added

- 新增 `behavior.view_failure_fallback_threshold` 配置项，默认值为 `2`，最小值为 `1`。

### Changed

- 连续查看失败的降级门槛改为实时读取插件配置，不再由请求服务硬编码。
- 配置版本提升至 `0.1.2`。

## [0.1.9] - 2026-07-24

### Changed

- 优化自主转发 deferred Tool 的首次暴露描述，明确按 `msg_id` 分享有意思、符合人设且值得转发的内容。
- 成功查看合并转发后，在紧接着的 Planner 续轮末尾追加一次判断提醒；请求被新消息打断时保留提醒，直到 Planner 真正返回。
- 普通缓存缺失或首次查看失败时拒绝转发；仅在连续两次已知查看失败或缓存确认过期时允许摘要、预览降级。

### Fixed

- 按 `tool_call_id` 去重查看结果，避免 Planner 历史中的旧结果反复刷新缓存 TTL。
- 识别当前 Host 的已知 `view_forward_message` 失败文本，避免把错误信息作为完整内容写入目标群上下文。

## [0.1.8] - 2026-07-24

### Changed

- 将 `request_cross_group_forward` 从全量可见工具改为 deferred tool，由 Planner 通过 `tool_search` 按需发现。
- Planner Hook 改为仅按当前聊天流缓存 `view_forward_message` 结果，不再改写工具定义；实际调用权限继续由处理器实时校验。
- 移除启动阶段的全量群聊流预加载和 source stream 快照，目标聊天流改为实际投递时按群号惰性解析，消除 Host 聊天管理器尚未初始化导致的时序竞态。

## [0.1.7] - 2026-07-24

### Fixed

- 将 source 定义明确为“读取消息并发起分享的当前群聊”，不再要求合并转发消息元数据中的原始群号等于 source 群号。
- 继续通过 Tool 调用群白名单、查询 `stream_id` 和返回消息 `session_id` 三层校验阻止非 source 群调用及跨聊天流消息读取。

## [0.1.6] - 2026-07-23

### Fixed

- 兼容 SDK 对 `message.get_by_id` 成功响应的自动解包，避免把正常源消息误判为 `success=False` 并返回“未知错误”。
- 保留旧版成功包装兼容，并在 Host 明确拒绝查询时记录消息 ID、聊天流和响应字段，向 Planner 返回 Host 提供的具体失败原因。

## [0.1.5] - 2026-07-23

### Fixed

- 配置文件在编辑保存过程中短暂出现未闭合数组等 TOML 语法错误时，继续使用最近一次有效配置，避免默认关闭状态被 Host 误判为主动禁用并卸载插件。
- 保持合法配置的原生热更新和删除配置后的默认重置语义，无需为修改白名单手动重新启用插件。

## [0.1.4] - 2026-07-23

### Changed

- 移除对 SnowLuma Adapter 的 manifest 强依赖。插件仅使用 MaiBot 通用 capabilities，适配器未激活时不再阻止插件本身加载。

## [0.1.3] - 2026-07-23

### Fixed

- 兼容 SDK 对聊天流 capability 成功响应的自动解包，避免插件启用时误将正常群聊列表识别为格式错误。
- 修复按群号查询或创建目标聊天流时未识别 SDK 解包后的聊天流字典的问题。

## [0.1.2] - 2026-07-23

### Changed

- 统一采用“自主跨群转发”的插件名称和用户可见表述，强调麦麦理解内容并主动作出分享决定的主体性。

### Fixed

- 修复 MaiBot Runner 以合成包加载 `plugin.py` 时无法导入内部 `forward_messages_auto` 子包、导致插件启动失败的问题。

## [0.1.1] - 2026-07-23

### Changed

- 将配置、领域模型、消息解析、查看结果缓存、聊天流索引、状态持久化、请求校验和后台投递拆分为职责独立的模块。
- 将 `plugin.py` 收敛为插件入口、生命周期、组件声明和服务装配层。
- 使用类封装运行时状态与行为，明确各模块之间的单向依赖。
- 为每项测试补充测试场景和预期行为说明。
- 新增多文件解耦、面向对象封装和测试说明规范。

## [0.1.0] - 2026-07-23

### Added

- 建立插件开发、提交和版本维护规范。
- 定义 source 与 target 群聊白名单。
- 定义由源群 Planner 决策、插件顺序转发、目标群 Planner 自主评论的初始功能方案。
- 新增详细功能设计、缓存策略、失败处理和测试要求。
- 新增 MaiBot SDK `2.7.1` 插件骨架、Manifest、强类型配置和完整生命周期。
- 新增 `request_cross_group_forward` Tool，并限制为 SnowLuma Adapter 下的 QQ source 白名单群。
- 新增 Planner Hook，用于缓存 `view_forward_message` 展开结果并在非 source 会话隐藏转发 Tool。
- 新增 target 白名单顺序投递、Maisaka 上下文注入和目标群 Planner 主动触发。
- 新增分阶段持久化状态与幂等恢复，避免后续步骤失败时重复发送。
- 新增测试，覆盖缓存、白名单、消息节点、顺序投递、失败隔离和去重。

[Unreleased]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.18...HEAD
[0.1.18]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.17...v0.1.18
[0.1.17]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.16...v0.1.17
[0.1.16]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/releases/tag/v0.1.0
