"""麦麦自动跨群转发插件入口。"""

from __future__ import annotations

from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from forward_messages_auto import (
    CONFIG_VERSION,
    PLUGIN_VERSION,
    ForwardMessagesAutoConfig,
    ForwardingRuntime,
)
from forward_messages_auto.runtime import FORWARD_TOOL_NAME


class ForwardMessagesAutoPlugin(MaiBotPlugin):
    """声明 SDK 组件并装配跨群转发运行时。"""

    config_model = ForwardMessagesAutoConfig

    def __init__(self) -> None:
        super().__init__()
        self._runtime: ForwardingRuntime | None = None

    @property
    def runtime(self) -> ForwardingRuntime:
        """返回已经由生命周期初始化的运行时。"""

        if self._runtime is None:
            raise RuntimeError("插件运行时尚未初始化")
        return self._runtime

    def get_components(self) -> list[dict[str, Any]]:
        """将转发工具明确限制为群聊组件。"""

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
        """创建并启动插件运行时。"""

        self._runtime = ForwardingRuntime(self.ctx, lambda: self.config)
        await self.runtime.start()
        self._warn_for_version_mismatch()
        self.ctx.logger.info(
            "麦麦自动跨群转发插件 v%s 已加载，enabled=%s source=%d target=%d",
            PLUGIN_VERSION,
            self.config.plugin.enabled,
            len(self.runtime.source_groups()),
            len(self.runtime.target_groups()),
        )

    async def on_unload(self) -> None:
        """停止后台任务并保存运行时状态。"""

        if self._runtime is not None:
            await self._runtime.stop()
        self.ctx.logger.info("麦麦自动跨群转发插件已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, Any],
        version: str,
    ) -> None:
        """让运行时应用热更新后的配置。"""

        del config_data
        if self._runtime is not None:
            await self._runtime.reconfigure()
        self._warn_for_version_mismatch()
        self.ctx.logger.info("自动跨群转发配置已更新: scope=%s version=%s", scope, version)

    def _warn_for_version_mismatch(self) -> None:
        """记录用户配置中不一致的只读版本字段。"""

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
        description="缓存源群已经展开的合并转发内容，并仅在 source 白名单会话暴露转发工具。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def capture_view_forward_result(self, **kwargs: Any) -> dict[str, Any]:
        """缓存查看结果，并从非 source 会话移除转发工具。"""

        session_id = str(kwargs.get("session_id") or "").strip()
        is_source_session = self.runtime.is_source_session(session_id)
        if is_source_session:
            self.runtime.capture_view_results(session_id, kwargs.get("messages"))

        tool_definitions = kwargs.get("tool_definitions")
        if isinstance(tool_definitions, list) and not is_source_session:
            kwargs["tool_definitions"] = self.runtime.hide_forward_tool(tool_definitions)
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        FORWARD_TOOL_NAME,
        brief_description="把已经完整查看且值得分享的合并转发消息交给插件，按目标群白名单顺序转发。",
        detailed_description=(
            "仅在你已经成功调用 view_forward_message 查看 msg_id 的全部内容，并自主判断值得分享时调用。"
            "目标群由插件白名单决定，禁止自行指定目标群。content_summary 用于完整内容缓存失效时降级。"
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
                description="对完整转发内容的忠实摘要，仅在插件缓存失效时使用",
                required=False,
                default="",
            ),
        ],
        visibility="visible",
        chat_scope="group",
    )
    async def request_cross_group_forward(
        self,
        msg_id: str,
        sharing_reason: str = "",
        content_summary: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """委托运行时校验消息并创建后台转发任务。"""

        return await self.runtime.request_forward(
            msg_id,
            sharing_reason,
            content_summary,
            kwargs,
        )


def create_plugin() -> ForwardMessagesAutoPlugin:
    """创建插件实例。"""

    return ForwardMessagesAutoPlugin()
