"""转发 Tool 的可信 Planner 会话授权。"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from secrets import token_urlsafe
from typing import Any

FORWARD_TOOL_NAME = "request_cross_group_forward"
FORWARD_CONTEXT_TOKEN_ARGUMENT = "_forward_context_token"
PUBLIC_FORWARD_ARGUMENTS = ("msg_id", "sharing_reason", "content_summary")
MAX_PENDING_AUTHORIZATIONS = 1024


class ForwardInvocationGate:
    """清洗模型参数，并以一次性凭据绑定真实 Planner 会话。"""

    def __init__(self) -> None:
        """创建空的一次性授权表。

        授权只覆盖尚未进入正式 Tool handler 的调用，并限制最大数量。插件
        重载或配置更新会清空全部凭据；已经进入投递服务的任务不受影响。
        """

        self._authorizations: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def sanitize_and_authorize(self, session_id: str, tool_calls: Any) -> Any:
        """清洗全部转发调用，并为本轮有效调用签发新会话凭据。

        模型提供的 ``platform``、``group_id``、``stream_id`` 等未声明字段
        会被删除。非转发工具和供应商附加字段保持不变。每次 EARLY Hook
        调用都会先撤销该 session 尚未消费的旧凭据，再为当前批次签发新凭据；
        模型回放历史参数不能复用旧授权。非列表输入原样返回。

        Args:
            session_id: ``after_response`` Hook 提供的真实 Planner 会话 ID；
                为空时只清洗参数，不签发凭据。
            tool_calls: Host 序列化后的工具调用列表。

        Returns:
            清洗后的工具调用深拷贝；非列表输入保持原值。
        """

        if not isinstance(tool_calls, list):
            return tool_calls

        normalized_session_id = str(session_id or "").strip()
        if normalized_session_id:
            self._revoke_session(normalized_session_id)
        sanitized_calls = deepcopy(tool_calls)
        for tool_call in sanitized_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != FORWARD_TOOL_NAME:
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, dict):
                continue

            sanitized_arguments = {
                key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key in arguments
            }
            message_id = str(sanitized_arguments.get("msg_id") or "").strip()
            if message_id:
                sanitized_arguments["msg_id"] = message_id

            if normalized_session_id and message_id:
                sanitized_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] = self._issue(
                    normalized_session_id,
                    message_id,
                )
            function["arguments"] = sanitized_arguments
        return sanitized_calls

    def sanitize_for_orchestration(self, session_id: str, tool_calls: Any) -> Any:
        """清洗 LATE Hook 参数，并只保留 EARLY 已签发的有效凭据。

        本方法绝不签发或改绑凭据。两个 Hook 之间若配置更新清空授权、其他
        Hook 改写 ``session_id``，或调用携带未知凭据，内部字段会被删除，
        自动查看协调器不会接管，正式 handler 最终按缺少授权拒绝。

        Args:
            session_id: LATE ``after_response`` Hook 当前收到的 Planner 会话 ID。
            tool_calls: 上游 Hook 已处理的序列化工具调用列表。

        Returns:
            清洗后的工具调用深拷贝。只有凭据仍精确绑定当前 session 与
            ``msg_id`` 时才保留内部字段；非列表输入保持原值。
        """

        if not isinstance(tool_calls, list):
            return tool_calls

        normalized_session_id = str(session_id or "").strip()
        sanitized_calls = deepcopy(tool_calls)
        for tool_call in sanitized_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != FORWARD_TOOL_NAME:
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, dict):
                continue

            sanitized_arguments = {
                key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key in arguments
            }
            message_id = str(sanitized_arguments.get("msg_id") or "").strip()
            if message_id:
                sanitized_arguments["msg_id"] = message_id

            token = str(arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
            if token and self._authorizations.get(token) == (
                normalized_session_id,
                message_id,
            ):
                sanitized_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] = token
            elif token:
                self._authorizations.pop(token, None)
            function["arguments"] = sanitized_arguments
        return sanitized_calls

    def authorize_arguments(
        self,
        session_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """为插件恢复的转发参数签发一次性会话凭据。

        Args:
            session_id: 原始直接请求所属的真实 Planner 会话 ID。
            arguments: 仅包含公开 Tool 字段的原始请求参数。

        Returns:
            清洗后的独立参数字典。会话和 ``msg_id`` 均有效时额外包含内部
            一次性凭据；格式无效时不签发，正式处理器将安全拒绝。
        """

        normalized_session_id = str(session_id or "").strip()
        sanitized_arguments = {key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key in arguments}
        message_id = str(sanitized_arguments.get("msg_id") or "").strip()
        if message_id:
            sanitized_arguments["msg_id"] = message_id
        if normalized_session_id and message_id:
            sanitized_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] = self._issue(
                normalized_session_id,
                message_id,
            )
        return sanitized_arguments

    def consume(self, message_id: str, invocation_context: dict[str, Any]) -> str:
        """消费一次性凭据并返回其绑定的真实 Planner 会话。

        正式 Tool handler 不信任模型可写的 Host 上下文字段，只使用本方法
        返回的会话 ID。凭据无论匹配与否都会在首次消费时删除，避免猜测或
        错配调用反复试用。

        Args:
            message_id: 本次 Tool 调用声明的源消息 ID。
            invocation_context: Runner 传给 Tool handler 的完整参数字典；
                方法只读取内部凭据字段。

        Returns:
            凭据与规范化 ``message_id`` 匹配时返回真实 ``session_id``；
            缺失、未知或错配时返回空字符串。
        """

        token = str(invocation_context.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
        if not token:
            return ""
        authorization = self._authorizations.pop(token, None)
        if authorization is None:
            return ""
        session_id, authorized_message_id = authorization
        if authorized_message_id != str(message_id or "").strip():
            return ""
        return session_id

    def revoke_arguments(self, arguments: dict[str, Any]) -> None:
        """撤销一组被自动查看流程替换的未消费参数凭据。

        Args:
            arguments: 含内部凭据的清洗后转发参数。字段缺失或未知时忽略。
        """

        token = str(arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
        if token:
            self._authorizations.pop(token, None)

    def clear(self) -> None:
        """清除全部尚未消费的一次性 Planner 会话凭据。"""

        self._authorizations.clear()

    def _issue(self, session_id: str, message_id: str) -> str:
        """生成并登记一条有容量上限的一次性授权。

        Args:
            session_id: 授权绑定的真实 Planner 会话 ID。
            message_id: 授权绑定的源消息 ID。

        Returns:
            不透明且不可由模型预测的随机凭据字符串。
        """

        token = token_urlsafe(32)
        self._authorizations[token] = (session_id, message_id)
        while len(self._authorizations) > MAX_PENDING_AUTHORIZATIONS:
            self._authorizations.popitem(last=False)
        return token

    def _revoke_session(self, session_id: str) -> None:
        """撤销某个 Planner 会话尚未消费的全部旧凭据。

        Args:
            session_id: 即将开始新一轮 EARLY Hook 授权的 Planner 会话 ID。
        """

        stale_tokens = [
            token
            for token, (authorized_session_id, _) in self._authorizations.items()
            if authorized_session_id == session_id
        ]
        for token in stale_tokens:
            self._authorizations.pop(token, None)
