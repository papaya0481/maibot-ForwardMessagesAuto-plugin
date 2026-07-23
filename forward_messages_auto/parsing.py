"""消息和 Planner 历史解析。"""

from __future__ import annotations

from typing import Any

VIEW_FORWARD_TOOL_NAME = "view_forward_message"


class ForwardMessageParser:
    """将 Host 消息转换为可发送的合并转发数据。"""

    @staticmethod
    def extract(message: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
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
        if not isinstance(messages, list):
            return []

        call_to_message_id: dict[str, str] = {}
        results: list[tuple[str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role == "assistant":
                PlannerHistoryParser._record_calls(message, call_to_message_id)
            elif role == "tool":
                PlannerHistoryParser._record_result(message, call_to_message_id, results)
        return results

    @staticmethod
    def _record_calls(message: dict[str, Any], call_to_message_id: dict[str, str]) -> None:
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
        results: list[tuple[str, str]],
    ) -> None:
        call_id = str(message.get("tool_call_id") or "").strip()
        message_id = call_to_message_id.get(call_id, "")
        content = message.get("content")
        if message_id and isinstance(content, str) and content.strip():
            results.append((message_id, content.strip()))


class ToolDefinition:
    """读取并筛选 Planner 工具定义。"""

    @staticmethod
    def name(definition: Any) -> str:
        if not isinstance(definition, dict):
            return ""
        function = definition.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "").strip()
        return str(definition.get("name") or "").strip()

    @classmethod
    def excluding(cls, definitions: list[Any], tool_name: str) -> list[Any]:
        return [definition for definition in definitions if cls.name(definition) != tool_name]
