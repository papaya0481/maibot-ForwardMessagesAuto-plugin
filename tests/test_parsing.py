"""合并转发与 Planner 查看结果解析的回归测试。"""

from __future__ import annotations

import pytest

from forward_messages_auto.models import ViewObservationKind
from forward_messages_auto.parsing import ForwardMessageParser, PlannerHistoryParser
from forward_messages_auto.view_context import ViewEligibilityStore
from tests.support import build_forward_message


def test_extract_forward_payload_preserves_nodes_and_binary_data() -> None:
    """验证合并转发解析同时生成上下文段和发送节点。

    期望解析结果保留 ``forward`` 类型、节点昵称以及图片
    ``binary_data_base64``。该测试防止消息规范化过程中丢失发送者信息或
    媒体二进制数据，导致目标群收到不完整内容。
    """

    payload = ForwardMessageParser.extract(build_forward_message())
    assert payload is not None
    segment, messages = payload
    assert segment["type"] == "forward"
    assert messages[0]["nickname"] == "群友甲"
    assert messages[0]["segments"][1]["binary_data_base64"] == "aW1hZ2U="


def test_extract_view_forward_results_pairs_tool_call_and_result() -> None:
    """验证 Planner 历史按工具调用 ID 配对消息 ID 与完整结果。

    assistant 消息声明 ``call-1`` 查看 ``message-1``，tool 消息再引用同一
    ID。期望返回唯一二元组 ``("message-1", "完整展开内容")``。该测试
    防止查看资格把结果关联到错误 source 消息。
    """

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "view_forward_message",
                        "arguments": {"msg_id": "message-1"},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": "完整展开内容",
            "tool_call_id": "call-1",
        },
    ]
    assert PlannerHistoryParser.extract_view_results(messages) == [("message-1", "完整展开内容")]


@pytest.mark.parametrize(
    ("content", "expected_kind"),
    [
        (
            "未找到目标转发消息，msg_id=missing",
            ViewObservationKind.CORRECTABLE_FAILURE,
        ),
        (
            "目标消息不是可展开查看的转发消息，msg_id=plain",
            ViewObservationKind.TERMINAL_FAILURE,
        ),
        (
            "查看转发消息完整内容时发生异常。",
            ViewObservationKind.RETRYABLE_FAILURE,
        ),
        (
            "转发消息内容为空，msg_id=empty",
            ViewObservationKind.EMPTY_CONTENT_FAILURE,
        ),
        ("", ViewObservationKind.EMPTY_CONTENT_FAILURE),
        (" \n ", ViewObservationKind.EMPTY_CONTENT_FAILURE),
        (
            "工具 view_forward_message 执行失败。",
            ViewObservationKind.UNKNOWN_FAILURE,
        ),
    ],
)
def test_view_result_parser_classifies_known_host_failures(
    content: str,
    expected_kind: ViewObservationKind,
) -> None:
    """验证已知 Host 失败文本会被分类而不是登记为完整内容。

    每种稳定错误前缀以及原始空白结果都应映射为可修正、终止、可重试、
    空内容或未知失败，同时成功结果接口返回空列表。该测试防止错误文本
    写入目标群上下文，也防止空结果被遗漏或不可恢复错误被误计入阈值。

    Args:
        content: pytest 参数化提供的 Host ToolResult 文本。
        expected_kind: pytest 参数化提供的预期失败分类。
    """

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "failed-call",
                    "function": {
                        "name": "view_forward_message",
                        "arguments": {"msg_id": "message-1"},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": content,
            "tool_call_id": "failed-call",
        },
    ]

    observations = PlannerHistoryParser.extract_view_observations(messages)
    assert len(observations) == 1
    assert observations[0].call_id == "failed-call"
    assert observations[0].message_id == "message-1"
    assert observations[0].kind is expected_kind
    assert PlannerHistoryParser.extract_view_results(messages) == []


def test_empty_raw_view_results_reach_fallback_counter() -> None:
    """验证连续原始空结果会进入查看资格的空内容降级计数。

    两个独立 ``view_forward_message`` 调用分别返回空字符串和纯空白文本时，
    解析器应保留两条空内容失败，资格存储应得到连续计数 ``2``。该测试防止
    解析层再次丢弃空结果，导致既定的第二次空内容降级永远无法触发。
    """

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"empty-call-{index}",
                    "function": {
                        "name": "view_forward_message",
                        "arguments": {"msg_id": "message-1"},
                    },
                }
            ],
        }
        for index in range(2)
    ]
    messages.insert(
        1,
        {
            "role": "tool",
            "content": "",
            "tool_call_id": "empty-call-0",
        },
    )
    messages.append(
        {
            "role": "tool",
            "content": " \n ",
            "tool_call_id": "empty-call-1",
        }
    )

    observations = PlannerHistoryParser.extract_view_observations(messages)
    store = ViewEligibilityStore()
    store.sync_context("stream-1", observations)

    lookup = store.lookup("stream-1", "message-1")
    assert len(observations) == 2
    assert all(item.kind is ViewObservationKind.EMPTY_CONTENT_FAILURE for item in observations)
    assert lookup.empty_content_failure_count == 2
