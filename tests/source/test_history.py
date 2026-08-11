"""source Planner 查看历史解析的回归测试。"""

from __future__ import annotations

import pytest

from forward_messages_auto.source.history import PlannerHistoryParser
from forward_messages_auto.source.models import ViewEligibilityStatus, ViewObservationKind
from forward_messages_auto.source.view_state import ViewEligibilityStore
from tests.source.support import build_output_item_call, build_output_item_result


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


def test_view_observations_preserve_requested_and_discovered_nested_paths() -> None:
    """验证新旧 Planner 历史都保留调用路径和下一层嵌套路径。

    两种载荷都模拟查看 ``path=[0, 0]`` 后由 Host 返回更深的
    ``path=[0, 0, 0]`` 占位。期望解析结果同时保留已查看路径和新发现路径，
    防止完整性计算把单层成功结果误认为整条嵌套链已经展开。
    """

    content = (
        "【合并转发消息:\n"
        "【内层用户】: 内层正文 [嵌套转发消息，path=[0, 0, 0]，"
        "可再次调用 view_forward_message 展开] "
        "[嵌套转发消息，path=[0, 0, 1]，可再次调用 view_forward_message 展开]\n】"
    )
    legacy_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "legacy-nested-call",
                    "function": {
                        "name": "view_forward_message",
                        "arguments": {"msg_id": "message-1", "path": [0, 0]},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": content,
            "tool_call_id": "legacy-nested-call",
        },
    ]
    context_items = [
        build_output_item_call(
            call_id="context-nested-call",
            item_id="context-nested-call-item",
            tool_name="view_forward_message",
            message_id="message-1",
            path=[0, 0],
        ),
        build_output_item_result(
            "context-nested-call",
            content,
            item_id="context-nested-result-item",
        ),
    ]

    for observation in (
        PlannerHistoryParser.extract_view_observations(legacy_messages)[0],
        PlannerHistoryParser.extract_view_observations(context_items)[0],
    ):
        assert observation.path == (0, 0)
        assert observation.nested_paths == ((0, 0, 0), (0, 0, 1))
        assert observation.kind is ViewObservationKind.SUCCESS


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
    被登记为 source 查看资格，也防止空结果被遗漏或不可恢复错误被误计入阈值。

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


def test_context_item_explicit_failure_does_not_grant_view_eligibility() -> None:
    """验证 Context Item 的显式失败不会被未知错误文本伪装成成功查看。

    Host 返回 ``success=False`` 和尚未加入兼容前缀表的非空 ``path`` 错误时，
    解析器应记录 ``UNKNOWN_FAILURE``，资格存储仍应为 ``MISSING``。该测试防止
    新错误文案被默认分类为成功，从而绕过完整查看授权边界。
    """

    items = [
        build_output_item_call(
            call_id="failed-context-call",
            item_id="failed-context-call-item",
            tool_name="view_forward_message",
            message_id="message-1",
        ),
        build_output_item_result(
            "failed-context-call",
            "查看转发消息工具的 `path` 必须是由非负整数组成的数组。",
            success=False,
            item_id="failed-context-result-item",
        ),
    ]
    items[0]["tool_call"]["args"]["path"] = "invalid-path"

    observations = PlannerHistoryParser.extract_view_observations(items)
    store = ViewEligibilityStore()
    store.sync_context("stream-1", observations)
    lookup = store.lookup("stream-1", "message-1")

    assert len(observations) == 1
    assert observations[0].kind is ViewObservationKind.UNKNOWN_FAILURE
    assert observations[0].path is None
    assert PlannerHistoryParser.extract_view_results(items) == []
    assert lookup.status is ViewEligibilityStatus.MISSING
    assert lookup.last_observation_kind is ViewObservationKind.UNKNOWN_FAILURE
