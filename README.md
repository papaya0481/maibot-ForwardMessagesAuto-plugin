# 麦麦自主跨群转发插件

[![当前版本](https://img.shields.io/github/v/tag/papaya0481/maibot-ForwardMessagesAuto-plugin?sort=semver&label=%E5%BD%93%E5%89%8D%E7%89%88%E6%9C%AC)](https://github.com/papaya0481/maibot-ForwardMessagesAuto-plugin/tags)
[![许可证](https://img.shields.io/github/license/papaya0481/maibot-ForwardMessagesAuto-plugin?label=%E8%AE%B8%E5%8F%AF%E8%AF%81)](LICENSE)

让 MaiBot 像群聊成员一样，自主判断 source 白名单群中的合并转发是否值得分享，
再按配置顺序发送到 target 白名单群。插件不会让模型自行指定来源群或目标群。

> [!WARNING]
> 目前只在 QQ 群聊的 SnowLuma Adapter 环境下完成验证。Napcat 理论可行。

## 主要功能

- source 群 Planner 查看合并转发完整内容后，自主决定是否分享；若偶然漏看，
  插件会在实际发送前补做查看，不会直接绕过内容检查。
- 一个 source 消息可按配置顺序发送到多个 target 群；单个 target 失败不会阻止
  后续 target，已完成的投递不会因重试或路由调整而重复发送。
- 可独立配置是否在 source 群收到合并转发后强制触发 Planner，以及是否在每个
  target 群发送成功后触发该群 Planner。
- 配置支持热更新；编辑过程中短暂出现无效 TOML 时，插件会继续使用最近一次
  有效配置，不中断当前服务。

## 安装要求

- MaiBot `1.1.0` 或更高兼容版本
- maibot-plugin-sdk `2.7.1` 或更高兼容版本
- 已启用 QQ 群聊能力的适配器；目前仅验证 SnowLuma Adapter `0.8.4` 或更高的
  `0.x` 版本

SnowLuma Adapter 不是 Manifest 强依赖。插件只使用 MaiBot 通用 capabilities，
因此适配器暂未加载不会阻止插件本身加载，但实际转发仍要求运行环境提供相应能力。

将插件放入 MaiBot 的
`plugins/MaiBot_ForwardMessagesAuto_Plugin/` 目录。MaiBot 会根据 `plugin.py`
中的配置模型生成 `config.toml`；该运行时配置文件不应提交到仓库。

## 配置

```toml
[routing]
source_groups = ["123456789"]
target_groups = ["234567890", "345678901"]

[behavior]
view_failure_fallback_threshold = 2
trigger_source_planner = false
trigger_target_planner = true
```

- `source_groups`：允许触发自主分享的 QQ 群号列表，群号使用字符串。
- `target_groups`：允许接收分享的 QQ 群号列表；列表顺序就是每次任务的发送顺序。
- `view_failure_fallback_threshold`：完整内容查看连续发生可重试故障多少次后，
  才允许使用消息摘要或预览继续判断；最小值为 `1`，默认值为 `2`。参数错误、
  非合并转发和无法安全分类的失败不会触发降级。首次得到空内容时会再查看一次，
  连续第二次仍为空才允许降级。
- `trigger_source_planner`：默认关闭。启用后，source 白名单群收到真实合并转发时
  会强制触发一次本群 Planner，但不代表一定查看、转发或回复。
- `trigger_target_planner`：默认开启。每个 target 发送成功后，插件会为该群安排
  一次 Planner 主动任务。

保存合法 TOML 后，白名单和行为配置会立即刷新，无需关闭再开启插件。若文件
最终仍不合法，新值不会生效，请根据 MaiBot 日志修正语法。

## 运行方式

1. source 白名单群收到合并转发；启用 `trigger_source_planner` 时，插件会在
   MaiBot 可查询到该消息后触发本群 Planner。
2. source 群 Planner 查看完整内容并决定是否请求分享。合并转发内部节点最初
   来自哪个群不影响判断，真正的 source 始终是当前白名单群。
3. 插件校验 source 白名单、消息归属、消息类型和防重状态，再按
   `target_groups` 顺序逐个发送。
4. 每个 target 发送成功后，插件按配置决定是否触发目标群 Planner；所有 target
   都处理完毕后，source 群 Planner 才会收到本次任务的实际结果。

插件只保证“发送”和“主动任务入队”按 target 顺序发生。当前 SDK 无法等待一个
目标群 Planner 完整执行结束，因此不同目标群的 Planner 可能并发推理。

投递状态会按 source 消息和 target 群分别保存在 MaiBot 分配的插件数据目录中。
若发送成功但后续 Planner 入队失败，再次请求时会从未完成阶段继续；新增 target
群只会补发到新目标，不会重发已经完成的目标。

## 当前限制

- 目前只支持 QQ 合并转发消息，不支持普通文本、图片或内容平台分享卡片。
- source 侧看到的完整内容不会重复注入 target 群上下文。目标群 Planner 可以在
  能唯一定位真实目标消息时再次查看完整内容，但插件目前不强制这一步，也不能
  保证目标 Planner 在首次主动任务中已经取得完整内容。
- 目标群近期聊天会按 MaiBot 的正常上下文窗口参与判断；热运行和冷启动时，合并
  转发的初始展示详略可能不同。无法可靠确认目标消息或内容时，Planner 会保持
  沉默。
