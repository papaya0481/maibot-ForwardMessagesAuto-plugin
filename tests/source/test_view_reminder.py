"""source Planner 嵌套查看续轮提醒的回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from forward_messages_auto.source.history import VIEW_FORWARD_TOOL_NAME
from forward_messages_auto.source.models import ViewEligibilityStatus
from tests.source.support import build_output_item_call, build_output_item_result
from tests.support import build_plugin


@pytest.mark.asyncio
async def test_nested_reminder_is_optional_until_every_discovered_path_is_viewed(tmp_path: Path) -> None:
    """验证未展开嵌套时使用可选继续查看提醒，路径闭包后才声明完整。

    根层结果先暴露 ``path=[0, 0]``，期望提醒明确允许继续查看或直接判断，
    同时资格仍为 ``READY``。随后把该路径的成功结果加入当前历史，期望提醒
    切换为已审核的完整内容文本。该测试防止嵌套占位再次被误报为完整内容，
    也防止可选查看被实现成强制转发门槛。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离插件运行状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    root_call = build_output_item_call(
        call_id="root-view",
        item_id="root-view-item",
        tool_name=VIEW_FORWARD_TOOL_NAME,
        message_id="nested-message",
    )
    root_result = build_output_item_result(
        "root-view",
        "【外层用户】: [嵌套转发消息，path=[0, 0]，可再次调用 view_forward_message 展开]",
        item_id="root-result-item",
    )

    root_capture = await plugin.capture_view_forward_result(
        session_id="source-stream",
        items=[root_call, root_result],
        item_schema_version=1,
        tool_definitions=[],
    )
    root_reminder = root_capture["modified_kwargs"]["items"][-1]["parts"][0]["text"]
    assert root_reminder == (
        "<system-reminder>\n"
        "刚刚已经查看合并转发消息 msg_id=nested-message 的当前层内容，但其中仍有尚未展开的嵌套转发消息。"
        "如果继续查看有助于判断，建议使用刚看到的内容中给出的 path 再次调用 view_forward_message；"
        "你也可以直接依据当前已看到的内容，判断它是否有意思、符合你的人设并值得分享到其他群聊。"
        "值得时通过 tool_search 发现并调用 request_cross_group_forward，不值得时不要转发。"
        "不要等待下一条聊天消息后再作判断。\n"
        "</system-reminder>"
    )
    root_lookup = plugin.runtime.view_eligibility.lookup("source-stream", "nested-message")
    assert root_lookup.status is ViewEligibilityStatus.READY
    assert root_lookup.content_complete is False

    nested_call = build_output_item_call(
        call_id="nested-view",
        item_id="nested-view-item",
        tool_name=VIEW_FORWARD_TOOL_NAME,
        message_id="nested-message",
        path=[0, 0],
    )
    nested_result = build_output_item_result(
        "nested-view",
        "【内层用户】: 内层正文",
        item_id="nested-result-item",
    )
    complete_capture = await plugin.capture_view_forward_result(
        session_id="source-stream",
        items=[root_call, root_result, nested_call, nested_result],
        item_schema_version=1,
        tool_definitions=[],
    )
    complete_reminder = complete_capture["modified_kwargs"]["items"][-1]["parts"][0]["text"]
    assert complete_reminder == (
        "<system-reminder>\n"
        "刚刚已经成功查看合并转发消息 msg_id=nested-message 的完整内容。"
        "请在本次续轮中优先依据刚看到的完整内容，判断它是否有意思、符合你的人设并值得分享到其他群聊；"
        "值得时通过 tool_search 发现并调用 request_cross_group_forward，不值得时不要转发。"
        "不要等待下一条聊天消息后再作判断。\n"
        "</system-reminder>"
    )
    assert (
        plugin.runtime.view_eligibility.lookup(
            "source-stream",
            "nested-message",
        ).content_complete
        is True
    )
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_mixed_view_reminder_separates_complete_and_nested_message_ids(tmp_path: Path) -> None:
    """验证同轮完整与未完整消息使用已审核的混合提醒文本。

    同一 Planner 历史包含一条无嵌套的成功结果和一条仍暴露 ``path`` 的结果，
    期望提醒分别列出两组消息 ID，并只建议后者按需继续查看。该测试防止批量
    捕获时用任一消息的状态覆盖整批提醒。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离插件运行状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    complete_call = build_output_item_call(
        call_id="complete-view",
        item_id="complete-view-item",
        tool_name=VIEW_FORWARD_TOOL_NAME,
        message_id="complete-message",
    )
    nested_call = build_output_item_call(
        call_id="nested-view",
        item_id="nested-view-item",
        tool_name=VIEW_FORWARD_TOOL_NAME,
        message_id="nested-message",
    )
    captured = await plugin.capture_view_forward_result(
        session_id="source-stream",
        items=[
            complete_call,
            build_output_item_result(
                "complete-view",
                "完整正文",
                item_id="complete-result-item",
            ),
            nested_call,
            build_output_item_result(
                "nested-view",
                "[嵌套转发消息，path=[0, 0]，可再次调用 view_forward_message 展开]",
                item_id="nested-result-item",
            ),
        ],
        item_schema_version=1,
        tool_definitions=[],
    )

    reminder = captured["modified_kwargs"]["items"][-1]["parts"][0]["text"]
    assert reminder == (
        "<system-reminder>\n"
        "刚刚已经成功查看合并转发消息 msg_id=complete-message 的完整内容；"
        "也已经查看合并转发消息 msg_id=nested-message 的当前层内容，但后者仍有尚未展开的嵌套转发消息。"
        "对于仍有嵌套的消息，如果继续查看有助于判断，建议使用刚看到的内容中给出的 path 再次调用 "
        "view_forward_message；你也可以直接依据当前已看到的内容，分别判断它们是否有意思、符合你的人设并值得"
        "分享到其他群聊。值得时通过 tool_search 发现并调用 request_cross_group_forward，不值得时不要转发。"
        "不要等待下一条聊天消息后再作判断。\n"
        "</system-reminder>"
    )
    await plugin.on_unload()
