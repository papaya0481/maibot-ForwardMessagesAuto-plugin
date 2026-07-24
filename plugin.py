"""麦麦自主跨群转发插件入口。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

if __package__:
    from .forward_messages_auto import (
        CONFIG_VERSION,
        PLUGIN_VERSION,
        ForwardMessagesAutoConfig,
        ForwardingRuntime,
    )
    from .forward_messages_auto.config_recovery import LastKnownGoodConfig
    from .forward_messages_auto.runtime import FORWARD_TOOL_NAME
else:
    from forward_messages_auto import (
        CONFIG_VERSION,
        PLUGIN_VERSION,
        ForwardMessagesAutoConfig,
        ForwardingRuntime,
    )
    from forward_messages_auto.config_recovery import LastKnownGoodConfig
    from forward_messages_auto.runtime import FORWARD_TOOL_NAME


class ForwardMessagesAutoPlugin(MaiBotPlugin):
    """声明 SDK 组件并装配跨群转发运行时。"""

    config_model = ForwardMessagesAutoConfig

    def __init__(self) -> None:
        """创建尚未启动运行时的插件实例。

        SDK 上下文和配置会在 Runner 加载阶段注入。构造方法仅初始化运行时
        引用；磁盘状态、聊天流和后台服务由 ``on_load`` 创建。
        """

        super().__init__()
        self._runtime: ForwardingRuntime | None = None
        self._config_recovery = LastKnownGoodConfig(Path(__file__).resolve().with_name("config.toml"))

    def normalize_plugin_config(
        self,
        config_data: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        """校验新配置，并在 TOML 临时不完整时保留最近有效配置。

        Runner 的文件监听可能在编辑器尚未写完数组时读取失败，并在调用本
        方法前重建出默认配置。此时方法仅在磁盘文件确实不可解析时恢复最近
        一次有效快照，避免默认关闭状态被误判为用户主动禁用。合法配置仍交
        给 SDK 合并和校验；配置文件被删除时也继续使用 SDK 默认配置。

        Args:
            config_data: Runner 完成版本预处理后的插件配置；解析失败时通常
                是配置模型生成的完整默认值。

        Returns:
            二元组包含可注入的完整配置字典，以及 SDK 是否需要把归一化结果
            视为发生变化。恢复旧快照时第二项固定为 ``False``。
        """

        recovered_config = self._config_recovery.recover_for_invalid_file(
            config_data,
            type(self).build_default_config(),
        )
        if recovered_config is not None:
            try:
                self.ctx.logger.warning("config.toml 当前不可解析，继续使用最近一次有效配置并等待下次保存")
            except (AttributeError, RuntimeError):
                pass
            return recovered_config, False

        normalized_config, changed = super().normalize_plugin_config(config_data)
        self._config_recovery.remember(normalized_config)
        return normalized_config, changed

    @property
    def runtime(self) -> ForwardingRuntime:
        """返回已经由插件加载生命周期初始化的运行时。

        Returns:
            当前插件实例共享的 ``ForwardingRuntime``。

        Raises:
            RuntimeError: 在 ``on_load`` 完成前或运行时尚未创建时访问。
        """

        if self._runtime is None:
            raise RuntimeError("插件运行时尚未初始化")
        return self._runtime

    def get_components(self) -> list[dict[str, Any]]:
        """读取 SDK 组件定义，并强制转发 Tool 只在群聊生效。

        方法保留 SDK 生成的全部组件，仅对 ``request_cross_group_forward``
        设置顶层 ``chat_scope="group"``，并移除 metadata 中可能重复的同名
        字段，以符合 Host 的组件元数据格式。

        Returns:
            可注册到 Host 的组件定义列表。
        """

        components = super().get_components()
        for component in components:
            if component.get("name") != FORWARD_TOOL_NAME:
                continue
            component["chat_scope"] = "group"
            metadata = component.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("chat_scope", None)
        return components

    async def on_load(self) -> None:
        """创建运行时并完成插件加载准备。

        方法装配使用动态配置提供器的 ``ForwardingRuntime``，依次加载状态、
        清理过期记录，随后检查版本字段并记录当前启用状态及白名单数量。
        """

        self._runtime = ForwardingRuntime(self.ctx, lambda: self.config)
        await self.runtime.start()
        self._warn_for_version_mismatch()
        self.ctx.logger.info(
            "麦麦自主跨群转发插件 v%s 已加载，enabled=%s source=%d target=%d",
            PLUGIN_VERSION,
            self.config.plugin.enabled,
            len(self.runtime.source_groups()),
            len(self.runtime.target_groups()),
        )

    async def on_unload(self) -> None:
        """停止后台任务、保存状态并完成插件卸载。

        即使运行时尚未创建也允许安全调用。已创建时会取消并等待所有转发
        任务，再持久化最后阶段，避免卸载后继续向目标群发送。
        """

        if self._runtime is not None:
            await self._runtime.stop()
        self.ctx.logger.info("麦麦自主跨群转发插件已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, Any],
        version: str,
    ) -> None:
        """让运行时应用 Runner 已写入的新配置。

        Args:
            scope: 配置更新范围，由 MaiBot Runner 提供并写入日志。
            config_data: 更新后的原始配置字典。本插件通过 ``self.config``
                读取强类型配置，因此不直接消费该参数。
            version: 本次热更新事件携带的配置版本标识，用于日志追踪。
        """

        del config_data
        if self._runtime is not None:
            await self._runtime.reconfigure()
        self._warn_for_version_mismatch()
        self.ctx.logger.info("自主跨群转发配置已更新: scope=%s version=%s", scope, version)

    def _warn_for_version_mismatch(self) -> None:
        """检查配置声明的版本字段并记录不一致警告。

        该方法不修改用户配置，也不阻止插件加载。插件版本和配置版本分别与
        源码常量比较，以提示旧配置或手工编辑造成的偏差。
        """

        if self.config.plugin.version != PLUGIN_VERSION:
            self.ctx.logger.warning(
                "配置中的 plugin.version=%s 与插件版本 %s 不一致",
                self.config.plugin.version,
                PLUGIN_VERSION,
            )
        if self.config.plugin.config_version != CONFIG_VERSION:
            self.ctx.logger.warning(
                "配置版本 %s 与插件要求的 %s 不一致",
                self.config.plugin.config_version,
                CONFIG_VERSION,
            )

    @HookHandler(
        "maisaka.planner.before_request",
        name="capture_view_forward_result",
        description="按当前聊天流缓存 Planner 已经展开的合并转发内容。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def capture_view_forward_result(self, **kwargs: Any) -> dict[str, Any]:
        """缓存查看结果，并在成功查看后的续轮末尾追加一次判断提醒。

        方法从 Planner 历史中提取 ``view_forward_message`` 结果，并以当前
        ``session_id`` 隔离保存。每个工具调用只处理一次；成功结果会触发
        一个位于请求末尾的 ``system-reminder``，要求 Planner 立即判断是否
        值得分享。它不修改工具定义；自主转发 Tool 仍由 MaiBot 的 deferred
        tool 机制负责发现。

        Args:
            **kwargs: ``maisaka.planner.before_request`` Hook 参数。使用
                ``session_id`` 隔离状态，并读取及按需追加 ``messages``。

        Returns:
            阻塞 Hook 的继续结果。没有待判断消息时参数保持原样；成功查看
            后会在 ``modified_kwargs.messages`` 末尾追加一次判断提醒。

        Raises:
            RuntimeError: Hook 在 ``on_load`` 初始化运行时前被调用。
        """

        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id:
            self.runtime.capture_view_results(session_id, kwargs.get("messages"))
            reminder = self.runtime.build_view_judgment_reminder(session_id)
            messages = kwargs.get("messages")
            if reminder and isinstance(messages, list):
                messages.append({"role": "user", "content": reminder})
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.after_response",
        name="acknowledge_view_forward_judgment",
        description="确认 Planner 已完成成功查看后的续轮判断。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def acknowledge_view_forward_judgment(self, **kwargs: Any) -> dict[str, Any]:
        """在 Planner 成功返回后清除对应聊天流的一次性判断提醒。

        被新消息中断的 Planner 请求不会触发本 Hook，因此待处理提醒会保留
        到重试请求；正常返回后立即清除，避免未来无关消息再次触发旧判断。

        Args:
            **kwargs: ``maisaka.planner.after_response`` Hook 参数。仅读取
                ``session_id``，其余响应和工具调用字段保持不变。

        Returns:
            保持 Planner 响应不变的阻塞 Hook 继续结果。

        Raises:
            RuntimeError: Hook 在 ``on_load`` 初始化运行时前被调用。
        """

        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id:
            self.runtime.acknowledge_view_judgment(session_id)
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        FORWARD_TOOL_NAME,
        brief_description=(
            "根据 msg_id，将已经完整查看且你觉得有意思、符合人设、值得转发的合并转发消息分享到其他群聊。"
        ),
        detailed_description=(
            "仅在你已经成功调用 view_forward_message 查看 msg_id 的全部内容，并自主判断值得分享时调用。"
            "目标群由插件白名单决定，禁止自行指定目标群。content_summary 只在完整内容确认过期、"
            "连续可重试故障达到配置阈值或连续两次返回空内容时降级使用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="刚刚通过 view_forward_message 完整查看过的源合并转发消息 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="sharing_reason",
                param_type=ToolParamType.STRING,
                description="认为这则内容值得分享的简短原因",
                required=True,
                default="",
            ),
            ToolParameterInfo(
                name="content_summary",
                param_type=ToolParamType.STRING,
                description=(
                    "对完整转发内容的忠实摘要，仅在缓存确认过期、连续可重试故障达到配置阈值"
                    "或连续两次返回空内容时降级使用"
                ),
                required=False,
                default="",
            ),
        ],
        visibility="deferred",
        chat_scope="group",
    )
    async def request_cross_group_forward(
        self,
        msg_id: str,
        sharing_reason: str = "",
        content_summary: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """请求把一则已完整查看的 source 合并转发分享至白名单群。

        该 Tool 不接收 target 参数；目标集合和顺序只能来自插件配置。方法
        将 SDK 调用上下文和 Planner 参数交给运行时，验证成功后快速返回，
        实际发送在后台进行。

        Args:
            msg_id: 刚通过 ``view_forward_message`` 查看过的源消息 ID。
            sharing_reason: Planner 判断内容值得分享的简短理由。
            content_summary: 查看缓存确认过期、连续可重试故障达到配置阈值
                或连续两次返回空内容时使用的忠实摘要。
            **kwargs: SDK 注入的 Tool 上下文。必须能解析 QQ ``platform``、
                ``group_id`` 和 ``stream_id`` 或 ``chat_id``。

        Returns:
            Planner 可读的任务结果。新任务包含 ``accepted=True``、任务 ID
            和目标数量；重复任务包含 ``accepted=False``；拒绝时包含
            ``success=False`` 和中文原因。

        Raises:
            RuntimeError: Tool 在 ``on_load`` 初始化运行时前被调用。
        """

        return await self.runtime.request_forward(
            msg_id,
            sharing_reason,
            content_summary,
            kwargs,
        )


def create_plugin() -> ForwardMessagesAutoPlugin:
    """创建供 MaiBot Runner 加载的插件实例。

    Returns:
        尚未注入上下文或启动运行时的 ``ForwardMessagesAutoPlugin``。Runner
        随后负责设置配置与上下文并调用 ``on_load``。
    """

    return ForwardMessagesAutoPlugin()
