"""路径 B 自动查看与转发续接的回归测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from forward_messages_auto.source.authorization import (
    FORWARD_AUTHORIZATION_ROUND_KWARG,
)
from forward_messages_auto.source.history import VIEW_FORWARD_TOOL_NAME
from forward_messages_auto.source.authorization import FORWARD_TOOL_NAME
from tests.support import (
    build_forward_message,
    build_plugin,
    invoke_forward_tool,
    seed_successful_view,
    wait_for_background_tasks,
)
from tests.source.support import (
    build_forward_call,
    build_view_history,
    public_forward_arguments,
    run_after_response_hooks,
)


@pytest.mark.asyncio
async def test_unviewed_direct_request_is_replaced_by_single_real_view(tmp_path: Path) -> None:
    """验证未查看的单独转发调用只会先执行一次真实查看。

    期望 Hook 用唯一 ``view_forward_message`` 替换原调用，保留其他 Hook
    字段且不产生发送事件；该测试防止把查看和转发放进同一工具批次，导致
    转发处理器仍读取到旧的缺失资格。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离插件运行状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    original_call = build_forward_call()

    result = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="现在转发",
        tool_calls=[deepcopy(original_call)],
        prompt_tokens=17,
    )

    modified = result["modified_kwargs"]
    assert modified["response"] == "转发前先查看这则合并转发的完整内容。"
    assert modified["prompt_tokens"] == 17
    assert FORWARD_AUTHORIZATION_ROUND_KWARG not in modified
    assert len(modified["tool_calls"]) == 1
    view_call = modified["tool_calls"][0]
    assert view_call["id"].startswith("cross-forward-auto-view-")
    assert view_call["function"] == {
        "name": VIEW_FORWARD_TOOL_NAME,
        "arguments": {"msg_id": "forward-message"},
    }
    assert original_call["function"]["name"] == FORWARD_TOOL_NAME
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_successful_auto_view_restores_original_request_and_sends(tmp_path: Path) -> None:
    """验证路径 B 成功查看后恢复完整原参数并进入既有投递流程。

    自动查看的成功结果不应生成路径 A 的再次判断提醒。下一 ``after_response``
    必须忽略模型本轮输出，只恢复一个全新 ID 的原转发调用；执行该调用后应
    按现有顺序完成两个 target，且 ToolResult 对账后不再重复恢复。未声明的
    群聊字段不得覆盖 Host 执行上下文；该测试防止参数丢失、上下文伪造、
    重复判断以及重复物理发送。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离投递和防重状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    original_call = build_forward_call()
    original_call["function"]["arguments"]["group_id"] = "伪造群号"
    first = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="现在转发",
        tool_calls=[deepcopy(original_call)],
    )
    view_call = first["modified_kwargs"]["tool_calls"][0]
    messages = build_view_history(view_call, "完整展开内容")

    before = await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )
    assert before["modified_kwargs"]["messages"] == messages

    resumed = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="模型桥接轮输出不会成为最终行动",
        tool_calls=[],
    )
    restored_call = resumed["modified_kwargs"]["tool_calls"][0]
    assert resumed["modified_kwargs"]["response"] == "已完成转发前查看，继续执行原转发请求。"
    assert restored_call["id"].startswith("cross-forward-auto-request-")
    assert restored_call["id"] != original_call["id"]
    assert restored_call["function"]["name"] == FORWARD_TOOL_NAME
    assert public_forward_arguments(restored_call) == {
        "msg_id": "forward-message",
        "sharing_reason": "预览看起来很有意思",
        "content_summary": "预览摘要",
    }

    arguments = restored_call["function"]["arguments"]
    delivery_result = await plugin.request_cross_group_forward(
        **arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert delivery_result["success"] is True
    await wait_for_background_tasks(plugin)
    assert plugin.ctx.events == [
        ("send", "target-a"),
        ("planner", "target-a"),
        ("send", "target-b"),
        ("planner", "target-b"),
    ]

    messages.extend(
        [
            {"role": "assistant", "content": "", "tool_calls": [restored_call]},
            {
                "role": "tool",
                "content": "跨群转发已完成",
                "tool_call_id": restored_call["id"],
            },
        ]
    )
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )
    settled = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="后续普通响应",
        tool_calls=[],
    )
    assert settled["modified_kwargs"]["tool_calls"] == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_existing_successful_view_keeps_path_a_call_unchanged(tmp_path: Path) -> None:
    """验证当前上下文已有成功查看时继续沿用路径 A。

    期望 Hook 不修改模型响应、原调用 ID 或参数；该测试防止新增路径 B 对
    已经完成查看的正常调用重复展开或改变供应商附加字段。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离插件运行状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    call = build_forward_call()
    call["function"]["arguments"].update(
        {
            "platform": "qq",
            "group_id": "伪造群号",
            "stream_id": "伪造聊天流",
        }
    )
    call["extra_content"] = {"thought_signature": "保留供应商字段"}
    manual_view_call = {
        "id": "manual-view",
        "function": {
            "name": VIEW_FORWARD_TOOL_NAME,
            "arguments": {"msg_id": "forward-message"},
        },
    }
    messages = build_view_history(manual_view_call, "人工查看的完整内容")
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )

    result = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="按路径 A 转发",
        tool_calls=[deepcopy(call)],
    )
    assert result["modified_kwargs"]["response"] == "按路径 A 转发"
    sanitized_call = result["modified_kwargs"]["tool_calls"][0]
    assert sanitized_call["id"] == call["id"]
    assert sanitized_call["extra_content"] == call["extra_content"]
    assert public_forward_arguments(sanitized_call) == {
        "msg_id": "forward-message",
        "sharing_reason": "预览看起来很有意思",
        "content_summary": "预览摘要",
    }
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_auto_view_preflight_rejects_non_source_session(tmp_path: Path) -> None:
    """验证非 source 聊天流不能借直接请求触发系统查看。

    预检必须按 Hook 的真实 ``session_id`` 从 Host 群聊流反查群号；即使模型
    参数伪造白名单群和 QQ 平台，调用也应保持为清洗后的原转发请求，最终由
    正式处理器拒绝。该测试防止路径 B 在权限校验前展开完整内容。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离插件运行状态。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.message.message = build_forward_message(
        stream_id="other-stream",
        group_id="99999",
    )
    plugin.ctx.chat.streams.append({"platform": "qq", "group_id": "99999", "stream_id": "other-stream"})
    await plugin.on_load()
    call = build_forward_call()
    call["function"]["arguments"].update(
        {
            "platform": "qq",
            "group_id": "10001",
            "stream_id": "other-stream",
        }
    )

    result = await run_after_response_hooks(
        plugin,
        session_id="other-stream",
        response="尝试越权转发",
        tool_calls=[call],
    )
    retained = result["modified_kwargs"]["tool_calls"][0]
    assert retained["function"]["name"] == FORWARD_TOOL_NAME
    assert public_forward_arguments(retained) == {
        "msg_id": "forward-message",
        "sharing_reason": "预览看起来很有意思",
        "content_summary": "预览摘要",
    }
    assert plugin.ctx.message.calls == []
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_auto_view_preflight_rejects_invalid_message_and_empty_targets(tmp_path: Path) -> None:
    """验证消息类型或 target 配置无效时不会先执行完整查看。

    非合并转发消息和没有可用 target 的请求都应保持为正式处理器可见的原
    转发调用，不得改写成 ``view_forward_message``；该测试防止无效任务产生
    不必要的完整展开或媒体分析。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于构造两组独立插件状态。
    """

    invalid_message_plugin = build_plugin(tmp_path / "invalid")
    invalid_message_plugin.ctx.message.message["raw_message"] = [{"type": "text", "data": "普通消息"}]
    await invalid_message_plugin.on_load()
    invalid_result = await run_after_response_hooks(
        invalid_message_plugin,
        session_id="source-stream",
        response="无效消息",
        tool_calls=[build_forward_call()],
    )
    assert invalid_result["modified_kwargs"]["tool_calls"][0]["function"]["name"] == FORWARD_TOOL_NAME
    await invalid_message_plugin.on_unload()

    no_target_plugin = build_plugin(tmp_path / "no-target", target_groups=[])
    await no_target_plugin.on_load()
    no_target_result = await run_after_response_hooks(
        no_target_plugin,
        session_id="source-stream",
        response="没有目标",
        tool_calls=[build_forward_call()],
    )
    assert no_target_result["modified_kwargs"]["tool_calls"][0]["function"]["name"] == FORWARD_TOOL_NAME
    await no_target_plugin.on_unload()


@pytest.mark.asyncio
async def test_auto_view_uses_session_mapping_instead_of_message_origin_group(tmp_path: Path) -> None:
    """验证路径 B 不把消息记录的原始来源群误当作当前 source 群。

    消息属于可信 ``source-stream``，但消息元数据群号模拟为转发内容原始来源
    群。预检应通过 Host 群聊流映射得到白名单群 ``10001`` 并执行真实查看；
    该测试防止安全加固重新引入“消息来源群必须等于 source 群”的旧限制。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离自动查看状态。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.message.message = build_forward_message(
        stream_id="source-stream",
        group_id="original-group",
    )
    await plugin.on_load()

    result = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="分享外群转来的内容",
        tool_calls=[build_forward_call()],
    )
    assert result["modified_kwargs"]["tool_calls"][0]["function"]["name"] == VIEW_FORWARD_TOOL_NAME
    assert plugin.ctx.message.calls == [("forward-message", "source-stream", False)]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_completed_request_does_not_repeat_auto_view(tmp_path: Path) -> None:
    """验证永久状态已完成的重复请求不会再次展开同一消息。

    首次路径 A 完成全部 target 后清除当前查看资格，再次直接请求应由预检
    识别 durable duplicate 并保留原调用，而不是触发新的自动查看。该测试
    防止路由未变化时重复消耗查看和媒体分析。

    Args:
        tmp_path: pytest 提供的临时目录，用于持久化已完成 target 阶段。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    seed_successful_view(plugin)
    completed = await invoke_forward_tool(
        plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert completed["success"] is True
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=[],
        tool_definitions=[],
    )
    previous_events = list(plugin.ctx.events)

    repeated = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="重复请求",
        tool_calls=[build_forward_call()],
    )
    assert repeated["modified_kwargs"]["tool_calls"][0]["function"]["name"] == FORWARD_TOOL_NAME
    assert plugin.ctx.events == previous_events
    await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_content",
    [
        "查看转发消息完整内容时发生异常。",
        "",
    ],
)
async def test_auto_view_retries_until_existing_fallback_boundary(
    tmp_path: Path,
    failure_content: str,
) -> None:
    """验证自动查看沿用可重试故障和空内容的既有两次边界。

    首次失败后必须再生成一个查看调用，第二次连续同类失败后才恢复原请求；
    处理器随后使用原 ``content_summary`` 完成转发。该测试防止自动路径首次
    失败就降级，或达到阈值后仍无限消耗 Planner 内部轮次。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离投递状态。
        failure_content: 两次查看返回的同类失败文本；空串模拟空内容。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    first = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="直接请求",
        tool_calls=[build_forward_call(content_summary="自动查看失败后的摘要")],
    )
    first_view = first["modified_kwargs"]["tool_calls"][0]
    messages = build_view_history(first_view, failure_content)
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )

    retry = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="桥接",
        tool_calls=[],
    )
    second_view = retry["modified_kwargs"]["tool_calls"][0]
    assert second_view["function"]["name"] == VIEW_FORWARD_TOOL_NAME
    assert second_view["id"] != first_view["id"]

    messages = build_view_history(
        second_view,
        failure_content,
        previous_messages=messages,
    )
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )
    resumed = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="第二次桥接",
        tool_calls=[],
    )
    restored = resumed["modified_kwargs"]["tool_calls"][0]
    assert restored["function"]["name"] == FORWARD_TOOL_NAME

    result = await plugin.request_cross_group_forward(
        **restored["function"]["arguments"],
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is True
    assert plugin.ctx.events[:2] == [("send", "target-a"), ("planner", "target-a")]
    await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_content",
    [
        "查看转发消息工具需要提供有效的 `msg_id` 参数。",
        "目标消息不是可展开查看的转发消息，msg_id=forward-message",
        "工具 view_forward_message 执行失败。",
    ],
)
async def test_non_retryable_auto_view_failure_never_sends(
    tmp_path: Path,
    failure_content: str,
) -> None:
    """验证不可自动重试的查看失败仍由最终处理器安全拒绝。

    协调器会恢复原请求以取得现有分类错误，但处理器不得创建投递或产生任何
    target 事件；该测试防止参数、消息类型或未知故障被路径 B 绕过。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于确认没有写入投递事件。
        failure_content: 可修正、终止或未知失败的稳定 Host 文本。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    first = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="直接请求",
        tool_calls=[build_forward_call()],
    )
    view_call = first["modified_kwargs"]["tool_calls"][0]
    messages = build_view_history(view_call, failure_content)
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )
    resumed = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="桥接",
        tool_calls=[],
    )
    restored = resumed["modified_kwargs"]["tool_calls"][0]

    result = await plugin.request_cross_group_forward(
        **restored["function"]["arguments"],
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is False
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_missing_auto_view_result_abandons_resume(tmp_path: Path) -> None:
    """验证系统查看结果不在当前上下文时不会恢复转发请求。

    在 pending 已建立但下一 ``before_request`` 看不到精确调用结果时，后续
    响应必须保持空工具列表且不发送；该测试防止上下文裁剪或 Host 结果缺失
    后仅凭进程内参数越过查看资格。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于确认没有 target 副作用。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="直接请求",
        tool_calls=[build_forward_call()],
    )
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=[],
        tool_definitions=[],
    )

    result = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="结果缺失",
        tool_calls=[],
    )
    assert result["modified_kwargs"]["response"] == "结果缺失"
    assert result["modified_kwargs"]["tool_calls"] == []
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_missing_restored_result_releases_session_for_new_request(tmp_path: Path) -> None:
    """验证恢复调用结果被裁剪后不会永久占用该 Planner 会话。

    自动查看成功并恢复转发调用后，下一 ``before_request`` 即使看不到该调用
    的 ToolResult，也应安全释放旧 pending。随后新的未查看请求必须重新进入
    自动查看，而不是因旧 ``restored_call_id`` 被永久放行。该测试防止路径 B
    在一次历史裁剪后对整个会话失效。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离协调状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    first = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="首次请求",
        tool_calls=[build_forward_call()],
    )
    view_call = first["modified_kwargs"]["tool_calls"][0]
    messages = build_view_history(view_call, "完整展开内容")
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )
    restored = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="恢复请求",
        tool_calls=[],
    )
    assert restored["modified_kwargs"]["tool_calls"][0]["function"]["name"] == FORWARD_TOOL_NAME

    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=[],
        tool_definitions=[],
    )
    next_request = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="新的请求",
        tool_calls=[build_forward_call(call_id="new-request")],
    )
    assert next_request["modified_kwargs"]["tool_calls"][0]["function"]["name"] == VIEW_FORWARD_TOOL_NAME
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_config_update_cancels_unfinished_direct_request(tmp_path: Path) -> None:
    """验证配置热更新会取消尚未发送的旧路径 B 授权。

    直接请求被替换为查看后模拟一次配置更新，再把旧查看成功结果放入上下文；
    后续普通响应不得自动恢复原转发调用。该测试防止禁用、路由调整或重新启用
    后，更新前保存的分享意图在无新授权的情况下复活。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于加载插件和 pending 状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    first = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="更新前请求",
        tool_calls=[build_forward_call()],
    )
    view_call = first["modified_kwargs"]["tool_calls"][0]
    await plugin.on_config_update(
        scope="plugin",
        config_data={},
        version="test-update",
    )
    messages = build_view_history(view_call, "更新后才返回的旧查看结果")
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )

    result = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="更新后的普通响应",
        tool_calls=[],
    )
    assert result["modified_kwargs"]["tool_calls"] == []
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_auto_view_pending_is_isolated_by_session(tmp_path: Path) -> None:
    """验证不同 Planner 聊天流可以独立维护直接请求续接状态。

    source A 建立 pending 后，source B 的普通响应不得恢复、清理或继承 A 的
    参数；该测试防止相同 ``msg_id`` 在不同聊天流间串联或误转发。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于加载进程内协调状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    first = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="A 请求",
        tool_calls=[build_forward_call()],
    )
    assert first["modified_kwargs"]["tool_calls"][0]["function"]["name"] == VIEW_FORWARD_TOOL_NAME

    other = await run_after_response_hooks(
        plugin,
        session_id="another-stream",
        response="B 普通响应",
        tool_calls=[],
    )
    assert other["modified_kwargs"]["response"] == "B 普通响应"
    assert other["modified_kwargs"]["tool_calls"] == []
    await plugin.on_unload()
