"""source Planner 路径测试共享构造辅助函数。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


from forward_messages_auto.source.authorization import (
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
)
from forward_messages_auto.source.authorization import FORWARD_TOOL_NAME


def build_forward_call(
    *,
    call_id: str = "request-call",
    message_id: str = "forward-message",
    sharing_reason: str = "预览看起来很有意思",
    content_summary: str = "预览摘要",
) -> dict[str, Any]:
    """构造 Host ``after_response`` 契约中的直接转发调用。

    Args:
        call_id: 模型生成的原始工具调用 ID。
        message_id: Planner 请求分享的源消息 ID。
        sharing_reason: 应在自动查看后原样恢复的分享理由。
        content_summary: 达到 fallback 条件后可使用的降级摘要。

    Returns:
        使用嵌套 ``function`` 和字典参数的序列化工具调用。
    """

    return {
        "id": call_id,
        "function": {
            "name": FORWARD_TOOL_NAME,
            "arguments": {
                "msg_id": message_id,
                "sharing_reason": sharing_reason,
                "content_summary": content_summary,
            },
        },
    }


def build_view_history(
    view_call: dict[str, Any],
    content: str,
    *,
    previous_messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把一次系统查看调用及结果追加为 Planner 可见历史。

    Args:
        view_call: 自动查看协调器生成的序列化工具调用。
        content: ``view_forward_message`` 返回的文本；空串模拟空内容失败。
        previous_messages: 可选的既有历史，传入时先深拷贝再追加新调用。

    Returns:
        包含 assistant 工具调用和配对 tool 结果的独立消息列表。
    """

    messages = deepcopy(previous_messages or [])
    messages.extend(
        [
            {"role": "assistant", "content": "", "tool_calls": [deepcopy(view_call)]},
            {
                "role": "tool",
                "content": content,
                "tool_call_id": view_call["id"],
            },
        ]
    )
    return messages


def public_forward_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    """读取调用中的公开转发参数并确认内部凭据存在。

    Args:
        tool_call: 已经过 EARLY 清洗或由路径 B 恢复的转发调用。

    Returns:
        删除内部一次性凭据后的独立公开参数字典。
    """

    arguments = deepcopy(tool_call["function"]["arguments"])
    token = str(arguments.pop(FORWARD_CONTEXT_TOKEN_ARGUMENT, "") or "").strip()
    assert token
    return arguments


async def run_after_response_hooks(
    plugin: Any,
    *,
    session_id: str,
    response: str,
    tool_calls: list[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    """按 Host 顺序执行 EARLY 授权与 LATE 自动查看 Hook。

    Args:
        plugin: 已加载并注入测试上下文的插件实例。
        session_id: Host 为当前 Planner 轮提供的真实会话 ID。
        response: 模型生成的文本响应。
        tool_calls: 模型生成、尚未清洗的序列化工具调用列表。
        **kwargs: 要在两个 Hook 之间保留的其他响应字段，例如 token 统计。

    Returns:
        LATE Hook 返回的阻塞继续结果，包含最终响应和工具调用。
    """

    early_result = await plugin.authorize_forward_request_context(
        session_id=session_id,
        response=response,
        tool_calls=tool_calls,
        **kwargs,
    )
    return await plugin.orchestrate_view_before_forward(
        **early_result["modified_kwargs"],
    )
