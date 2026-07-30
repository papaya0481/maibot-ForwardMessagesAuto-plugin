"""路径 B 自动查看与转发续接的回归测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from forward_messages_auto.invocation_gate import (
    FORWARD_AUTHORIZATION_ROUND_KWARG,
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
)
from forward_messages_auto.parsing import VIEW_FORWARD_TOOL_NAME
from forward_messages_auto.view_before_forward import FORWARD_TOOL_NAME
from tests.support import (
    build_forward_message,
    build_plugin,
    invoke_forward_tool,
    seed_successful_view,
    wait_for_background_tasks,
)


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
async def test_pending_restore_revokes_discarded_early_call(tmp_path: Path) -> None:
    """验证路径 B 覆盖桥接轮模型调用时同步撤销其一次性凭据。

    系统查看成功后的桥接轮模拟模型又生成一个转发调用。LATE 必须丢弃该调用、
    撤销 EARLY 为它签发的凭据，并只恢复最初请求；被丢弃参数不能重放，恢复
    调用仍应正常完成投递。该测试防止编排覆盖留下可用的孤立授权。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离 pending 和投递状态。
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
    messages = build_view_history(view_call, "完整展开内容")
    await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=[],
    )
    bridge_early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="桥接轮模型输出",
        tool_calls=[build_forward_call(call_id="discarded-call")],
    )
    discarded_arguments = deepcopy(bridge_early["modified_kwargs"]["tool_calls"][0]["function"]["arguments"])

    resumed = await plugin.orchestrate_view_before_forward(
        **bridge_early["modified_kwargs"],
    )
    restored = resumed["modified_kwargs"]["tool_calls"][0]
    discarded = await plugin.request_cross_group_forward(
        **discarded_arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    result = await plugin.request_cross_group_forward(
        **restored["function"]["arguments"],
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert discarded["success"] is False
    assert "可信的 Planner 会话授权" in discarded["content"]
    assert result["success"] is True
    await wait_for_background_tasks(plugin)
    assert plugin.ctx.events[:2] == [("send", "target-a"), ("planner", "target-a")]
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
async def test_early_authorization_keeps_late_hook_failure_fail_closed(tmp_path: Path) -> None:
    """验证跳过异步编排 Hook 后仍不能伪造 source 上下文。

    测试只执行 EARLY 清洗授权，模拟后续自动查看 Hook 超时或异常而未修改
    调用。模型伪造的白名单群和聊天流字段必须已被删除；正式 handler 应按
    凭据绑定的真实非 source 会话拒绝请求，且在读取消息前停止。该测试防止
    参数清洗与异步预检共用失败边界而重新变成 fail-open。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于确认没有消息读取或投递。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.chat.streams.append({"platform": "qq", "group_id": "99999", "stream_id": "other-stream"})
    await plugin.on_load()
    call = build_forward_call()
    call["function"]["arguments"].update(
        {
            "platform": "qq",
            "group_id": "10001",
            "stream_id": "source-stream",
        }
    )

    early_result = await plugin.authorize_forward_request_context(
        session_id="other-stream",
        response="尝试越权",
        tool_calls=[call],
    )
    authorized_call = early_result["modified_kwargs"]["tool_calls"][0]
    assert public_forward_arguments(authorized_call) == {
        "msg_id": "forward-message",
        "sharing_reason": "预览看起来很有意思",
        "content_summary": "预览摘要",
    }

    result = await plugin.request_cross_group_forward(
        **authorized_call["function"]["arguments"],
        platform="qq",
        group_id="99999",
        stream_id="other-stream",
    )
    assert result["success"] is False
    assert "source 白名单" in result["content"]
    assert plugin.ctx.message.calls == []
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_handler_rejects_missing_and_replayed_authorization(tmp_path: Path) -> None:
    """验证正式 handler 缺少或重复使用一次性凭据时拒绝执行。

    未经 EARLY Hook 的原始调用即使伪造完整 Host 上下文也必须失败。合法凭据
    首次使用可进入现有查看资格校验，但同一参数再次调用必须因凭据已消费而
    失败；该测试防止 Hook 故障绕过授权及历史中的内部参数被重放。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离一次性授权状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    missing = await plugin.request_cross_group_forward(
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert missing["success"] is False
    assert "可信的 Planner 会话授权" in missing["content"]

    early_result = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="合法请求",
        tool_calls=[build_forward_call()],
    )
    arguments = early_result["modified_kwargs"]["tool_calls"][0]["function"]["arguments"]
    first = await plugin.request_cross_group_forward(
        **arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    replay = await plugin.request_cross_group_forward(
        **arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert first["success"] is False
    assert "当前 Planner 上下文中尚无成功" in first["content"]
    assert replay["success"] is False
    assert "可信的 Planner 会话授权" in replay["content"]
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_early_hook_rotates_stale_authorization(tmp_path: Path) -> None:
    """验证新响应轮会撤销输入参数实际回放的旧凭据。

    第二次 EARLY Hook 即使收到带旧内部字段的历史参数，也必须签发不同的新
    凭据；旧参数随后应被正式 handler 拒绝，新参数仍能进入查看资格校验。
    该测试防止模型或历史消息跨响应轮重放尚未消费的授权。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离一次性授权状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    first_early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="首次请求",
        tool_calls=[build_forward_call()],
    )
    first_arguments = deepcopy(first_early["modified_kwargs"]["tool_calls"][0]["function"]["arguments"])
    second_call = build_forward_call(call_id="replayed-call")
    second_call["function"]["arguments"] = deepcopy(first_arguments)
    second_early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="重放请求",
        tool_calls=[second_call],
    )
    second_arguments = second_early["modified_kwargs"]["tool_calls"][0]["function"]["arguments"]

    assert second_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] != first_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT]
    stale = await plugin.request_cross_group_forward(
        **first_arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    current = await plugin.request_cross_group_forward(
        **second_arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert stale["success"] is False
    assert "可信的 Planner 会话授权" in stale["content"]
    assert current["success"] is False
    assert "当前 Planner 上下文中尚无成功" in current["content"]
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_same_session_empty_early_keeps_path_a_authorization(tmp_path: Path) -> None:
    """验证同 session 的辅助响应不会撤销路径 A 正在执行的凭据。

    主 Planner 已有成功查看资格并经过 EARLY 签发后，模拟 reply 等工具触发
    同 session、无工具调用的辅助 Planner 响应。主调用随后经过 LATE 和正式
    handler 仍应完成投递；该测试防止并发辅助请求随机撤销合法主调用，也覆盖
    路径 A 从两个 Hook 到目标投递的端到端行为。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于隔离查看和投递状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    seed_successful_view(plugin)
    main_early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="路径 A 请求",
        tool_calls=[build_forward_call()],
    )
    await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="辅助响应",
        tool_calls=[],
    )

    main_late = await plugin.orchestrate_view_before_forward(
        **main_early["modified_kwargs"],
    )
    retained = main_late["modified_kwargs"]["tool_calls"][0]
    assert retained["function"]["name"] == FORWARD_TOOL_NAME
    result = await plugin.request_cross_group_forward(
        **retained["function"]["arguments"],
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
async def test_late_hook_does_not_rebind_authorization_after_session_change(tmp_path: Path) -> None:
    """验证 EARLY 与 LATE 之间的 session 变化不会改绑转发凭据。

    EARLY 在 source 会话签发凭据后，模拟其他 Hook 把 LATE 的 session 改成
    另一聊天流。LATE 必须删除内部凭据且不触发自动查看，正式 handler 随后
    安全拒绝。该测试防止异步编排阶段把旧授权重新绑定到错误会话。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于确认没有查看或投递副作用。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="源群请求",
        tool_calls=[build_forward_call()],
    )
    late_kwargs = deepcopy(early["modified_kwargs"])
    late_kwargs["session_id"] = "other-stream"

    late = await plugin.orchestrate_view_before_forward(**late_kwargs)
    retained = late["modified_kwargs"]["tool_calls"][0]
    retained_arguments = retained["function"]["arguments"]
    assert retained["function"]["name"] == FORWARD_TOOL_NAME
    assert FORWARD_CONTEXT_TOKEN_ARGUMENT not in retained_arguments
    result = await plugin.request_cross_group_forward(
        **retained_arguments,
        platform="qq",
        group_id="99999",
        stream_id="other-stream",
    )
    assert result["success"] is False
    assert "可信的 Planner 会话授权" in result["content"]
    assert plugin.ctx.message.calls == []
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_round", "changed_message"])
async def test_late_hook_rejects_missing_round_or_changed_message(
    tmp_path: Path,
    mutation: str,
) -> None:
    """验证 LATE 只接受本次 EARLY 分发链的精确调用绑定。

    分别删除 EARLY 传递的本轮随机标记，或在两个 Hook 之间改写 ``msg_id``；
    LATE 都必须移除内部凭据且不触发自动查看，正式 handler 随后安全拒绝。
    该测试防止历史遗留凭据在 EARLY 缺席时被接纳，或被改绑到另一消息。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于确认没有查看或投递副作用。
        mutation: 要模拟的 Hook 链异常，取 ``missing_round`` 或
            ``changed_message``。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="源群请求",
        tool_calls=[build_forward_call()],
    )
    late_kwargs = deepcopy(early["modified_kwargs"])
    if mutation == "missing_round":
        late_kwargs.pop(FORWARD_AUTHORIZATION_ROUND_KWARG)
    else:
        late_kwargs["tool_calls"][0]["function"]["arguments"]["msg_id"] = "changed-message"

    late = await plugin.orchestrate_view_before_forward(**late_kwargs)
    retained = late["modified_kwargs"]["tool_calls"][0]
    retained_arguments = retained["function"]["arguments"]
    assert retained["function"]["name"] == FORWARD_TOOL_NAME
    assert FORWARD_CONTEXT_TOKEN_ARGUMENT not in retained_arguments
    result = await plugin.request_cross_group_forward(
        **retained_arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is False
    assert "可信的 Planner 会话授权" in result["content"]
    assert plugin.ctx.message.calls == []
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_late_hook_does_not_restore_authorization_cleared_by_config_update(
    tmp_path: Path,
) -> None:
    """验证配置更新清除的 EARLY 凭据不会被 LATE 重新签发。

    测试在两个 Hook 之间执行配置热更新，再把原 EARLY 结果交给 LATE。调用
    必须保留为无授权的转发请求且不触发自动查看，正式 handler 只能拒绝。
    该测试防止旧配置下的调用跨越新路由或启停边界复活。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于加载并热更新插件状态。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    early = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        response="更新前请求",
        tool_calls=[build_forward_call()],
    )
    await plugin.on_config_update(
        scope="plugin",
        config_data={},
        version="test-update",
    )

    late = await plugin.orchestrate_view_before_forward(**early["modified_kwargs"])
    retained = late["modified_kwargs"]["tool_calls"][0]
    retained_arguments = retained["function"]["arguments"]
    assert retained["function"]["name"] == FORWARD_TOOL_NAME
    assert FORWARD_CONTEXT_TOKEN_ARGUMENT not in retained_arguments
    result = await plugin.request_cross_group_forward(
        **retained_arguments,
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is False
    assert "可信的 Planner 会话授权" in result["content"]
    assert plugin.ctx.message.calls == []
    assert plugin.ctx.events == []
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
async def test_multi_tool_response_is_not_reordered(tmp_path: Path) -> None:
    """验证混合工具批次不被路径 B 删除、插入或重排。

    转发调用和 ``reply`` 同时出现时应保持顺序，只清除转发调用中伪造的
    上下文字段，让未查看请求继续由现有 Tool handler 拒绝；该测试防止自动
    编排改变其他工具的执行语义或保留不可信调用上下文。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于加载协调器。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    tool_calls = [
        build_forward_call(),
        {
            "id": "reply-call",
            "function": {
                "name": "reply",
                "arguments": {"message_id": "some-message"},
            },
        },
    ]
    tool_calls[0]["function"]["arguments"]["group_id"] = "伪造群号"

    result = await run_after_response_hooks(
        plugin,
        session_id="source-stream",
        response="混合调用",
        tool_calls=deepcopy(tool_calls),
    )
    assert result["modified_kwargs"]["response"] == "混合调用"
    assert [call["function"]["name"] for call in result["modified_kwargs"]["tool_calls"]] == [
        FORWARD_TOOL_NAME,
        "reply",
    ]
    assert public_forward_arguments(result["modified_kwargs"]["tool_calls"][0]) == {
        "msg_id": "forward-message",
        "sharing_reason": "预览看起来很有意思",
        "content_summary": "预览摘要",
    }
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
