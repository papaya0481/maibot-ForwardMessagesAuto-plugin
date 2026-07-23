# Changelog

本项目的重要变更都会记录在此文件中。

本文档格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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

[Unreleased]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/releases/tag/v0.1.0
