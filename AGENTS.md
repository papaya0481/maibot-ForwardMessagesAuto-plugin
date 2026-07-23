# 插件开发指南

你正在为 MaiBot 编写第三方插件。插件必须放在 plugins/<plugin-name>/ 下，不要修改 MaiBot 主程序代码，除非我明确许可。请使用 maibot-plugin-sdk，入口文件为 plugin.py，元信息文件为 _manifest.json。必须实现 on_load、on_unload、on_config_update 和 create_plugin。优先使用 @Tool、@Command、@HookHandler、@EventHandler、@API、@MessageGateway；不要给新插件使用 @Action。所有用户可见文本优先使用简体中文。请保持改动边界清晰，并给出测试方式。

详细信息以官方 [Vibe Coding 插件开发指南](https://docs.mai-mai.org/plugin/vibe-coding) 为基础约束。Manifest、生命周期、配置管理、各类组件和 SDK API 的详细说明，应继续查阅 [MaiBot 插件开发文档](https://docs.mai-mai.org/plugin/) 下对应的小节；实现细节与本文件冲突时，以官方文档为准。

# 其他事项

- 每完成一处边界清晰、可以独立说明的修改，都应尽量及时创建 commit；代码、文档、配置及其他类型的改动均适用。
- commit 应只包含本次相关改动，并使用简短、明确的说明概括修改内容。
- 使用 coding agent 完成修改时，提交 commit 必须按该 agent 的署名要求，在提交信息末尾附加对应的署名 trailer。

# 插件功能
