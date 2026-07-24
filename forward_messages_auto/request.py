"""转发请求校验与任务构造。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .config import ForwardMessagesAutoConfig, GroupIdList
from .delivery import ForwardDeliveryService
from .models import (
    ForwardJob,
    TargetStage,
    ViewEligibilityStatus,
    ViewObservationKind,
)
from .parsing import ForwardMessageParser
from .state import ForwardStateStore
from .view_context import ViewEligibilityStore

EMPTY_CONTENT_FALLBACK_THRESHOLD = 2


class ForwardRequestService:
    """校验 Tool 调用、读取源消息并创建幂等任务。"""

    def __init__(
        self,
        context: Any,
        config_provider: Callable[[], ForwardMessagesAutoConfig],
        view_eligibility: ViewEligibilityStore,
        state: ForwardStateStore,
        delivery: ForwardDeliveryService,
    ) -> None:
        """创建负责 Tool 请求前置处理的服务。

        Args:
            context: MaiBot ``PluginContext``，用于读取源消息和记录日志。
            config_provider: 返回最新强类型配置的回调。
            view_eligibility: 保存源 Planner 当前上下文查看资格的共享索引。
            state: 提供幂等完成判断的持久化状态存储。
            delivery: 接收已验证 ``ForwardJob`` 的后台投递服务。
        """

        self._ctx = context
        self._config_provider = config_provider
        self._view_eligibility = view_eligibility
        self._state = state
        self._delivery = delivery

    @property
    def config(self) -> ForwardMessagesAutoConfig:
        """读取当前生效的插件配置。

        Returns:
            配置提供器当前返回的 ``ForwardMessagesAutoConfig``。
        """

        return self._config_provider()

    async def create(
        self,
        msg_id: str,
        sharing_reason: str,
        content_summary: str,
        invocation_context: dict[str, Any],
    ) -> dict[str, Any]:
        """校验转发请求、读取源消息并安排后台任务。

        处理顺序包括调用环境校验、source 消息归属校验、合并转发节点解析、
        展开内容选择和幂等判断。所有校验通过后仅安排后台任务，不等待各
        target 实际发送完成。

        Args:
            msg_id: source Planner 刚通过 ``view_forward_message`` 查看过的
                合并转发消息 ID。
            sharing_reason: Planner 对“为何值得分享”的简短说明，可为空。
            content_summary: 连续可重试失败达到阈值或连续两次返回空内容时
                使用的忠实摘要，可为空。
            invocation_context: SDK 注入的 Tool 调用上下文。必须提供 QQ
                ``platform``、``group_id``，以及 ``stream_id`` 或 ``chat_id``。

        Returns:
            Planner 可读的结果字典。接受新任务时包含 ``success=True``、
            ``accepted=True``、稳定 ``job_id`` 和 ``target_count``；任务正在
            处理或已经完成时返回 ``accepted=False``；校验或读取失败时返回
            ``success=False`` 及中文 ``content``。
        """

        validation = self._validate_invocation(msg_id, invocation_context)
        if isinstance(validation, dict):
            return validation
        source_stream_id, source_group_id, source_message_id, target_group_ids = validation

        message_result = await self._get_source_message(source_message_id, source_stream_id)
        failure = message_result.get("_failure")
        if isinstance(failure, str):
            return self.failure(failure)
        message = message_result

        message_error = self._validate_source_message(
            message,
            source_stream_id,
        )
        if message_error:
            return self.failure(message_error)

        forward_payload = ForwardMessageParser.extract(message)
        if forward_payload is None:
            return self.failure("指定消息不是可发送的合并转发消息。")
        forward_segment, forward_messages = forward_payload
        expanded_content = self._resolve_expanded_content(
            source_stream_id,
            source_message_id,
            content_summary,
            message,
        )
        if isinstance(expanded_content, dict):
            return expanded_content

        state_key = self._state.build_state_key(
            source_stream_id,
            source_message_id,
        )
        job_id = state_key.rsplit(":", 1)[-1]
        duplicate_result = self._duplicate_result(state_key, job_id, target_group_ids)
        if duplicate_result is not None:
            return duplicate_result

        job = ForwardJob(
            job_id=job_id,
            state_key=state_key,
            source_stream_id=source_stream_id,
            source_group_id=source_group_id,
            source_message_id=source_message_id,
            target_group_ids=target_group_ids,
            forward_segment=forward_segment,
            forward_messages=forward_messages,
            expanded_content=expanded_content,
            sharing_reason=str(sharing_reason or "").strip(),
        )
        self._delivery.schedule(job)
        return {
            "success": True,
            "content": (
                f"已接受跨群转发请求，将按白名单顺序处理 {len(target_group_ids)} 个目标群。任务 ID：{job_id}。"
            ),
            "job_id": job_id,
            "accepted": True,
            "target_count": len(target_group_ids),
        }

    def _validate_invocation(
        self,
        msg_id: str,
        invocation_context: dict[str, Any],
    ) -> tuple[str, str, str, list[str]] | dict[str, Any]:
        """验证 Tool 调用环境并计算本次目标群列表。

        该方法只接受启用状态下、来自 SnowLuma QQ source 白名单群的调用。
        target 完全由配置决定，并排除与 source 相同的群。

        Args:
            msg_id: Planner 传入的源消息 ID；去除空白后不能为空。
            invocation_context: SDK Tool 上下文，读取 ``platform``、
                ``group_id``、``stream_id`` 和兼容字段 ``chat_id``。

        Returns:
            校验成功时返回 ``(source_stream_id, source_group_id,
            source_message_id, target_group_ids)``；失败时返回
            ``{"success": False, "content": ...}``。
        """

        if not self.config.plugin.enabled:
            return self.failure("自主跨群转发插件当前未启用。")

        platform = str(invocation_context.get("platform") or "").strip().lower()
        source_group_id = str(invocation_context.get("group_id") or "").strip()
        source_stream_id = str(invocation_context.get("stream_id") or invocation_context.get("chat_id") or "").strip()
        source_message_id = str(msg_id or "").strip()
        if platform != "qq":
            return self.failure("初版仅支持 SnowLuma Adapter 下的 QQ 群聊。")
        if not source_stream_id or not source_group_id:
            return self.failure("缺少当前 QQ 群聊上下文，无法创建转发任务。")
        if source_group_id not in set(GroupIdList.normalize(self.config.routing.source_groups)):
            return self.failure("当前群不在 source 白名单中，不能发起跨群转发。")
        if not source_message_id:
            return self.failure("必须提供刚刚查看过的合并转发消息 msg_id。")

        target_group_ids = [
            group_id
            for group_id in GroupIdList.normalize(self.config.routing.target_groups)
            if group_id != source_group_id
        ]
        if not target_group_ids:
            return self.failure("没有可用的 target 白名单群，请先完成插件配置。")
        return source_stream_id, source_group_id, source_message_id, target_group_ids

    async def _get_source_message(
        self,
        source_message_id: str,
        source_stream_id: str,
    ) -> dict[str, Any]:
        """从 Host 获取包含媒体二进制数据的源消息。

        当前 SDK 会把成功响应自动解包为消息字典；方法同时兼容旧版 SDK
        返回的 ``{"success": True, "message": ...}`` 包装。capability 异常、
        明确失败、消息缺失和格式错误都会转换成带 ``_failure`` 的内部字典，
        供 ``create`` 生成稳定的 Planner 错误结果，不向 Tool 调用方暴露堆栈。

        Args:
            source_message_id: 要读取的 source 消息 ID。
            source_stream_id: 限制查询范围的 source 聊天流 ID。

        Returns:
            成功时返回 Host 消息字典；失败时返回
            ``{"_failure": "中文原因"}``。
        """

        try:
            result = await self._ctx.message.get_by_id(
                source_message_id,
                stream_id=source_stream_id,
                include_binary_data=True,
            )
        except Exception as exc:
            self._ctx.logger.warning(
                "读取源消息失败: msg_id=%s error=%s",
                source_message_id,
                exc,
            )
            return {"_failure": "读取源消息失败，暂时无法创建转发任务。"}
        if result is None:
            return {"_failure": "没有找到指定的源消息。"}
        if not isinstance(result, dict):
            self._ctx.logger.warning(
                "读取源消息返回格式错误: msg_id=%s stream_id=%s actual_type=%s",
                source_message_id,
                source_stream_id,
                type(result).__name__,
            )
            return {"_failure": "读取源消息失败：Host 返回格式错误。"}

        if "success" not in result:
            return result
        if not result.get("success", False):
            error = str(result.get("error") or "").strip() or "Host 未提供失败原因"
            self._ctx.logger.warning(
                "读取源消息被 Host 拒绝: msg_id=%s stream_id=%s error=%s response_keys=%s",
                source_message_id,
                source_stream_id,
                error,
                sorted(result),
            )
            return {"_failure": f"读取源消息失败：{error}"}

        message = result.get("message")
        if not isinstance(message, dict):
            return {"_failure": "没有找到指定的源消息。"}
        return message

    @staticmethod
    def _validate_source_message(
        message: dict[str, Any],
        source_stream_id: str,
    ) -> str:
        """确认查询结果属于当前 QQ source 聊天流。

        Source 表示读取消息并发起分享的群聊，不表示合并转发节点的原始
        来源群。消息 capability 已使用当前 ``stream_id`` 限定查询，本方法
        再校验返回消息的 ``session_id``，但不会比较消息元数据中的
        ``group_id``，因此允许 source 群分享从其他群收到的合并转发。

        Args:
            message: Host 返回的源消息字典。
            source_stream_id: Tool 调用所在的聊天流 ID。

        Returns:
            校验通过返回空字符串；聊天流或平台不匹配时返回可直接展示给
            Planner 的中文错误说明。
        """

        if str(message.get("session_id") or "").strip() != source_stream_id:
            return "指定消息不属于当前 source 聊天流。"
        if str(message.get("platform") or "").strip().lower() != "qq":
            return "指定消息不是 QQ 群聊消息。"
        return ""

    def _resolve_expanded_content(
        self,
        source_stream_id: str,
        source_message_id: str,
        content_summary: str,
        message: dict[str, Any],
    ) -> str | dict[str, Any]:
        """选择完整内容，并仅在明确不可推进时允许降级。

        当前上下文中的有效成功查看始终优先且不按时间过期。参数或消息错误
        要求修正，非合并转发和未知错误会阻止任务；可重试故障只有连续次数
        达到配置阈值才允许降级，空内容则允许一次诊断重试。查看结果已被
        上下文裁剪时必须重新查看，不能按过期使用摘要。

        Args:
            source_stream_id: 源消息所属聊天流 ID。
            source_message_id: 源合并转发消息 ID。
            content_summary: Planner 提供的可选降级摘要。
            message: Host 源消息，用于读取 ``processed_plain_text`` 预览。

        Returns:
            可用时返回非空目标群上下文文本；尚应继续尝试查看时返回
            ``{"success": False, "content": ...}``，阻止创建转发任务。
        """

        lookup = self._view_eligibility.lookup(
            source_stream_id,
            source_message_id,
        )
        if lookup.status is ViewEligibilityStatus.READY:
            return lookup.content

        observation_kind = lookup.last_observation_kind
        if observation_kind is ViewObservationKind.TERMINAL_FAILURE:
            return self.failure(
                "view_forward_message 已确认该消息不是可展开的合并转发消息；重复查看不会开放降级，请勿创建转发任务。"
            )
        if observation_kind is ViewObservationKind.CORRECTABLE_FAILURE:
            return self.failure(
                "view_forward_message 返回了可修正的参数或消息错误；"
                "请检查并修正 msg_id，使用相同错误参数重试不会开放降级。"
            )
        if observation_kind is ViewObservationKind.UNKNOWN_FAILURE:
            return self.failure(
                "view_forward_message 返回了无法安全分类的失败；为避免误转发，当前不会累计次数或开放降级。"
            )

        failure_threshold = self.config.behavior.view_failure_fallback_threshold
        fallback_reason = ""
        if (
            observation_kind is ViewObservationKind.RETRYABLE_FAILURE
            and lookup.retryable_failure_count >= failure_threshold
        ):
            fallback_reason = f"view_forward_message 已连续发生 {lookup.retryable_failure_count} 次可重试故障"
        elif (
            observation_kind is ViewObservationKind.EMPTY_CONTENT_FAILURE
            and lookup.empty_content_failure_count >= EMPTY_CONTENT_FALLBACK_THRESHOLD
        ):
            fallback_reason = f"view_forward_message 已连续 {lookup.empty_content_failure_count} 次返回空内容"

        if not fallback_reason:
            if observation_kind is ViewObservationKind.RETRYABLE_FAILURE:
                retry_detail = (
                    "当前连续记录到 "
                    f"{lookup.retryable_failure_count} 次 view_forward_message 可重试故障，"
                    f"尚未达到允许降级的配置阈值 {failure_threshold} 次；"
                    "请再次查看，成功后再重试转发。"
                )
            elif observation_kind is ViewObservationKind.EMPTY_CONTENT_FAILURE:
                retry_detail = (
                    "view_forward_message 首次返回空内容；请再进行一次诊断查看。"
                    "若仍为空，插件才允许使用摘要或消息预览降级。"
                )
            else:
                retry_detail = "尚未取得成功的 view_forward_message 完整内容，请先完成查看后再重试转发。"
            return self.failure(retry_detail)

        expanded_content = str(content_summary or "").strip()
        if expanded_content:
            self._ctx.logger.info(
                "%s，使用 Planner 摘要降级: msg_id=%s",
                fallback_reason,
                source_message_id,
            )
            return expanded_content

        self._ctx.logger.warning(
            "%s，且 Planner 摘要缺失，使用消息预览降级: msg_id=%s",
            fallback_reason,
            source_message_id,
        )
        return str(message.get("processed_plain_text") or "").strip() or "[合并转发消息]"

    def _duplicate_result(
        self,
        state_key: str,
        job_id: str,
        target_group_ids: list[str],
    ) -> dict[str, Any] | None:
        """判断请求是否正在处理或已完成当前路由。

        当目标群 Planner 功能关闭时，以 ``CONTEXT_APPENDED`` 为完成标准；
        开启时要求所有目标达到 ``PLANNER_QUEUED``。

        Args:
            state_key: 当前 source stream 与消息 ID 的永久幂等键。
            job_id: 面向日志和 Planner 的短任务 ID。
            target_group_ids: 当前配置生成的目标 QQ 群列表。

        Returns:
            重复任务返回 ``accepted=False`` 的成功结果字典；任务尚未活动且
            未完成时返回 ``None``，表示可以创建新后台任务或恢复未完成阶段。
        """

        if self._delivery.is_active(state_key):
            return {
                "success": True,
                "content": f"该合并转发已经在处理中，任务 ID：{job_id}。",
                "job_id": job_id,
                "accepted": False,
            }
        required_stage = (
            TargetStage.PLANNER_QUEUED if self.config.behavior.trigger_target_planner else TargetStage.CONTEXT_APPENDED
        )
        if self._state.is_complete(state_key, target_group_ids, required_stage):
            return {
                "success": True,
                "content": f"该合并转发已经完成当前白名单投递，任务 ID：{job_id}。",
                "job_id": job_id,
                "accepted": False,
            }
        return None

    @staticmethod
    def failure(content: str) -> dict[str, Any]:
        """构造统一的 Planner 可读失败结果。

        Args:
            content: 说明拒绝或失败原因的简体中文文本。

        Returns:
            ``{"success": False, "content": content}`` 字典。
        """

        return {"success": False, "content": content}
