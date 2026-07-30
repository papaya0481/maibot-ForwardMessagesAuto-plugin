"""跨群转发运行时装配。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .config import ForwardMessagesAutoConfig, GroupIdList
from .delivery import ForwardDeliveryService
from .invocation_gate import FORWARD_TOOL_NAME as FORWARD_TOOL_NAME
from .invocation_gate import ForwardInvocationGate
from .parsing import PlannerHistoryParser
from .request import ForwardRequestService
from .state import ForwardStateStore
from .streams import GroupStreamRegistry
from .source_trigger import SourcePlannerTriggerService
from .view_context import ViewEligibilityStore
from .view_before_forward import ViewBeforeForwardCoordinator


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
        self.view_eligibility = ViewEligibilityStore()
        self._pending_view_judgments: dict[str, set[str]] = {}
        self.streams = GroupStreamRegistry(context)
        self.state = ForwardStateStore(
            context.paths.data_dir / "forward_state.json",
            context.logger,
        )
        self.source_trigger = SourcePlannerTriggerService(
            context,
            config_provider,
            self.state,
        )
        self.delivery = ForwardDeliveryService(
            context,
            config_provider,
            self.streams,
            self.state,
        )
        self.invocation_gate = ForwardInvocationGate()
        self.requests = ForwardRequestService(
            context,
            config_provider,
            self.view_eligibility,
            self.state,
            self.delivery,
            self.streams.resolve_group,
        )
        self.view_before_forward = ViewBeforeForwardCoordinator(
            config_provider,
            self.view_eligibility,
            self.invocation_gate,
            self.requests.can_auto_view,
            context.logger,
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

        return self.delivery.background_tasks | self.source_trigger.background_tasks

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

        启动顺序为恢复投递服务并加载永久防重状态。目标聊天流只在实际
        投递时按群号解析，避免插件加载与 Host 聊天管理器初始化之间产生
        时序依赖。
        """

        self.delivery.resume()
        await self.state.load()
        await self.source_trigger.start()

    async def stop(self) -> None:
        """停止后台投递并保存最终状态。

        方法先取消并等待所有后台任务，再持久化阶段快照，保证卸载后没有
        遗留任务继续调用 Host capability。
        """

        self.cancel_pending_direct_forwards()
        await self.source_trigger.stop()
        await self.delivery.stop()
        await self.state.save()

    def observe_source_message(self, message: Any) -> bool:
        """把一条入站 Hook 消息交给 source Planner 触发服务筛选。

        Args:
            message: ``chat.receive.after_process`` 提供的序列化消息载荷。

        Returns:
            消息满足当前配置并新建后台触发任务时返回 ``True``；否则返回
            ``False``。
        """

        return self.source_trigger.observe(message)

    def capture_view_results(self, session_id: str, messages: Any) -> list[str]:
        """按 Planner 当前历史同步查看资格并登记新的成功结果。

        每轮都用当前消息历史替换该聊天流的旧快照，因此被上下文裁剪的
        查看结果立即失效。仍可见的成功结果持续合法且不按时间过期；失败
        结果按可重试、空内容、可修正、终止和未知类型重新计算。每个成功
        ``tool_call_id`` 只会首次登记为等待 Planner 立即判断的消息。

        Args:
            session_id: source Planner 当前聊天流 ID。
            messages: Planner 请求携带的 OpenAI 兼容消息历史。

        Returns:
            本次首次捕获成功的 ``msg_id`` 列表。调用方可据此判断是否需要
            向紧接着的 Planner 请求追加一次性续轮提醒。
        """

        observations = PlannerHistoryParser.extract_view_observations(messages)
        fresh_message_ids = self.view_eligibility.sync_context(
            session_id,
            observations,
        )
        auto_view_message_ids = self.view_before_forward.capture_context(
            session_id,
            messages,
            observations,
        )
        manual_message_ids = [message_id for message_id in fresh_message_ids if message_id not in auto_view_message_ids]
        if manual_message_ids:
            self._pending_view_judgments.setdefault(session_id, set()).update(manual_message_ids)
        return fresh_message_ids

    async def transform_after_response(
        self,
        session_id: str,
        response: Any,
        tool_calls: Any,
    ) -> tuple[str, Any]:
        """编排直接请求的自动查看，并确认普通查看后的判断提醒。

        Args:
            session_id: 当前 source Planner 聊天流 ID。
            response: 模型原始文本响应。
            tool_calls: Host 序列化后的模型工具调用列表。

        Returns:
            协调器处理后的响应文本与工具调用。路径 A 和未接管响应保持原样；
            路径 B 会只返回一个系统查看或恢复后的转发调用。
        """

        transformed = await self.view_before_forward.transform_after_response(
            session_id,
            response,
            tool_calls,
        )
        self.acknowledge_view_judgment(session_id)
        return transformed

    def authorize_forward_calls(self, session_id: str, tool_calls: Any) -> Any:
        """清洗转发参数并绑定真实 ``after_response`` 会话。

        该步骤不执行 capability I/O，供 EARLY Hook 在自动查看编排前独立
        完成。即使后续 Hook 超时，正式 Tool handler 也只接受这里签发的
        一次性凭据；缺少凭据的原始模型调用会被拒绝。

        Args:
            session_id: Host Hook 提供的真实 Planner 会话 ID。
            tool_calls: Host 序列化后的模型工具调用列表。

        Returns:
            清除未声明上下文字段并加入内部一次性凭据的工具调用列表；
            非列表输入保持原值。
        """

        return self.invocation_gate.sanitize_and_authorize(session_id, tool_calls)

    def cancel_pending_direct_forwards(self) -> None:
        """取消尚未发送的路径 B 续接请求。

        配置热更新和插件卸载都会调用该方法，避免旧分享授权跨越配置边界后
        在未来普通 Planner 响应中复活。已经开始的真实投递不受影响。
        """

        self.view_before_forward.clear()
        self.invocation_gate.clear()

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
        """将 Tool 调用委托给请求服务完成校验和真实投递。

        Args:
            msg_id: source Planner 请求分享的合并转发消息 ID。
            sharing_reason: Planner 给出的分享理由，可为空。
            content_summary: 连续可重试故障达到配置阈值或连续两次返回
                空内容时使用的忠实摘要，可为空。
            invocation_context: Runner 传入的完整调用参数。运行时只消费
                EARLY Hook 签发的一次性凭据，不信任其中模型可写的
                ``platform``、``group_id``、``stream_id`` 或 ``chat_id``。

        Returns:
            可供 Planner 阅读的结果字典。完整投递成功时包含
            ``success=True``、``completed=True``、``status="succeeded"``、
            ``job_id`` 和目标统计；重复任务会返回 ``accepted=False``；
            校验或投递失败返回 ``success=False`` 和中文 ``content``。
        """

        source_stream_id = self.invocation_gate.consume(
            msg_id,
            invocation_context,
        )
        if not source_stream_id:
            return self.requests.failure(
                "转发请求缺少可信的 Planner 会话授权，已拒绝执行；请在当前 Planner 轮次重新发起请求。"
            )
        return await self.requests.create(
            msg_id,
            sharing_reason,
            content_summary,
            source_stream_id,
        )
