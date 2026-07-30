"""消息和 Planner 历史解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ViewObservationKind

VIEW_FORWARD_TOOL_NAME = "view_forward_message"
VIEW_FORWARD_CORRECTABLE_FAILURE_PREFIXES = (
    "查看转发消息工具需要提供有效的 `msg_id` 参数。",
    "未找到目标转发消息，msg_id=",
)
VIEW_FORWARD_TERMINAL_FAILURE_PREFIXES = ("目标消息不是可展开查看的转发消息，msg_id=",)
VIEW_FORWARD_RETRYABLE_FAILURE_PREFIXES = (
    "查看转发消息完整内容时发生异常。",
    "工具 view_forward_message 调用失败：",
    "未找到工具：view_forward_message",
    "未找到内置工具处理器：view_forward_message",
    "统一工具注册表尚未初始化。",
)
VIEW_FORWARD_EMPTY_CONTENT_FAILURE_PREFIXES = ("转发消息内容为空，msg_id=",)
VIEW_FORWARD_UNKNOWN_FAILURE_PREFIXES = ("工具 view_forward_message 执行失败。",)


@dataclass(frozen=True, slots=True)
class ViewToolObservation:
    """一组已经配对的查看工具调用及文本结果。"""

    call_id: str
    message_id: str
    content: str
    kind: ViewObservationKind


class ForwardMessageParser:
    """将 Host 消息转换为可发送的合并转发数据。"""

    @staticmethod
    def extract(message: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """从 Host 消息中提取首个有效的合并转发消息段。

        解析器会遍历 ``raw_message``，跳过非 ``forward`` 段、空节点和
        缺少有效消息内容的节点。媒体段中的二进制字段会原样复制，以便
        后续 ``ctx.send.forward`` 重建消息。

        Args:
            message: ``ctx.message.get_by_id`` 返回的 Host 消息字典。

        Returns:
            成功时返回二元组：第一个元素是适合写入 Maisaka 上下文的
            ``{"type": "forward", "data": ...}`` 消息段；第二个元素是
            适合传给 ``ctx.send.forward`` 的节点列表。没有有效合并转发
            段时返回 ``None``。
        """

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return None

        for segment in raw_message:
            if not ForwardMessageParser._is_forward_segment(segment):
                continue
            payload = ForwardMessageParser._normalize_nodes(segment["data"])
            if payload is not None:
                return payload
        return None

    @staticmethod
    def _is_forward_segment(segment: Any) -> bool:
        """判断原始消息段是否为包含节点的合并转发段。

        Args:
            segment: ``raw_message`` 中的任意元素。

        Returns:
            当元素为字典、类型为 ``forward`` 且 ``data`` 是非空列表时
            返回 ``True``，否则返回 ``False``。
        """

        return (
            isinstance(segment, dict)
            and str(segment.get("type") or "").strip().lower() == "forward"
            and isinstance(segment.get("data"), list)
            and bool(segment["data"])
        )

    @staticmethod
    def _normalize_nodes(
        raw_nodes: list[Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """规范化合并转发节点，并构造上下文与发送两种表示。

        无效节点会被跳过。缺失昵称时使用“未知用户”，缺失消息 ID 时按
        节点下标生成稳定的占位 ID；消息内容中的未知字段和媒体二进制数据
        会通过浅拷贝保留。

        Args:
            raw_nodes: Host ``forward`` 消息段中的原始节点列表。

        Returns:
            至少存在一个有效节点时，返回上下文消息段与发送节点列表组成的
            二元组；所有节点均无效时返回 ``None``。
        """

        normalized_nodes: list[dict[str, Any]] = []
        forward_messages: list[dict[str, Any]] = []
        for index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                continue
            raw_content = raw_node.get("content")
            if not isinstance(raw_content, list):
                continue
            content = [dict(item) for item in raw_content if isinstance(item, dict)]
            if not content:
                continue

            node = {
                "user_id": str(raw_node.get("user_id") or ""),
                "user_nickname": str(raw_node.get("user_nickname") or "未知用户"),
                "user_cardname": str(raw_node.get("user_cardname") or ""),
                "message_id": str(raw_node.get("message_id") or f"forward_node_{index}"),
                "content": content,
            }
            normalized_nodes.append(node)
            forward_messages.append(
                {
                    "user_id": node["user_id"],
                    "nickname": node["user_nickname"],
                    "user_cardname": node["user_cardname"],
                    "message_id": node["message_id"],
                    "segments": content,
                }
            )

        if not normalized_nodes:
            return None
        return {"type": "forward", "data": normalized_nodes}, forward_messages


class PlannerHistoryParser:
    """从 Planner 历史中配对查看工具调用及结果。"""

    @staticmethod
    def extract_view_results(messages: Any) -> list[tuple[str, str]]:
        """提取 ``view_forward_message`` 调用对应的完整文本结果。

        方法按消息出现顺序扫描 Planner 历史，先记录 assistant 工具调用中
        的 ``call_id -> msg_id``，再用 tool 消息中的 ``tool_call_id`` 配对。
        无关工具、无 ID 调用和格式异常消息都会被忽略；空结果会被记录为
        空内容失败，因此不会出现在本方法返回的成功结果中。

        Args:
            messages: Planner 请求中的消息历史。期望为 OpenAI 兼容字典列表；
                传入其他类型时视为空历史。

        Returns:
            ``(msg_id, content)`` 二元组列表，顺序与匹配到的 tool 结果顺序
            一致。没有匹配项时返回空列表。

        Examples:
            assistant 调用 ID 为 ``call-1``、参数 ``msg_id="m1"``，随后 tool
            结果引用 ``call-1`` 时，返回 ``[("m1", "完整内容")]``。
        """

        return [
            (observation.message_id, observation.content)
            for observation in PlannerHistoryParser.extract_view_observations(messages)
            if observation.kind is ViewObservationKind.SUCCESS
        ]

    @staticmethod
    def extract_view_observations(messages: Any) -> list[ViewToolObservation]:
        """提取查看调用、结果文本和插件可识别的结果分类。

        当前 Host 的 ``before_request`` Hook 不提供 ToolResult ``success``
        字段，因此结果分类暂时通过内置工具和统一注册表的稳定错误前缀
        判断。未命中已知失败前缀的非空结果按成功处理。

        Args:
            messages: Planner 请求中的 OpenAI 兼容消息字典列表。

        Returns:
            按 tool 消息出现顺序排列的 ``ViewToolObservation`` 列表。无法
            配对或结构异常的消息会被忽略；已配对的空字符串或纯空白结果
            会分类为 ``EMPTY_CONTENT_FAILURE``。
        """

        if not isinstance(messages, list):
            return []

        call_to_message_id: dict[str, str] = {}
        observations: list[ViewToolObservation] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role == "assistant":
                PlannerHistoryParser._record_calls(message, call_to_message_id)
            elif role == "tool":
                PlannerHistoryParser._record_result(message, call_to_message_id, observations)
        return observations

    @staticmethod
    def has_tool_result(messages: Any, call_id: str) -> bool:
        """判断当前 Planner 历史是否包含指定工具调用的结果。

        Args:
            messages: Planner 请求中的 OpenAI 兼容消息字典列表。
            call_id: 要查找的非空工具调用 ID。

        Returns:
            存在角色为 ``tool`` 且 ``tool_call_id`` 精确匹配的消息时返回
            ``True``；输入格式异常或结果不存在时返回 ``False``。
        """

        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id or not isinstance(messages, list):
            return False
        return any(
            isinstance(message, dict)
            and str(message.get("role") or "").strip().lower() == "tool"
            and str(message.get("tool_call_id") or "").strip() == normalized_call_id
            for message in messages
        )

    @staticmethod
    def _record_calls(message: dict[str, Any], call_to_message_id: dict[str, str]) -> None:
        """记录一条 assistant 消息中的查看工具调用。

        兼容 ``function`` 嵌套定义和扁平工具定义。该方法会原地更新调用
        映射，不返回新字典。

        Args:
            message: 角色为 assistant 的 Planner 历史消息。
            call_to_message_id: 可变映射，键为工具调用 ID，值为被查看的
                ``msg_id``。
        """

        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            return
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if isinstance(function, dict):
                tool_name = str(function.get("name") or "").strip()
                arguments = function.get("arguments")
            else:
                tool_name = str(tool_call.get("name") or "").strip()
                arguments = tool_call.get("arguments")
            if tool_name != VIEW_FORWARD_TOOL_NAME or not isinstance(arguments, dict):
                continue
            call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
            message_id = str(arguments.get("msg_id") or "").strip()
            if call_id and message_id:
                call_to_message_id[call_id] = message_id

    @staticmethod
    def _record_result(
        message: dict[str, Any],
        call_to_message_id: dict[str, str],
        observations: list[ViewToolObservation],
    ) -> None:
        """将一条可配对的 tool 结果追加到结果列表。

        Args:
            message: 角色为 tool 的 Planner 历史消息。
            call_to_message_id: 已收集的工具调用 ID 到源消息 ID 的映射。
            observations: 接收已配对查看结果的可变列表。调用 ID 已知且
                ``content`` 为字符串时追加；空白文本按空内容失败记录。
        """

        call_id = str(message.get("tool_call_id") or "").strip()
        message_id = call_to_message_id.get(call_id, "")
        content = message.get("content")
        if not message_id or not isinstance(content, str):
            return

        normalized_content = content.strip()
        observations.append(
            ViewToolObservation(
                call_id=call_id,
                message_id=message_id,
                content=normalized_content,
                kind=(
                    PlannerHistoryParser._classify_view_result(normalized_content)
                    if normalized_content
                    else ViewObservationKind.EMPTY_CONTENT_FAILURE
                ),
            )
        )

    @staticmethod
    def _classify_view_result(content: str) -> ViewObservationKind:
        """根据当前 Host 的稳定文本前缀分类查看结果。

        Args:
            content: 已去除首尾空白的非空 ToolResult 文本。

        Returns:
            对应的 ``ViewObservationKind``。未匹配任何已知失败前缀时返回
            ``SUCCESS``，因为当前 Hook 无法读取 ToolResult 的成功标志。
        """

        if content.startswith(VIEW_FORWARD_CORRECTABLE_FAILURE_PREFIXES):
            return ViewObservationKind.CORRECTABLE_FAILURE
        if content.startswith(VIEW_FORWARD_TERMINAL_FAILURE_PREFIXES):
            return ViewObservationKind.TERMINAL_FAILURE
        if content.startswith(VIEW_FORWARD_RETRYABLE_FAILURE_PREFIXES):
            return ViewObservationKind.RETRYABLE_FAILURE
        if content.startswith(VIEW_FORWARD_EMPTY_CONTENT_FAILURE_PREFIXES):
            return ViewObservationKind.EMPTY_CONTENT_FAILURE
        if content.startswith(VIEW_FORWARD_UNKNOWN_FAILURE_PREFIXES):
            return ViewObservationKind.UNKNOWN_FAILURE
        return ViewObservationKind.SUCCESS
