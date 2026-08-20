"""target 顺序投递与失败恢复的回归测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from tests.support import (
    BlockingSendCapability,
    build_plugin,
    invoke_forward_tool,
    seed_successful_view,
    wait_for_background_tasks,
)


@pytest.mark.asyncio
async def test_tool_waits_for_real_delivery_before_reporting_success(tmp_path: Path) -> None:
    """验证 Tool 在真实发送放行前不会提前返回成功。

    发送替身进入首个 target 后保持阻塞，期望 Tool 调用仍未完成；释放闸门
    后全部 target 完成，Tool 才返回 ``completed=True`` 和真实成功统计。
    该测试防止后台任务刚入队就被错误报告为转发成功。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于运行真实请求和阶段持久化。
    """

    plugin = build_plugin(tmp_path)
    blocking_send = BlockingSendCapability(plugin.ctx.events)
    plugin.ctx.send = blocking_send
    await plugin.on_load()
    seed_successful_view(plugin)

    request_task = asyncio.create_task(
        invoke_forward_tool(
            plugin,
            "forward-message",
            sharing_reason="很有意思",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )
    )
    await blocking_send.started.wait()
    assert request_task.done() is False

    blocking_send.release.set()
    result = await request_task

    assert result["success"] is True
    assert result["completed"] is True
    assert result["status"] == "succeeded"
    assert result["sent_target_count"] == 2
    assert result["completed_target_count"] == 2
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_tool_sends_targets_in_order_and_deduplicates(tmp_path: Path) -> None:
    """验证多目标严格按发送与 Planner 顺序处理，并对完成任务执行去重。

    首次请求应依次产生 target-a 的 send/planner，再处理 target-b，且不再
    追加源群展开内容。第二次相同请求应返回 ``accepted=False``，发送事件
    总数保持为二。该测试防止并行或乱序投递、重复上下文和重复转发。

    Args:
        tmp_path: pytest 提供的临时目录，用于检查 ``forward_state.json``。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    seed_successful_view(plugin)

    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        sharing_reason="很有意思",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is True
    assert result["accepted"] is True
    assert result["completed"] is True
    assert result["status"] == "succeeded"
    await wait_for_background_tasks(plugin)

    assert plugin.ctx.events == [
        ("send", "target-a"),
        ("planner", "target-a"),
        ("send", "target-b"),
        ("planner", "target-b"),
    ]
    assert plugin.ctx.maisaka.proactive.metadata_by_stream["target-a"] == {
        "job_id": result["job_id"],
        "target_message_id": "sent-target-a",
    }
    assert "source_message_id" not in plugin.ctx.maisaka.proactive.metadata_by_stream["target-a"]
    target_intent = plugin.ctx.maisaka.proactive.intents_by_stream["target-a"]
    assert "目标消息 ID (msg_id) 是 sent-target-a" in target_intent
    assert "调用 reply 工具进行回复时只能使用这个 ID" in target_intent
    assert "完整内容已经写入当前上下文" not in target_intent
    assert plugin.ctx.message.calls == [("forward-message", "source-stream", True)]

    repeated = await invoke_forward_tool(
        plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert repeated["success"] is True
    assert repeated["accepted"] is False
    assert len([event for event in plugin.ctx.events if event[0] == "send"]) == 2
    state_payload = json.loads((tmp_path / "forward_state.json").read_text(encoding="utf-8"))
    assert state_payload["version"] == 3
    saved_job = next(iter(state_payload["jobs"].values()))
    assert saved_job["target_message_ids"] == {
        "20001": "sent-target-a",
        "20002": "sent-target-b",
    }
    await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send_result",
    [True, {"sent": True, "message_id": None}],
)
async def test_send_result_without_message_id_keeps_safe_reply_fallback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    send_result: bool | dict[str, object],
) -> None:
    """验证发送成功但缺少目标消息 ID 时仍沿用安全回复 fallback。

    发送替身分别模拟旧 Host 的布尔成功结果，以及详细结果中没有最终
    ``message_id`` 的成功响应。期望插件继续触发 Planner，但 metadata 不
    包含目标消息 ID，意图要求无法可靠定位时保持沉默，持久化状态也不伪造
    ID，并记录包含任务、target 和返回类型的 warning。该测试防止详细结果
    支持破坏无 ID fallback，或让生产日志无法判断兼容路径是否生效。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于检查兼容状态持久化。
        caplog: pytest 日志捕获器，用于验证 fallback warning 的级别和内容。
        send_result: pytest 参数化提供的无目标消息 ID 发送成功结果。
    """

    plugin = build_plugin(tmp_path, target_groups=["20001"])
    caplog.set_level(logging.WARNING, logger="test.forward-plugin")

    async def legacy_forward(
        messages: list[dict[str, object]],
        stream_id: str,
        **kwargs: object,
    ) -> bool | dict[str, object]:
        """模拟发送成功但没有返回最终目标消息 ID 的 Host。

        Args:
            messages: 插件传入的原始转发节点。
            stream_id: 接收消息的目标聊天流 ID。
            **kwargs: 插件传入的发送选项，用于确认仍请求详细结果。

        Returns:
            pytest 参数化提供的布尔值或详细成功结果。
        """

        assert messages
        assert kwargs["return_details"] is True
        plugin.ctx.events.append(("send", stream_id))
        return send_result

    plugin.ctx.send.forward = legacy_forward
    await plugin.on_load()
    seed_successful_view(plugin)

    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )

    assert result["success"] is True
    assert plugin.ctx.events == [("send", "target-a"), ("planner", "target-a")]
    assert plugin.ctx.maisaka.proactive.metadata_by_stream["target-a"] == {
        "job_id": result["job_id"],
    }
    assert "无法可靠定位时请保持沉默" in plugin.ctx.maisaka.proactive.intents_by_stream["target-a"]
    state_payload = json.loads((tmp_path / "forward_state.json").read_text(encoding="utf-8"))
    saved_job = next(iter(state_payload["jobs"].values()))
    assert saved_job["target_message_ids"] == {}
    expected_result_type = type(send_result).__name__
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage()
        == (
            "send.forward 未返回目标消息 ID，启用安全 fallback: "
            f"job={result['job_id']} target=20001 result_type={expected_result_type}"
        )
        for record in caplog.records
    )
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_raw_capability_call_preserves_host_returned_target_message_id(tmp_path: Path) -> None:
    """验证 SDK 布尔代理不会吞掉 Host 返回的最终目标消息 ID。

    替身把普通 ``ctx.send.forward`` 模拟为会丢失详情的当前 SDK 代理，并让
    ``call_host_method("cap.call")`` 返回 MaiBot dev 的原始详细发送结果。期望
    插件仅使用原始调用，持久化 Host 给出的最终 ID，并把它作为既有目标 Planner
    回复锚点。该测试防止以后改回普通 SDK 代理后把 ``message_id`` 静默压缩成
    ``True``。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于检查持久化目标消息 ID。
    """

    plugin = build_plugin(tmp_path, target_groups=["20001"])
    raw_calls: list[dict[str, object]] = []

    async def normalized_forward(
        messages: list[dict[str, object]],
        stream_id: str,
        **kwargs: object,
    ) -> bool:
        """在测试中阻止误用会压缩详细结果的普通 SDK 发送代理。

        Args:
            messages: 普通 SDK 代理本应接收的原始转发节点。
            stream_id: 普通 SDK 代理本应接收的目标聊天流 ID。
            **kwargs: 普通 SDK 代理本应接收的发送选项。

        Raises:
            AssertionError: 当投递服务没有使用原始 ``cap.call`` 通道时抛出。
        """

        del messages, stream_id, kwargs
        raise AssertionError("详细发送结果不能经过会布尔归一化的 ctx.send.forward")

    async def call_host_method(
        method: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        """模拟保留 Host 原始详细结果的受限 ``cap.call`` 通道。

        Args:
            method: SDK 请求的原始 Host RPC 方法，必须为 ``"cap.call"``。
            payload: 包含 capability 名称及其参数的 RPC 载荷。
            **kwargs: 本测试中不应传入的额外 RPC 选项。

        Returns:
            具有 ``success``、``sent`` 与平台最终 ``message_id`` 的 Host
            详细发送结果。
        """

        assert method == "cap.call"
        assert kwargs == {}
        assert isinstance(payload, dict)
        assert payload["capability"] == "send.forward"
        args = payload["args"]
        assert isinstance(args, dict)
        assert args["stream_id"] == "target-a"
        assert args["return_details"] is True
        assert args["sync_to_maisaka_history"] is True
        raw_calls.append(payload)
        plugin.ctx.events.append(("send", "target-a"))
        return {
            "success": True,
            "sent": True,
            "message_id": "host-final-target-id",
        }

    plugin.ctx.send.forward = normalized_forward
    plugin.ctx.call_host_method = call_host_method
    await plugin.on_load()
    seed_successful_view(plugin)

    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )

    assert result["success"] is True
    assert len(raw_calls) == 1
    assert plugin.ctx.events == [("send", "target-a"), ("planner", "target-a")]
    assert plugin.ctx.maisaka.proactive.metadata_by_stream["target-a"] == {
        "job_id": result["job_id"],
        "target_message_id": "host-final-target-id",
    }
    state_payload = json.loads((tmp_path / "forward_state.json").read_text(encoding="utf-8"))
    saved_job = next(iter(state_payload["jobs"].values()))
    assert saved_job["target_message_ids"] == {"20001": "host-final-target-id"}
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_new_target_does_not_resend_completed_existing_target(tmp_path: Path) -> None:
    """验证路由新增 target 时永久防重状态只允许发送新目标。

    第一个实例只配置 target-a 并完成投递；第二个实例使用同一状态目录，
    将 target-b 加入路由。新请求应被接受以处理 target-b，但不得再次向
    target-a 发送。该测试防止 target 集合参与任务键后因路由变化重发旧群。

    Args:
        tmp_path: 两个插件实例共享的 pytest 临时状态目录。
    """

    first_plugin = build_plugin(tmp_path, target_groups=["20001"])
    await first_plugin.on_load()
    seed_successful_view(first_plugin)
    first_result = await invoke_forward_tool(
        first_plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert first_result["accepted"] is True
    await wait_for_background_tasks(first_plugin)
    await first_plugin.on_unload()

    expanded_plugin = build_plugin(
        tmp_path,
        target_groups=["20001", "20002"],
    )
    await expanded_plugin.on_load()
    seed_successful_view(expanded_plugin)
    expanded_result = await invoke_forward_tool(
        expanded_plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert expanded_result["accepted"] is True
    await wait_for_background_tasks(expanded_plugin)

    assert expanded_plugin.ctx.events == [
        ("send", "target-b"),
        ("planner", "target-b"),
    ]
    await expanded_plugin.on_unload()


@pytest.mark.asyncio
async def test_send_failure_skips_planner_but_continues_next_target(tmp_path: Path) -> None:
    """验证单个 target 发送失败不会触发 Planner 或中断后续 target。

    target-a 模拟物理发送失败，期望不出现其 planner 事件；target-b 仍应
    完成 send/planner。该测试防止失败消息触发目标 Planner，也防止一个群
    的故障阻塞整个路由。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

    plugin = build_plugin(tmp_path, failed_streams={"target-a"})
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
    assert result["accepted"] is True
    assert result["success"] is False
    assert result["completed"] is True
    assert result["status"] == "partial_failed"
    assert result["sent_target_count"] == 1
    assert result["completed_target_count"] == 1
    assert "20001（发送：模拟发送失败）" in result["content"]
    await wait_for_background_tasks(plugin)

    assert plugin.ctx.events == [
        ("send", "target-a"),
        ("send", "target-b"),
        ("planner", "target-b"),
    ]
    await plugin.on_unload()
