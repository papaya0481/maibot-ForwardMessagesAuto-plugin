"""未查看转发请求的 Planner 续轮编排。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .config import ForwardMessagesAutoConfig
from .invocation_gate import (
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
    FORWARD_TOOL_NAME,
    PUBLIC_FORWARD_ARGUMENTS,
    ForwardInvocationGate,
)
from .models import ViewEligibilityLookup, ViewEligibilityStatus, ViewObservationKind
from .parsing import PlannerHistoryParser, VIEW_FORWARD_TOOL_NAME, ViewToolObservation
from .request import EMPTY_CONTENT_FALLBACK_THRESHOLD
from .view_context import ViewEligibilityStore


@dataclass(slots=True)
class PendingDirectForward:
    """一条等待系统查看后恢复的直接转发请求。"""

    message_id: str
    arguments: dict[str, Any]
    view_call_id: str
    view_result_seen: bool = False
    restored_call_id: str = ""


class ViewBeforeForwardCoordinator:
    """在 Planner 内部续轮中执行“先查看、再恢复转发请求”。"""

    def __init__(
        self,
        config_provider: Callable[[], ForwardMessagesAutoConfig],
        view_eligibility: ViewEligibilityStore,
        invocation_gate: ForwardInvocationGate,
        preflight: Callable[[str, str], Awaitable[bool]],
        logger: Any,
    ) -> None:
        """创建与一次插件生命周期绑定的直接请求协调器。

        协调器只保存尚未发送的原始工具参数，不持久化媒体内容。插件卸载或
        上下文丢失时，待续接请求会安全停止；现有请求处理器仍作为最终权限、
        消息类型和查看资格防线。

        Args:
            config_provider: 返回当前 ``ForwardMessagesAutoConfig`` 的无参数
                回调，用于读取启用状态和查看失败降级阈值。
            view_eligibility: 当前 Planner 可见上下文的查看资格索引。
            invocation_gate: 清洗模型参数并签发一次性会话凭据的正式入口。
            preflight: 在真实查看前验证 source、消息、target 与防重状态的异步
                回调；返回 ``False`` 时保留原调用交给正式处理器拒绝。
            logger: MaiBot 插件日志对象，用于记录自动查看和续接边界。
        """

        self._config_provider = config_provider
        self._view_eligibility = view_eligibility
        self._invocation_gate = invocation_gate
        self._preflight = preflight
        self._logger = logger
        self._pending_by_session: dict[str, PendingDirectForward] = {}

    @property
    def config(self) -> ForwardMessagesAutoConfig:
        """读取当前生效的插件配置。

        Returns:
            配置提供器当前返回的 ``ForwardMessagesAutoConfig``。
        """

        return self._config_provider()

    def capture_context(
        self,
        session_id: str,
        messages: Any,
        observations: list[ViewToolObservation],
    ) -> set[str]:
        """对账系统合成的查看调用或已经恢复的转发调用。

        系统查看必须通过精确 ``call_id`` 与当前可见 ToolResult 配对，不能只
        根据相同 ``msg_id`` 复用其他调用。恢复后的转发已经进入下一轮时会
        立即释放 pending；若结果缺失会记录警告，但正式 handler 的一次性
        凭据和永久防重仍阻止重复发送。查看结果被裁剪或缺失时只标记为
        未见，后续响应不会擅自恢复转发。

        Args:
            session_id: 当前 source Planner 的聊天流 ID。
            messages: 本轮 ``before_request`` 可见的完整消息列表。
            observations: 已从 ``messages`` 解析出的查看调用及结果。

        Returns:
            由自动查看流程接管的 ``msg_id`` 集合。运行时用它排除路径 A 的
            自主判断提醒，避免自动恢复和再次判断同时发生。
        """

        normalized_session_id = str(session_id or "").strip()
        pending = self._pending_by_session.get(normalized_session_id)
        if pending is None:
            return set()

        if pending.restored_call_id:
            restored_result_seen = PlannerHistoryParser.has_tool_result(
                messages,
                pending.restored_call_id,
            )
            self._pending_by_session.pop(normalized_session_id, None)
            if not restored_result_seen:
                self._logger.warning(
                    "恢复后的转发结果不在当前 Planner 上下文，释放续接状态: stream=%s msg_id=%s",
                    normalized_session_id,
                    pending.message_id,
                )
            return set()

        matching_result = next(
            (
                observation
                for observation in observations
                if observation.call_id == pending.view_call_id and observation.message_id == pending.message_id
            ),
            None,
        )
        pending.view_result_seen = matching_result is not None
        if matching_result is None:
            return set()
        return {pending.message_id}

    async def transform_after_response(
        self,
        session_id: str,
        response: Any,
        tool_calls: Any,
    ) -> tuple[str, Any]:
        """改写 Planner 工具调用以串联真实查看和原始转发请求。

        首次遇到单独出现且尚无查看资格的转发调用时，将整批调用替换成一个
        ``view_forward_message``。下一续轮只有在精确查看结果仍可见时才继续：
        成功或达到既有降级阈值会恢复原请求，可重试故障会再次查看，其他
        失败则恢复原请求交给最终处理器返回安全错误。多工具或结构异常响应
        不会被重排。

        Args:
            session_id: 当前 Planner 聊天流 ID；为空时不执行改写。
            response: 模型原始文本响应，改写工具调用时替换为中性说明。
            tool_calls: Host 序列化后的工具调用列表。

        Returns:
            二元组包含最终响应文本和工具调用。未接管时原样返回；接管时
            工具列表只包含一个系统查看或恢复后的转发调用。
        """

        normalized_session_id = str(session_id or "").strip()
        normalized_response = str(response or "")
        sanitized_tool_calls = self._invocation_gate.sanitize_and_authorize(
            normalized_session_id,
            tool_calls,
        )
        if not normalized_session_id:
            return normalized_response, sanitized_tool_calls
        if not self.config.plugin.enabled:
            self._pending_by_session.pop(normalized_session_id, None)
            return normalized_response, sanitized_tool_calls

        pending = self._pending_by_session.get(normalized_session_id)
        if pending is not None:
            return self._advance_pending(
                normalized_session_id,
                pending,
                normalized_response,
                sanitized_tool_calls,
            )

        parsed_request = self._extract_single_forward_request(sanitized_tool_calls)
        if parsed_request is None:
            return normalized_response, sanitized_tool_calls
        message_id, arguments, sanitized_call = parsed_request

        lookup = self._view_eligibility.lookup(normalized_session_id, message_id)
        if not self._needs_another_view(lookup):
            return normalized_response, [sanitized_call]
        if not await self._preflight(normalized_session_id, message_id):
            self._logger.info(
                "直接转发请求未通过自动查看预检，保留给正式处理器: stream=%s msg_id=%s",
                normalized_session_id,
                message_id,
            )
            return normalized_response, [sanitized_call]

        self._invocation_gate.revoke_arguments(sanitized_call["function"]["arguments"])
        view_call = self._build_tool_call(VIEW_FORWARD_TOOL_NAME, {"msg_id": message_id})
        self._pending_by_session[normalized_session_id] = PendingDirectForward(
            message_id=message_id,
            arguments=arguments,
            view_call_id=view_call["id"],
        )
        self._logger.info(
            "转发请求缺少成功查看结果，先执行系统查看: stream=%s msg_id=%s",
            normalized_session_id,
            message_id,
        )
        return "转发前先查看这则合并转发的完整内容。", [view_call]

    def clear(self) -> None:
        """清除尚未发生物理发送的进程内续接请求。

        插件卸载或配置热更新时调用该方法，使旧参数不会脱离原 Planner
        上下文或跨越配置边界自动发送。已经开始的真实投递仍由
        ``ForwardDeliveryService`` 和持久化 target 阶段负责完成或恢复。
        """

        self._pending_by_session.clear()

    def _advance_pending(
        self,
        session_id: str,
        pending: PendingDirectForward,
        response: str,
        tool_calls: Any,
    ) -> tuple[str, Any]:
        """根据已捕获查看结果推进一条待续接请求。

        Args:
            session_id: 待续接请求所属的 Planner 聊天流 ID。
            pending: 当前聊天流保存的原始转发参数和合成调用 ID。
            response: 本轮模型原始文本响应。
            tool_calls: 本轮模型原始工具调用列表。

        Returns:
            尚未看到精确查看结果时原样返回并放弃旧 pending；需要继续查看
            时返回新的查看调用；其他已分类结果返回恢复后的原转发调用。
        """

        if pending.restored_call_id:
            return response, tool_calls
        if not pending.view_result_seen:
            self._pending_by_session.pop(session_id, None)
            self._logger.warning(
                "系统查看结果已不在当前 Planner 上下文，停止自动续接: stream=%s msg_id=%s",
                session_id,
                pending.message_id,
            )
            return response, tool_calls

        lookup = self._view_eligibility.lookup(session_id, pending.message_id)
        if self._needs_another_view(lookup):
            view_call = self._build_tool_call(
                VIEW_FORWARD_TOOL_NAME,
                {"msg_id": pending.message_id},
            )
            pending.view_call_id = view_call["id"]
            pending.view_result_seen = False
            self._logger.info(
                "系统查看尚未达到成功或降级条件，继续查看: stream=%s msg_id=%s",
                session_id,
                pending.message_id,
            )
            return "完整内容尚未取得，继续执行系统查看。", [view_call]

        restored_call = self._build_tool_call(
            FORWARD_TOOL_NAME,
            self._invocation_gate.authorize_arguments(
                session_id,
                pending.arguments,
            ),
        )
        pending.restored_call_id = restored_call["id"]
        self._logger.info(
            "系统查看阶段结束，恢复原转发请求: stream=%s msg_id=%s status=%s",
            session_id,
            pending.message_id,
            lookup.status.value,
        )
        return "已完成转发前查看，继续执行原转发请求。", [restored_call]

    def _needs_another_view(self, lookup: ViewEligibilityLookup) -> bool:
        """判断当前查看状态是否需要系统继续调用查看工具。

        Args:
            lookup: ``ViewEligibilityStore.lookup`` 返回的当前上下文状态。

        Returns:
            已成功查看、达到 fallback 阈值或出现不可自动重试的失败时返回
            ``False``；完全缺失、可重试故障未达阈值或首次空内容返回
            ``True``。
        """

        if lookup.status is ViewEligibilityStatus.READY:
            return False

        observation_kind = lookup.last_observation_kind
        if observation_kind is None:
            return True
        if observation_kind is ViewObservationKind.RETRYABLE_FAILURE:
            return lookup.retryable_failure_count < self.config.behavior.view_failure_fallback_threshold
        if observation_kind is ViewObservationKind.EMPTY_CONTENT_FAILURE:
            return lookup.empty_content_failure_count < EMPTY_CONTENT_FALLBACK_THRESHOLD
        return False

    @staticmethod
    def _extract_single_forward_request(
        tool_calls: Any,
    ) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        """读取结构完整且单独出现的直接转发调用。

        Args:
            tool_calls: Host ``after_response`` Hook 提供的序列化调用列表。

        Returns:
            唯一调用为 ``request_cross_group_forward`` 且含非空 ``msg_id``
            时返回消息 ID、已声明参数和清洗后的原调用；多工具或非法结构
            返回 ``None``。未声明字段不会进入恢复调用，避免覆盖 Host 注入
            上下文；原调用的 ID 与供应商附加字段保持不变。
        """

        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            return None
        tool_call = tool_calls[0]
        if not isinstance(tool_call, dict):
            return None
        call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
        function = tool_call.get("function")
        if not call_id or not isinstance(function, dict):
            return None
        if str(function.get("name") or "").strip() != FORWARD_TOOL_NAME:
            return None
        arguments = function.get("arguments")
        if not isinstance(arguments, dict):
            return None
        message_id = str(arguments.get("msg_id") or "").strip()
        if not message_id:
            return None
        copied_arguments = {
            key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key != "msg_id" and key in arguments
        }
        copied_arguments["msg_id"] = message_id
        sanitized_call = deepcopy(tool_call)
        sanitized_call["function"] = {
            "name": FORWARD_TOOL_NAME,
            "arguments": {
                **deepcopy(copied_arguments),
                **(
                    {FORWARD_CONTEXT_TOKEN_ARGUMENT: str(arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()}
                    if str(arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
                    else {}
                ),
            },
        }
        return message_id, copied_arguments, sanitized_call

    @staticmethod
    def _build_tool_call(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """构造符合 Host Hook 反序列化契约的工具调用。

        Args:
            tool_name: 要执行的注册工具名称。
            arguments: 传给工具的字典参数；方法会深拷贝以隔离后续修改。

        Returns:
            包含全新调用 ID、嵌套函数名和字典参数的序列化工具调用。
        """

        call_kind = "view" if tool_name == VIEW_FORWARD_TOOL_NAME else "request"
        return {
            "id": f"cross-forward-auto-{call_kind}-{uuid4().hex}",
            "function": {
                "name": tool_name,
                "arguments": deepcopy(arguments),
            },
        }
