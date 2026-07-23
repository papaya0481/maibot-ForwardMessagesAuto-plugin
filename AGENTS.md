# 插件开发注意事项

官方规范：[Vibe Coding 插件开发指南](https://docs.mai-mai.org/plugin/vibe-coding)

- 本仓库是独立的 MaiBot 第三方插件；改动应限制在本插件目录内。不得修改 MaiBot 主程序或上层仓库文件，确有需要时先说明原因、影响范围与可替代方案，并取得许可。
- 使用 `maibot-plugin-sdk` 的现行接口。入口为 `plugin.py`，插件类继承 `MaiBotPlugin`、声明 `config_model`，并实现 `on_load()`、`on_unload()`、`on_config_update()` 与工厂函数 `create_plugin()`。
- 仓库至少维护 `_manifest.json`、`plugin.py`、`README.md` 和 `.gitignore`。`_manifest.json` 使用 manifest v2，插件 ID、三段式版本、Host/SDK 兼容范围、依赖及 capabilities 必须准确；只声明实际使用的依赖和能力。
- 配置以继承 `PluginConfigBase` 的模型和 `Field` 定义为唯一结构来源；保留包含 `enabled`、`config_version` 的 `[plugin]` 分组。运行时 `config.toml` 由 Runner 生成，不提交仓库，并在本目录 `.gitignore` 中以 `/config.toml` 忽略。
- 根据触发方式选择组件：模型主动调用用 `@Tool`，用户命令用 `@Command`，流程拦截或观察用 `@HookHandler`，事件监听用 `@EventHandler`，跨插件稳定接口用 `@API`，外部平台接入用 `@MessageGateway`。新功能不得使用仅供旧插件兼容的 `@Action`；工具默认不要设为核心工具。
- 组件描述、参数、返回值和副作用应明确。Tool 参数使用 SDK 类型声明，面向模型的结果优先放入返回字典的 `content`；消息发送使用上下文提供的 `stream_id` 和 `self.ctx.send`，不得自行推算会话 ID。
- 网络请求必须设置超时并转换为可理解的错误；日志、异常处理不得泄露密钥、Cookie 等敏感信息。后台任务、连接、客户端及文件句柄必须可追踪，并在配置更新或卸载时可靠停止和释放。
- 用户可见文本、配置说明、日志和文档优先使用简体中文。不要提交密钥、个人标识、绝对路径、日志、数据库、缓存、虚拟环境或临时实验文件。
- 保持改动聚焦，不做无关重构或全仓格式化。完成实现后至少校验 Manifest JSON、Python 导入/静态检查、插件加载与配置更新；涉及命令、Tool、消息发送或后台资源时，补充对应功能测试与卸载清理验证。README 应同步说明安装、启用、配置、使用方式、所需能力和常见故障。

# 其他事项

- 每完成一处边界清晰、可以独立说明的修改，都应尽量及时创建 commit；代码、文档、配置及其他类型的改动均适用。
- commit 应只包含本次相关改动，并使用简短、明确的说明概括修改内容。

# 插件功能
