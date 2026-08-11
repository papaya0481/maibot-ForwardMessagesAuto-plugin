"""最新 MaiBot Context Item Hook 契约的回归测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from forward_messages_auto.source.authorization import (
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
    FORWARD_TOOL_NAME,
)
from forward_messages_auto.source.history import VIEW_FORWARD_TOOL_NAME
from tests.source.support import (
    build_output_item_assistant,
    build_output_item_call,
    build_output_item_result,
)
from tests.support import build_plugin, seed_successful_view, wait_for_background_tasks


@pytest.mark.asyncio
async def test_latest_output_items_authorize_and_deliver_path_a(tmp_path: Path) -> None:
    """验证最新 output_items 契约能够完成路径 A 的授权和实际投递。

    测试使用 ``FunctionCallItem`` 模拟当前 MaiBot Planner 输出，并预置同一
    session 的成功查看资格。EARLY 应在嵌套 ``tool_call.args`` 中签发凭据，
    LATE 应保留精确绑定，正式 Tool 随后应进入真实 target 投递；该场景防止
    最新 Host 载荷再次退化为缺少可信 Planner 会话授权。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离状态文件和投递任务。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    seed_successful_view(plugin)

    early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        output_items=[build_output_item_call()],
    )
    early_item = early["modified_kwargs"]["output_items"][0]
    early_arguments = early_item["tool_call"]["args"]
    assert FORWARD_CONTEXT_TOKEN_ARGUMENT in early_arguments
    assert "group_id" not in early_arguments

    late = await plugin.orchestrate_view_before_forward(**early["modified_kwargs"])
    final_item = late["modified_kwargs"]["output_items"][0]
    final_arguments = final_item["tool_call"]["args"]
    assert final_item["tool_call"]["func_name"] == FORWARD_TOOL_NAME
    assert final_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] == early_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT]

    result = await plugin.request_cross_group_forward(
        **final_arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is True
    await wait_for_background_tasks(plugin)
    assert plugin.ctx.events == [
        ("send", "target-a"),
        ("planner", "target-a"),
        ("send", "target-b"),
        ("planner", "target-b"),
    ]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_latest_output_items_path_b_captures_and_restores_request(tmp_path: Path) -> None:
    """验证最新 output_items 契约能够完成路径 B 的查看、续接和投递。

    测试先让 LATE 把单独转发调用替换为系统查看 Item，再把精确的
    ``FunctionCallItem``/``FunctionCallOutputItem`` 历史交给 before_request，
    最后模拟无工具的桥接轮并确认恢复调用使用当前轮逻辑元数据和全新授权。
    该场景防止查看结果已在新 Context Item 历史中可见却无法恢复原请求。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离 pending 和投递状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()

    first_early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        output_items=[build_output_item_call()],
    )
    first = await plugin.orchestrate_view_before_forward(**first_early["modified_kwargs"])
    view_item = first["modified_kwargs"]["output_items"][0]
    assert view_item["tool_call"]["func_name"] == VIEW_FORWARD_TOOL_NAME
    view_call_id = view_item["tool_call"]["call_id"]

    history = [
        deepcopy(view_item),
        build_output_item_result(view_call_id, "完整展开内容"),
    ]
    before = await plugin.capture_view_forward_result(
        session_id="source-stream",
        items=history,
        item_schema_version=1,
        tool_definitions=[],
    )
    assert before["modified_kwargs"]["items"] == history

    bridge_early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        output_items=[build_output_item_assistant("模型桥接轮输出")],
    )
    resumed = await plugin.orchestrate_view_before_forward(**bridge_early["modified_kwargs"])
    restored_item = next(
        item for item in resumed["modified_kwargs"]["output_items"] if item.get("item_type") == "FunctionCallItem"
    )
    restored_call = restored_item["tool_call"]
    restored_arguments = restored_call["args"]
    assert restored_call["func_name"] == FORWARD_TOOL_NAME
    assert restored_call["call_id"].startswith("cross-forward-auto-request-")
    assert restored_item["meta"]["logical_turn_id"] == "output-bridge-turn"
    assert restored_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT]
    assert restored_arguments["msg_id"] == "forward-message"

    result = await plugin.request_cross_group_forward(
        **restored_arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is True
    await wait_for_background_tasks(plugin)
    assert plugin.ctx.events == [
        ("send", "target-a"),
        ("planner", "target-a"),
        ("send", "target-b"),
        ("planner", "target-b"),
    ]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_latest_output_items_capture_manual_view_adds_existing_reminder(tmp_path: Path) -> None:
    """验证最新 ``items`` 历史中的主动查看仍会追加既有提醒，防止提醒丢失回归。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离状态文件和查看资格。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    view_call = build_output_item_call(
        call_id="manual-view",
        item_id="manual-view-item",
        tool_name=VIEW_FORWARD_TOOL_NAME,
        message_id="manual-message",
    )
    view_result = build_output_item_result(
        "manual-view",
        "完整转发内容",
        tool_name=VIEW_FORWARD_TOOL_NAME,
    )

    captured = await plugin.capture_view_forward_result(
        session_id="source-stream",
        items=[view_call, view_result],
        item_schema_version=1,
        tool_definitions=[],
    )

    reminder = captured["modified_kwargs"]["items"][-1]
    assert reminder["item_type"] == "UserMessageItem"
    assert "manual-message" in reminder["parts"][0]["text"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_latest_output_items_keep_mixed_tool_order_and_clean_only_forward_args(tmp_path: Path) -> None:
    """验证最新混合工具输出只清洗转发参数而不重排其他调用。

    EARLY 和 LATE 收到一个转发调用与一个普通调用的同批输出时，应保留
    Item 顺序和普通调用内容，只为转发调用签发并验证一次性凭据。该场景防止
    output_items 适配器把旧版单工具路径 B 错误应用到混合工具批次。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离授权表。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    original_items = [
        build_output_item_call(call_id="forward-call"),
        build_output_item_call(
            call_id="reply-call",
            tool_name="reply",
            message_id="reply-message",
            sharing_reason="",
            content_summary="",
            item_id="reply-item",
        ),
    ]

    early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        output_items=deepcopy(original_items),
    )
    late = await plugin.orchestrate_view_before_forward(**early["modified_kwargs"])
    final_items = late["modified_kwargs"]["output_items"]
    assert [item["tool_call"]["call_id"] for item in final_items] == [
        "forward-call",
        "reply-call",
    ]
    assert final_items[1] == original_items[1]
    assert FORWARD_CONTEXT_TOKEN_ARGUMENT in final_items[0]["tool_call"]["args"]
    assert "group_id" not in final_items[0]["tool_call"]["args"]
    await plugin.on_unload()
