"""转发请求校验与任务构造。"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
from typing import Any

from forward_messages_auto.cache import ViewResultCache
from forward_messages_auto.config import ForwardMessagesAutoConfig, GroupIdList
from forward_messages_auto.delivery import ForwardDeliveryService
from forward_messages_auto.models import ForwardJob, TargetStage
from forward_messages_auto.parsing import ForwardMessageParser
from forward_messages_auto.state import ForwardStateStore


class ForwardRequestService:
    """校验 Tool 调用、读取源消息并创建幂等任务。"""

    def __init__(
        self,
        context: Any,
        config_provider: Callable[[], ForwardMessagesAutoConfig],
        view_cache: ViewResultCache,
        state: ForwardStateStore,
        delivery: ForwardDeliveryService,
    ) -> None:
        self._ctx = context
        self._config_provider = config_provider
        self._view_cache = view_cache
        self._state = state
        self._delivery = delivery

    @property
    def config(self) -> ForwardMessagesAutoConfig:
        return self._config_provider()

    async def create(
        self,
        msg_id: str,
        sharing_reason: str,
        content_summary: str,
        invocation_context: dict[str, Any],
    ) -> dict[str, Any]:
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
            source_group_id,
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

        state_key = self._build_state_key(
            source_stream_id,
            source_message_id,
            target_group_ids,
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
        if not isinstance(result, dict) or not result.get("success", False):
            error = result.get("error", "未知错误") if isinstance(result, dict) else "返回格式错误"
            return {"_failure": f"读取源消息失败：{error}"}
        message = result.get("message")
        if not isinstance(message, dict):
            return {"_failure": "没有找到指定的源消息。"}
        return message

    @staticmethod
    def _validate_source_message(
        message: dict[str, Any],
        source_stream_id: str,
        source_group_id: str,
    ) -> str:
        if str(message.get("session_id") or "").strip() != source_stream_id:
            return "指定消息不属于当前 source 聊天流。"
        if str(message.get("group_id") or "").strip() != source_group_id:
            return "指定消息不属于当前 source 群。"
        if str(message.get("platform") or "").strip().lower() != "qq":
            return "指定消息不是 QQ 群聊消息。"
        return ""

    def _resolve_expanded_content(
        self,
        source_stream_id: str,
        source_message_id: str,
        content_summary: str,
        message: dict[str, Any],
    ) -> str:
        expanded_content = self._view_cache.get(
            source_stream_id,
            source_message_id,
            self.config.behavior.view_cache_ttl_seconds,
        )
        if expanded_content:
            return expanded_content

        expanded_content = str(content_summary or "").strip()
        if expanded_content:
            self._ctx.logger.info(
                "完整内容缓存未命中，使用 Planner 摘要: msg_id=%s",
                source_message_id,
            )
            return expanded_content

        self._ctx.logger.warning(
            "完整内容缓存及 Planner 摘要均缺失，使用消息预览: msg_id=%s",
            source_message_id,
        )
        return str(message.get("processed_plain_text") or "").strip() or "[合并转发消息]"

    def _duplicate_result(
        self,
        state_key: str,
        job_id: str,
        target_group_ids: list[str],
    ) -> dict[str, Any] | None:
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
    def _build_state_key(
        stream_id: str,
        message_id: str,
        target_group_ids: list[str],
    ) -> str:
        route_text = ",".join(sorted(set(target_group_ids)))
        digest = hashlib.sha256(f"{stream_id}\n{message_id}\n{route_text}".encode()).hexdigest()[:16]
        return f"{stream_id}:{message_id}:{digest}"

    @staticmethod
    def failure(content: str) -> dict[str, Any]:
        return {"success": False, "content": content}
