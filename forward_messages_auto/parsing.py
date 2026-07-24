"""消息和 Planner 历史解析。"""

from __future__ import annotations

from typing import Any

VIEW_FORWARD_TOOL_NAME = "view_forward_message"


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
        无关工具、无 ID 调用、空结果和格式异常消息都会被忽略。

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
        results: list[tuple[str, str]],
    ) -> None:
        """将一条可配对的 tool 结果追加到结果列表。

        Args:
            message: 角色为 tool 的 Planner 历史消息。
            call_to_message_id: 已收集的工具调用 ID 到源消息 ID 的映射。
            results: 接收 ``(msg_id, content)`` 的可变结果列表。只有调用 ID
                已知且文本结果非空时才会追加。
        """

        call_id = str(message.get("tool_call_id") or "").strip()
        message_id = call_to_message_id.get(call_id, "")
        content = message.get("content")
        if message_id and isinstance(content, str) and content.strip():
            results.append((message_id, content.strip()))
