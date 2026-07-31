"""转发请求校验与 source 消息读取的回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from forward_messages_auto.source.models import ViewObservationKind
from tests.support import (
    FakeMessageCapability,
    FakeStaticMessageCapability,
    build_forward_message,
    build_plugin,
    invoke_forward_tool,
    seed_successful_view,
    sync_failed_views,
    wait_for_background_tasks,
)


@pytest.mark.asyncio
async def test_tool_rejects_unviewed_and_single_failure_before_fallback(tmp_path: Path) -> None:
    """验证缺少成功查看时会要求紧接续轮重试且不会静默降级转发。

    未查看时即使提供摘要也应拒绝任务；登记一次已知失败后仍应要求 Planner
    在当前 ToolResult 后的紧接续轮再次查看，并明确禁止等待新消息或使用摘要
    绕过。第二个独立查看调用也失败后，才允许使用摘要创建转发任务。该测试
    防止暂时性缺失绕过完整查看要求或把重试错误推迟到下一条聊天消息。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于运行真实请求和后台投递。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()

    missing_result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="不应立即使用的摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert missing_result["success"] is False
    assert "当前 Planner 上下文中尚无成功" in missing_result["content"]
    assert 'view_forward_message(msg_id="forward-message")' in missing_result["content"]
    assert "收到本工具结果后的紧接续轮" in missing_result["content"]
    assert "不要等待新的聊天消息" in missing_result["content"]
    assert "不要使用 content_summary 绕过查看" in missing_result["content"]
    assert plugin.ctx.events == []

    sync_failed_views(plugin, [ViewObservationKind.RETRYABLE_FAILURE])
    first_failure_result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="仍不应使用的摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert first_failure_result["success"] is False
    assert "配置阈值 2 次" in first_failure_result["content"]
    assert "收到本工具结果后的紧接续轮" in first_failure_result["content"]
    assert plugin.ctx.events == []

    sync_failed_views(
        plugin,
        [
            ViewObservationKind.RETRYABLE_FAILURE,
            ViewObservationKind.RETRYABLE_FAILURE,
        ],
    )
    fallback_result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="两次失败后的降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert fallback_result["success"] is True
    assert fallback_result["accepted"] is True
    await wait_for_background_tasks(plugin)
    assert plugin.ctx.events[:2] == [("send", "target-a"), ("planner", "target-a")]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_tool_uses_configured_view_failure_fallback_threshold(tmp_path: Path) -> None:
    """验证自定义连续失败阈值会实时控制何时允许降级。

    将阈值配置为三次后，前两次查看失败仍应拒绝转发并报告当前阈值；第三次
    失败后才接受摘要降级。该测试防止请求服务重新使用硬编码次数。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于运行真实请求和后台投递。
    """

    plugin = build_plugin(tmp_path, view_failure_fallback_threshold=3)
    await plugin.on_load()
    sync_failed_views(
        plugin,
        [
            ViewObservationKind.RETRYABLE_FAILURE,
            ViewObservationKind.RETRYABLE_FAILURE,
        ],
    )

    before_threshold = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="两次失败时不应使用的摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert before_threshold["success"] is False
    assert "配置阈值 3 次" in before_threshold["content"]
    assert plugin.ctx.events == []

    sync_failed_views(
        plugin,
        [
            ViewObservationKind.RETRYABLE_FAILURE,
            ViewObservationKind.RETRYABLE_FAILURE,
            ViewObservationKind.RETRYABLE_FAILURE,
        ],
    )
    at_threshold = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="三次失败后的降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert at_threshold["success"] is True
    assert at_threshold["accepted"] is True
    await wait_for_background_tasks(plugin)
    assert plugin.ctx.events[:2] == [("send", "target-a"), ("planner", "target-a")]
    await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_text"),
    [
        (ViewObservationKind.CORRECTABLE_FAILURE, "请检查并修正 msg_id"),
        (ViewObservationKind.TERMINAL_FAILURE, "不是可展开的合并转发消息"),
        (ViewObservationKind.UNKNOWN_FAILURE, "不会累计次数或开放降级"),
    ],
)
async def test_non_retryable_failures_never_unlock_fallback(
    tmp_path: Path,
    failure_kind: ViewObservationKind,
    expected_text: str,
) -> None:
    """验证重复的可修正、终止和未知失败都不会获得降级资格。

    同一消息连续登记三次指定失败后，即使 Planner 提供摘要，Tool 仍应拒绝
    创建任务并返回对应处理建议。该测试防止无效 msg_id、普通消息或无法
    分类的错误通过机械重试越过安全边界。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于运行真实请求服务。
        failure_kind: pytest 参数化提供的非重试型失败分类。
        expected_text: 预期出现在 Tool 拒绝结果中的分类说明。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    sync_failed_views(plugin, [failure_kind] * 3)

    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="不应使用的摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )

    assert result["success"] is False
    assert expected_text in result["content"]
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_empty_view_content_allows_one_diagnostic_retry_before_fallback(
    tmp_path: Path,
) -> None:
    """验证空内容需连续出现两次才允许降级。

    首次空内容应要求 Planner 再诊断查看一次，第二个独立调用仍为空时才接受
    摘要并创建任务。该测试防止偶发空结果立即降低上下文质量，同时避免持续
    空内容无限阻塞转发。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于运行真实请求和后台投递。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    sync_failed_views(plugin, [ViewObservationKind.EMPTY_CONTENT_FAILURE])

    first_result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="首次不应使用的摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert first_result["success"] is False
    assert "再进行一次诊断查看" in first_result["content"]

    sync_failed_views(
        plugin,
        [
            ViewObservationKind.EMPTY_CONTENT_FAILURE,
            ViewObservationKind.EMPTY_CONTENT_FAILURE,
        ],
    )
    second_result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="连续空内容后的降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )

    assert second_result["success"] is True
    assert second_result["accepted"] is True
    await wait_for_background_tasks(plugin)
    assert plugin.ctx.events[:2] == [("send", "target-a"), ("planner", "target-a")]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_tool_rejects_after_successful_view_leaves_current_context(tmp_path: Path) -> None:
    """验证成功查看结果离开当前上下文后要求重新查看而不降级。

    先登记成功查看，再用空 Planner 上下文替换当前快照。即使 Planner 提供
    摘要，Tool 也应拒绝任务并要求重新查看。该测试防止进程内旧索引延长
    查看资格或重新引入“缓存过期后摘要降级”。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于运行后台投递。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    seed_successful_view(plugin)
    plugin.runtime.view_eligibility.sync_context("source-stream", [])

    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="不应使用的上下文外摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is False
    assert "当前 Planner 上下文中尚无成功" in result["content"]
    assert "收到本工具结果后的紧接续轮" in result["content"]
    assert "不要使用 content_summary 绕过查看" in result["content"]
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_source_message_query_accepts_legacy_wrapped_success(tmp_path: Path) -> None:
    """验证源消息查询仍兼容旧版 SDK 的成功包装结果。

    将消息 capability 替换为返回 ``success/message`` 包装的旧版替身后，
    Tool 仍应接受任务。该测试防止修复当前 SDK 自动解包格式时破坏旧版
    Host 或测试环境兼容性。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.message = FakeStaticMessageCapability({"success": True, "message": build_forward_message()})
    await plugin.on_load()
    seed_successful_view(plugin)
    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is True
    assert result["accepted"] is True
    await wait_for_background_tasks(plugin)
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_source_message_query_preserves_host_failure_reason(tmp_path: Path) -> None:
    """验证 Host 明确失败时向 Planner 保留具体原因。

    capability 返回带 ``error`` 的失败包装后，Tool 应拒绝任务并包含原始
    错误原因，且不安排发送事件。该测试防止诊断信息再次退化为模糊的
    “未知错误”。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.message = FakeStaticMessageCapability({"success": False, "error": "消息不属于指定聊天流"})
    await plugin.on_load()
    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is False
    assert "消息不属于指定聊天流" in result["content"]
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_source_message_query_reports_missing_host_error(tmp_path: Path) -> None:
    """验证 Host 失败响应缺少 error 时返回可操作的稳定说明。

    capability 返回只有 ``success=False`` 的包装后，Tool 应说明 Host 未提供
    失败原因，而不是误称“未知错误”；同时不得安排发送事件。该测试保护
    不完整 Host 响应下的诊断质量。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.message = FakeStaticMessageCapability({"success": False})
    await plugin.on_load()
    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is False
    assert "Host 未提供失败原因" in result["content"]
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_source_group_can_share_forward_received_from_another_group(tmp_path: Path) -> None:
    """验证 source 群可分享最初来自其他群的合并转发消息。

    消息的 ``session_id`` 属于当前 source 聊天流，但元数据 ``group_id``
    模拟记录为其他群。Tool 应接受任务并完成投递，因为 source 白名单描述
    的是读取和发起分享的当前群，而不是合并转发内容的原始来源群。该测试
    防止重新引入 ``message.group_id == invocation.group_id`` 的错误限制。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.message = FakeMessageCapability(
        build_forward_message(stream_id="source-stream", group_id="original-group")
    )
    await plugin.on_load()
    seed_successful_view(plugin, content="来自其他群的合并转发完整内容")
    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        content_summary="来自其他群的合并转发摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is True
    assert result["accepted"] is True
    await wait_for_background_tasks(plugin)
    assert [event for event in plugin.ctx.events if event[0] == "send"] == [
        ("send", "target-a"),
        ("send", "target-b"),
    ]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_source_group_cannot_read_message_from_another_stream(tmp_path: Path) -> None:
    """验证放宽原始群号后仍拒绝其他聊天流中的消息。

    Tool 调用来自 source 白名单群，但消息 capability 模拟返回
    ``session_id=other-stream`` 的消息。Tool 应拒绝请求且不发送任何消息，
    证明授权边界仍由当前 source stream 控制。该测试防止删除群号比较时
    意外放开通过猜测 ``msg_id`` 进行的跨聊天流读取。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.message = FakeMessageCapability(build_forward_message(stream_id="other-stream", group_id="10001"))
    await plugin.on_load()
    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is False
    assert "不属于当前 source 聊天流" in result["content"]
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_tool_rejects_non_source_group(tmp_path: Path) -> None:
    """验证非 source 白名单群在读取消息前即被拒绝。

    请求来自群号 ``99999`` 时，期望返回 ``success=False`` 且错误文本提及
    source 白名单；消息 capability 调用记录必须为空。该测试防止未授权
    群通过猜测 ``msg_id`` 触发跨会话消息读取。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.chat.streams.append({"platform": "qq", "group_id": "99999", "stream_id": "other-stream"})
    await plugin.on_load()
    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        session_id="other-stream",
        platform="qq",
        group_id="99999",
        stream_id="other-stream",
    )
    assert result["success"] is False
    assert "source 白名单" in result["content"]
    assert plugin.ctx.message.calls == []
    await plugin.on_unload()
