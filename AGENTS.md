# 插件开发注意事项

本节以官方 [Vibe Coding 插件开发指南](https://docs.mai-mai.org/plugin/vibe-coding) 为基础约束。Manifest、生命周期、配置管理、各类组件和 SDK API 的详细说明，应继续查阅 [MaiBot 插件开发文档](https://docs.mai-mai.org/plugin/) 下对应的小节；实现细节与本文件冲突时，以当前官方文档为准。

## 开发边界与仓库结构

### 改动范围

- 本仓库是独立的 MaiBot 第三方插件，改动应限制在本插件目录内，不得修改 MaiBot 的 `src/`、`dashboard/`、`config/` 或其他主程序文件。
- 如果需求确实依赖新的主程序能力，应先停止修改，说明原因、影响范围和可替代方案，取得明确许可后再继续。
- 保持每次改动边界清晰，不重构无关代码，不对整个仓库进行格式化或导入整理。

### 必要文件

- 插件仓库至少包含 `_manifest.json`、`plugin.py`、`README.md` 和 `.gitignore`；按需增加 `tests/`、`docs/`、`assets/` 等目录。
- 插件入口固定为 `plugin.py`，插件工厂固定为 `create_plugin()`。代码可以拆分为插件内的本地模块，但不得依赖 MaiBot 主程序的内部实现。
- 插件专属忽略项只写入本仓库的 `.gitignore`，其中必须包含 `/config.toml`；不得为本插件修改 MaiBot 根目录的 `.gitignore`。

## Manifest 与配置

### Manifest 声明

- `_manifest.json` 使用 manifest v2。`id` 使用稳定的小写反向域名或作者前缀，`version` 使用严格的三段式语义版本。
- 正确声明 `host_application` 和 `sdk` 的最低、最高兼容版本；作者、仓库等 URL 必须是完整的 `http://` 或 `https://` 地址。
- Python 包和插件间依赖统一写入 `dependencies`。`capabilities` 只声明代码实际使用的 Host 能力，不申请无关权限。
- 默认语言推荐设为 `zh-CN`。用户可见文本、配置说明、日志和 README 优先使用简体中文。

### 配置模型

- 使用继承 `PluginConfigBase` 的配置模型，并通过 `Field` 声明默认值、类型和说明；插件类通过 `config_model` 指向完整配置。
- 推荐保留 `[plugin]` 分组，并包含 `enabled` 与 `config_version`。需要 WebUI 展示信息时，可使用配置模型支持的 UI 元数据。
- 优先通过 `self.config.<section>.<field>` 读取强类型配置；只有确实需要原始数据时才使用 `self.get_plugin_config_data()`。
- `config_model` 是配置结构和默认值的源码定义，运行时 `config.toml` 由 Runner 生成和补全，不得提交到仓库。配置热重载逻辑放在 `on_config_update()` 中。

## 插件代码与生命周期

### SDK 与入口

- 使用 `maibot-plugin-sdk` 的现行公开接口，代码中从 `maibot_sdk` 导入；不得直接导入 MaiBot 主程序内部模块。
- 插件类必须继承 `MaiBotPlugin`、声明 `config_model`，并实现 `on_load()`、`on_unload()`、`on_config_update()`；`create_plugin()` 必须返回该插件实例，否则 Runner 会拒绝加载。
- 插件通过注入的 `self.ctx` 使用日志、配置、消息发送、数据库、LLM 等 Host 能力，不自行绕过 SDK 与主进程通信。

### 生命周期与资源管理

- `on_load()` 只启动插件运行所需的资源；初始化失败应记录可定位的错误，不得留下半初始化的后台任务或连接。
- `on_config_update()` 应确认更新范围，应用新的插件配置，并在需要时安全重启相关服务，避免重复创建任务或泄漏旧资源。
- 所有后台任务、连接、网络客户端和文件句柄都必须可追踪，并在 `on_unload()` 中可靠停止、取消或关闭。卸载后不得继续执行插件任务。
- 持久化数据与临时数据应使用 SDK 提供的插件专属路径；不要硬编码个人绝对路径，也不要把运行时数据库、缓存或下载文件写进源码目录。

## 组件与能力调用

### 组件选择

- 让 LLM 主动调用能力时使用 `@Tool`；用户命令使用 `@Command`；流程拦截或观察使用 `@HookHandler`；消息及工作流事件监听使用 `@EventHandler`。
- 跨插件调用只通过稳定的 `@API` 暴露；外部聊天平台接入使用 `@MessageGateway`；WebUI 首页展示使用 `@HomeCard`；扩展模型服务时使用 `@LLMProvider`。
- `@Action` 仅用于维护旧插件兼容代码，新插件和新功能应直接使用 `@Tool`。
- Tool 默认进入 deferred 池。只有高频、低风险且确有必要的工具才考虑核心工具设置，避免增加模型选择负担。

### 参数、返回值与消息发送

- 组件说明必须写清使用时机、参数含义、限制条件和副作用。Tool 参数优先使用 `ToolParameterInfo` 与 `ToolParamType` 声明。
- Tool 返回值优先使用字典，把供 LLM 阅读的文本放入 `content`；图片等媒体按官方组件文档使用结构化内容，不把大段 base64 混入文本。
- 已有 `stream_id` 时，通过 `self.ctx.send` 发送消息；图片、表情、转发和混合消息也优先使用 SDK 能力代理。不得自行推算会话 ID。
- 公开 API 只承诺稳定、必要的接口，并同步维护调用约定；不应把插件内部实现细节暴露为跨插件能力。

## 安全、文档与验证

### 错误与敏感数据

- 所有网络请求必须设置合理超时并处理预期失败，向调用方返回可理解的错误；不得用笼统 fallback 掩盖真实故障，也不得把难懂的异常堆栈直接作为用户回复。
- 日志和错误信息不得泄露密钥、Token、Cookie 等敏感值。仓库不得提交个人标识、私有 URL、绝对路径、日志、数据库、缓存、虚拟环境或临时实验文件。
- 处理用户提供的文件名或路径时，应使用受控映射，并确认解析后的路径仍位于插件专属目录内。

### 文档与测试

- README 应同步说明功能、安装与启用方式、配置项、命令或 Tool 的使用方式、依赖与 capabilities、常见故障和测试方法。
- 完成实现后至少检查 `_manifest.json` 是合法 JSON、`plugin.py` 可被 Python 导入，并实际验证插件加载、配置更新和 WebUI 配置展示。
- 涉及 Command、Tool、消息发送或后台资源时，应补充对应功能测试；禁用或卸载插件后，还要确认任务、连接和缓存均已清理。
- 交付时说明本次改动、验证命令与验证结果；无法执行的检查必须明确列出原因。

# 其他事项

- 每完成一处边界清晰、可以独立说明的修改，都应尽量及时创建 commit；代码、文档、配置及其他类型的改动均适用。
- commit 应只包含本次相关改动，并使用简短、明确的说明概括修改内容。
- 使用 coding agent 完成修改时，提交 commit 必须按该 agent 的署名要求，在提交信息末尾附加对应的署名 trailer。

# 插件功能
