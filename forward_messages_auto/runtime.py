"""跨群转发运行时装配。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forward_messages_auto.cache import ViewResultCache
from forward_messages_auto.config import ForwardMessagesAutoConfig, GroupIdList
from forward_messages_auto.delivery import ForwardDeliveryService
from forward_messages_auto.parsing import PlannerHistoryParser, ToolDefinition
from forward_messages_auto.request import ForwardRequestService
from forward_messages_auto.state import ForwardStateStore
from forward_messages_auto.streams import GroupStreamRegistry

FORWARD_TOOL_NAME = "request_cross_group_forward"


class ForwardingRuntime:
    """装配插件服务并协调生命周期和 Planner Hook。"""

    def __init__(
        self,
        context: Any,
        config_provider: Callable[[], ForwardMessagesAutoConfig],
    ) -> None:
        """创建一次插件生命周期内共享的服务对象。

        该构造方法只完成依赖装配，不执行磁盘或网络 I/O。状态加载和聊天流
        索引刷新由 ``start`` 负责。

        Args:
            context: MaiBot 注入的 ``PluginContext``，供各服务访问 capability、
                插件数据目录和日志。
            config_provider: 返回当前强类型插件配置的无参数回调。使用回调而非
                固定快照，使热更新后各服务可以立即读取新配置。
        """

        self._config_provider = config_provider
        self.view_cache = ViewResultCache()
        self.streams = GroupStreamRegistry(context)
        self.state = ForwardStateStore(
            context.paths.data_dir / "forward_state.json",
            context.logger,
        )
        self.delivery = ForwardDeliveryService(
            context,
            config_provider,
            self.streams,
            self.state,
        )
        self.requests = ForwardRequestService(
            context,
            config_provider,
            self.view_cache,
            self.state,
            self.delivery,
        )

    @property
    def config(self) -> ForwardMessagesAutoConfig:
        """读取当前生效的强类型插件配置。

        Returns:
            ``config_provider`` 当前返回的 ``ForwardMessagesAutoConfig``。
        """

        return self._config_provider()

    @property
    def background_tasks(self) -> set[Any]:
        """暴露尚未回收的后台投递任务集合。

        该属性主要用于生命周期清理和测试等待，不应由调用方直接增删。

        Returns:
            投递服务维护的 ``asyncio.Task`` 集合。
        """

        return self.delivery.background_tasks

    def source_groups(self) -> list[str]:
        """返回规范化后的 source QQ 群白名单。

        Returns:
            去空、去重并保持配置顺序的 source 群号字符串列表。
        """

        return GroupIdList.normalize(self.config.routing.source_groups)

    def target_groups(self) -> list[str]:
        """返回规范化后的 target QQ 群白名单。

        Returns:
            去空、去重并保持配置顺序的 target 群号字符串列表；该顺序也是
            后台投递顺序。
        """

        return GroupIdList.normalize(self.config.routing.target_groups)

    async def start(self) -> None:
        """启动运行时并准备处理 Planner 请求。

        启动顺序为恢复投递服务、加载持久化状态、清理过期去重记录、刷新
        QQ 群聊流索引。完成前不应暴露插件 Tool。
        """

        self.delivery.resume()
        await self.state.load()
        await self.state.prune(self.config.behavior.dedupe_ttl_seconds)
        await self._refresh_streams()

    async def stop(self) -> None:
        """停止后台投递并保存最终状态。

        方法先取消并等待所有后台任务，再持久化阶段快照，保证卸载后没有
        遗留任务继续调用 Host capability。
        """

        await self.delivery.stop()
        await self.state.save()

    async def reconfigure(self) -> None:
        """应用热更新后的缓存、去重与路由配置。

        方法使用最新配置清理查看缓存和任务状态，并重新读取 QQ 群聊流。
        正在运行的任务持有创建时的目标快照，不会被本次刷新中途改写。
        """

        self.view_cache.cleanup(self.config.behavior.view_cache_ttl_seconds)
        await self.state.prune(self.config.behavior.dedupe_ttl_seconds)
        await self._refresh_streams()

    async def _refresh_streams(self) -> None:
        """按当前启用状态和 source 白名单刷新聊天流索引。"""

        await self.streams.refresh(
            self.source_groups(),
            enabled=self.config.plugin.enabled,
        )

    def is_source_session(self, session_id: str) -> bool:
        """判断 Planner 会话是否可以看到自主转发 Tool。

        Args:
            session_id: Planner 请求所属的 MaiBot 聊天流 ID。

        Returns:
            仅当插件启用、ID 非空且聊天流属于 source 白名单群时返回
            ``True``。
        """

        return self.config.plugin.enabled and bool(session_id) and self.streams.is_source_stream(session_id)

    def capture_view_results(self, session_id: str, messages: Any) -> None:
        """从 Planner 历史提取并缓存合并转发完整内容。

        所有匹配结果均以当前 ``session_id`` 和各自 ``msg_id`` 为键写入，
        随后按最新配置清理过期条目。

        Args:
            session_id: source Planner 当前聊天流 ID。
            messages: Planner 请求携带的 OpenAI 兼容消息历史。
        """

        for message_id, content in PlannerHistoryParser.extract_view_results(messages):
            self.view_cache.put(session_id, message_id, content)
        self.view_cache.cleanup(self.config.behavior.view_cache_ttl_seconds)

    @staticmethod
    def hide_forward_tool(definitions: list[Any]) -> list[Any]:
        """从 Planner 工具定义中移除自主转发 Tool。

        Args:
            definitions: 当前 Planner 可见的工具定义列表。

        Returns:
            保持其他工具原始顺序的新列表，输入列表本身不被修改。
        """

        return ToolDefinition.excluding(definitions, FORWARD_TOOL_NAME)

    async def request_forward(
        self,
        msg_id: str,
        sharing_reason: str,
        content_summary: str,
        invocation_context: dict[str, Any],
    ) -> dict[str, Any]:
        """将 Tool 调用委托给请求服务完成校验和任务创建。

        Args:
            msg_id: source Planner 已完整查看的合并转发消息 ID。
            sharing_reason: Planner 给出的分享理由，可为空。
            content_summary: 查看缓存缺失时使用的忠实摘要，可为空。
            invocation_context: SDK 注入的调用上下文；请求服务读取
                ``platform``、``group_id``、``stream_id`` 或 ``chat_id``。

        Returns:
            可供 Planner 阅读的结果字典。成功接受时包含 ``success=True``、
            ``accepted=True``、``job_id`` 和 ``target_count``；重复任务会返回
            ``accepted=False``；校验失败返回 ``success=False`` 和中文
            ``content``。
        """

        return await self.requests.create(
            msg_id,
            sharing_reason,
            content_summary,
            invocation_context,
        )
