"""source 一次性授权与调用清洗的回归测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from forward_messages_auto.source.authorization import (
    FORWARD_AUTHORIZATION_ROUND_KWARG,
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
)
from forward_messages_auto.source.authorization import FORWARD_TOOL_NAME
from tests.support import (
    build_plugin,
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
