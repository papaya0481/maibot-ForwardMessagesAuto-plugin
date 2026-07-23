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
        return self._config_provider()

    @property
    def background_tasks(self) -> set[Any]:
        return self.delivery.background_tasks

    def source_groups(self) -> list[str]:
        return GroupIdList.normalize(self.config.routing.source_groups)

    def target_groups(self) -> list[str]:
        return GroupIdList.normalize(self.config.routing.target_groups)

    async def start(self) -> None:
        self.delivery.resume()
        await self.state.load()
        await self.state.prune(self.config.behavior.dedupe_ttl_seconds)
        await self._refresh_streams()

    async def stop(self) -> None:
        await self.delivery.stop()
        await self.state.save()

    async def reconfigure(self) -> None:
        self.view_cache.cleanup(self.config.behavior.view_cache_ttl_seconds)
        await self.state.prune(self.config.behavior.dedupe_ttl_seconds)
        await self._refresh_streams()

    async def _refresh_streams(self) -> None:
        await self.streams.refresh(
            self.source_groups(),
            enabled=self.config.plugin.enabled,
        )

    def is_source_session(self, session_id: str) -> bool:
        return self.config.plugin.enabled and bool(session_id) and self.streams.is_source_stream(session_id)

    def capture_view_results(self, session_id: str, messages: Any) -> None:
        for message_id, content in PlannerHistoryParser.extract_view_results(messages):
            self.view_cache.put(session_id, message_id, content)
        self.view_cache.cleanup(self.config.behavior.view_cache_ttl_seconds)

    @staticmethod
    def hide_forward_tool(definitions: list[Any]) -> list[Any]:
        return ToolDefinition.excluding(definitions, FORWARD_TOOL_NAME)

    async def request_forward(
        self,
        msg_id: str,
        sharing_reason: str,
        content_summary: str,
        invocation_context: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.requests.create(
            msg_id,
            sharing_reason,
            content_summary,
            invocation_context,
        )
