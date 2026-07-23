# Changelog

本项目的重要变更都会记录在此文件中。

本文档格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
- 新增自动化测试，覆盖缓存、白名单、消息节点、顺序投递、失败隔离和去重。

[Unreleased]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/papaya0481/MaiBot_ForwardMessagesAuto_Plugin/releases/tag/v0.1.0
