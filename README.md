# 麦麦自动跨群转发插件

让 MaiBot 在 source 白名单群聊中看完一则合并转发消息后，自主判断是否值得分享。插件按照 target 白名单顺序发送消息，并在每个目标群中触发 Planner，自主决定是否补充一句看法。

当前版本为 `0.1.0`，仅面向 SnowLuma Adapter 下的 QQ 群聊开发验证。

## 安装要求

- MaiBot `1.0.11` 或更高兼容版本
- maibot-plugin-sdk `2.7.1` 或更高兼容版本
- SnowLuma Adapter `0.8.4` 或更高的 `0.x` 兼容版本

插件必须位于 MaiBot 的 `plugins/MaiBot_ForwardMessagesAuto_Plugin/` 目录。运行时配置由 MaiBot 根据 `plugin.py` 中的 `config_model` 生成到 `config.toml`，该文件不应提交。

## 初始配置

```toml
[plugin]
enabled = true
version = "0.1.0"
config_version = "0.1.0"

[routing]
source_groups = ["123456789"]
target_groups = ["234567890", "345678901"]

[behavior]
view_cache_ttl_seconds = 1800
dedupe_ttl_seconds = 604800
trigger_target_planner = true
```

source 和 target 均只填写 QQ 群号字符串。`target_groups` 的列表顺序就是每次任务的发送顺序。未列入白名单的群不能触发或接收自动转发。

详细实现约束见 [功能设计文档](docs/forward-message-design.md)。

## 当前状态

插件骨架和配置已经建立，核心转发流程将在首版实现中补齐。
