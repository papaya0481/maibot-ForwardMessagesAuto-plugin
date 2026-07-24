"""跨群转发运行时装配。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .cache import ViewResultCache
from .config import ForwardMessagesAutoConfig, GroupIdList
from .delivery import ForwardDeliveryService
from .parsing import PlannerHistoryParser
from .request import ForwardRequestService
from .state import ForwardStateStore
from .streams import GroupStreamRegistry

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
        self._pending_view_judgments: dict[str, set[str]] = {}
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

        启动顺序为恢复投递服务、加载持久化状态和清理过期去重记录。目标
        聊天流只在实际投递时按群号解析，避免插件加载与 Host 聊天管理器
        初始化之间产生时序依赖。
        """

        self.delivery.resume()
        await self.state.load()
        await self.state.prune(self.config.behavior.dedupe_ttl_seconds)

    async def stop(self) -> None:
        """停止后台投递并保存最终状态。

        方法先取消并等待所有后台任务，再持久化阶段快照，保证卸载后没有
        遗留任务继续调用 Host capability。
        """

        await self.delivery.stop()
        await self.state.save()

    async def reconfigure(self) -> None:
        """应用热更新后的缓存、去重与路由配置。

        方法使用最新配置清理查看缓存和任务状态。聊天流由投递服务按需解析；
        正在运行的任务持有创建时的目标快照，不会被本次更新中途改写。
        """

        self.view_cache.cleanup(self.config.behavior.view_cache_ttl_seconds)
        await self.state.prune(self.config.behavior.dedupe_ttl_seconds)

    def capture_view_results(self, session_id: str, messages: Any) -> list[str]:
        """从 Planner 历史登记新的查看结果并缓存成功内容。

        每个 ``tool_call_id`` 在同一聊天流中只处理一次，避免历史结果在
        后续 Planner 请求中反复刷新 TTL。已知失败结果仅累计次数，成功
        结果才写入缓存，并登记为等待 Planner 立即判断的消息。

        Args:
            session_id: source Planner 当前聊天流 ID。
            messages: Planner 请求携带的 OpenAI 兼容消息历史。

        Returns:
            本次首次捕获成功的 ``msg_id`` 列表。调用方可据此判断是否需要
            向紧接着的 Planner 请求追加一次性续轮提醒。
        """

        fresh_message_ids: list[str] = []
        for observation in PlannerHistoryParser.extract_view_observations(messages):
            is_fresh_success = self.view_cache.record_observation(
                session_id,
                observation.call_id,
                observation.message_id,
                observation.content,
                failed=observation.failed,
            )
            if is_fresh_success:
                fresh_message_ids.append(observation.message_id)
        if fresh_message_ids:
            self._pending_view_judgments.setdefault(session_id, set()).update(fresh_message_ids)
        self.view_cache.cleanup(self.config.behavior.view_cache_ttl_seconds)
        return fresh_message_ids

    def build_view_judgment_reminder(self, session_id: str) -> str:
        """构造成功查看后等待 Planner 消费的续轮判断提醒。

        提醒在对应 Planner 请求成功返回前保持待处理，因此请求若被新消息
        打断，下一次重试仍会收到相同提醒。方法只读取状态，不清除待处理项。

        Args:
            session_id: 当前 source Planner 聊天流 ID。

        Returns:
            有待判断消息时返回一个 ``system-reminder`` 文本，否则返回空串。
        """

        message_ids = sorted(self._pending_view_judgments.get(session_id, set()))
        if not message_ids:
            return ""
        joined_message_ids = "、".join(message_ids)
        return (
            "<system-reminder>\n"
            f"刚刚已经成功查看合并转发消息 msg_id={joined_message_ids} 的完整内容。"
            "请在本次续轮中优先依据刚看到的完整内容，判断它是否有意思、符合你的人设并值得分享到其他群聊；"
            "值得时通过 tool_search 发现并调用 request_cross_group_forward，不值得时不要转发。"
            "不要等待下一条聊天消息后再作判断。\n"
            "</system-reminder>"
        )

    def acknowledge_view_judgment(self, session_id: str) -> None:
        """确认 Planner 已完成一次带续轮提醒的模型响应。

        Args:
            session_id: 已成功返回 Planner 响应的聊天流 ID。该方法清除该流
                当前全部待判断消息，使后续普通轮次不再重复注入提醒。
        """

        self._pending_view_judgments.pop(session_id, None)

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
            content_summary: 缓存确认过期或多次查看失败时使用的忠实摘要，
                可为空。
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
