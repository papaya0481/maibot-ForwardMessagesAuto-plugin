# Changelog

本项目的重要变更都会记录在此文件中。

本文档格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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

[Unreleased]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.7...HEAD
[0.1.7]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/releases/tag/v0.1.0
