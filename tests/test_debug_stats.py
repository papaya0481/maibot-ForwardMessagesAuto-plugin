"""本地调试统计的口径、持久化和安全降级测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from forward_messages_auto.debug_stats import (
    DEBUG_STATS_ENV_VAR,
    DEBUG_STATS_FILENAME,
    DebugForwardStatsStore,
)
from forward_messages_auto.source.authorization import (
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
    FORWARD_TOOL_NAME,
)
from forward_messages_auto.core.models import ForwardJob
from tests.support import build_forward_message, build_plugin


def _build_forward_call(call_id: str, message_id: str) -> dict[str, Any]:
    """构造只包含公开参数的 Planner 转发工具调用。

    Args:
        call_id: Host 序列化工具调用使用的唯一调用 ID。
        message_id: Planner 请求分享的 source 消息 ID。

    Returns:
        可传给 EARLY ``after_response`` Hook 的工具调用字典。
    """

    return {
        "id": call_id,
        "function": {
            "name": FORWARD_TOOL_NAME,
            "arguments": {
                "msg_id": message_id,
                "sharing_reason": "",
                "content_summary": "",
            },
        },
    }


@pytest.mark.asyncio
async def test_debug_stats_are_disabled_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证默认关闭时不读取、创建或写入任何统计文件。

    即使调用登记方法并停止存储，运行时目录中也不应出现统计文件。该测试
    防止普通用户在未选择调试时产生本地统计数据。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
        monkeypatch: pytest 提供的环境变量隔离工具。
    """

    monkeypatch.delenv(DEBUG_STATS_ENV_VAR, raising=False)
    path = tmp_path / DEBUG_STATS_FILENAME
    store = DebugForwardStatsStore(
        path,
        tmp_path / "source",
        "0.2.2",
        logging.getLogger("test.debug-stats.disabled"),
    )
    load_attempted = False

    def fail_if_loaded() -> dict[str, Any]:
        """标记意外读取并立即失败。

        Returns:
            此替身不会正常返回。

        Raises:
            AssertionError: 默认关闭的存储若尝试读取文件时抛出。
        """

        nonlocal load_attempted
        load_attempted = True
        raise AssertionError("默认关闭时不应读取统计文件")

    monkeypatch.setattr(store, "_load_sync", fail_if_loaded)

    assert await store.start() is False
    assert load_attempted is False
    assert store.record_observed("source-stream", "message-1") is False
    assert store.record_requested("source-stream", "message-1") is False
    await store.stop()

    assert not path.exists()


@pytest.mark.asyncio
async def test_observed_stats_do_not_depend_on_source_planner_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 source Planner 开关关闭时仍按消息身份统计唯一观察量。

    同一 ``stream + msg_id`` 的重复 Hook 只计一次，不同 stream 的同名消息
    分别计数；非 source、机器人自身消息和已知插件转发回声均不计。Planner
    触发事件保持为空。该测试防止分母被错误绑定到主动触发功能。

    Args:
        tmp_path: pytest 提供的插件运行时数据目录。
        monkeypatch: pytest 提供的环境变量设置工具。
    """

    monkeypatch.setenv(DEBUG_STATS_ENV_VAR, "1")
    plugin = build_plugin(tmp_path, trigger_source_planner=False)
    await plugin.on_load()

    source_message = build_forward_message()
    await plugin.trigger_source_planner_for_forward_message(message=source_message)
    await plugin.trigger_source_planner_for_forward_message(message=source_message)
    await plugin.trigger_source_planner_for_forward_message(
        message=build_forward_message(stream_id="second-source-stream")
    )
    await plugin.trigger_source_planner_for_forward_message(
        message=build_forward_message(group_id="30001", stream_id="non-source-stream")
    )
    await plugin.trigger_source_planner_for_forward_message(
        message=build_forward_message(sender_id="99999", self_id="99999")
    )

    echo_job = ForwardJob(
        job_id="debug-echo-job",
        state_key=plugin.runtime.state.build_state_key("origin-stream", "origin-message"),
        source_stream_id="origin-stream",
        source_group_id="40001",
        source_message_id="origin-message",
        target_group_ids=["10001"],
        forward_messages=[],
        sharing_reason="",
    )
    await plugin.runtime.state.record_target_sent(
        echo_job,
        "10001",
        "echo-message",
    )
    echo_message = build_forward_message(self_id="")
    echo_message["message_id"] = "echo-message"
    await plugin.trigger_source_planner_for_forward_message(message=echo_message)

    snapshot = plugin.runtime.debug_stats.snapshot()
    assert snapshot.observed == 2
    assert snapshot.requested == 0
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_early_hook_counts_only_observed_unique_planner_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 EARLY Hook 只统计已观察消息的唯一 Planner 原始请求。

    混合批次包含同一消息的两个转发调用、一个未观察消息和一个无关工具。
    期望调用顺序保持不变且 requested 只增加一次；重复轮次不抬高计数。
    该测试防止把重试、未知消息或插件后续恢复误算为新的 Planner 决策。

    Args:
        tmp_path: pytest 提供的插件运行时数据目录。
        monkeypatch: pytest 提供的环境变量设置工具。
    """

    monkeypatch.setenv(DEBUG_STATS_ENV_VAR, "true")
    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    await plugin.trigger_source_planner_for_forward_message(message=build_forward_message())

    tool_calls = [
        _build_forward_call("request-a", "forward-message"),
        {
            "id": "unrelated",
            "function": {"name": "other_tool", "arguments": {}},
        },
        _build_forward_call("request-b", "forward-message"),
        _build_forward_call("unknown", "not-observed"),
    ]
    first_result = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        tool_calls=tool_calls,
    )
    second_result = await plugin.authorize_forward_request_context(
        session_id="source-stream",
        tool_calls=tool_calls,
    )

    assert [call["function"]["name"] for call in first_result["modified_kwargs"]["tool_calls"]] == [
        FORWARD_TOOL_NAME,
        "other_tool",
        FORWARD_TOOL_NAME,
        FORWARD_TOOL_NAME,
    ]
    assert [call["function"]["name"] for call in second_result["modified_kwargs"]["tool_calls"]] == [
        FORWARD_TOOL_NAME,
        "other_tool",
        FORWARD_TOOL_NAME,
        FORWARD_TOOL_NAME,
    ]
    snapshot = plugin.runtime.debug_stats.snapshot()
    assert snapshot.observed == 1
    assert snapshot.requested == 1
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_stats_persist_anonymous_dedupe_keys_and_version_buckets(
    tmp_path: Path,
) -> None:
    """验证匿名去重键跨重载保存，并在升级后创建独立版本桶。

    首个版本写入两条观察和一条请求；同版本重载后重复登记不得增长。新版本
    初始计数为零且仍能读取旧桶，随后只更新自己的桶。文件不得出现任何原始
    stream、消息、群号或正文。该测试防止重载重复计数、版本混算和隐私泄露。

    Args:
        tmp_path: pytest 提供且由多个存储实例复用的本地数据目录。
    """

    path = tmp_path / DEBUG_STATS_FILENAME
    source_root = tmp_path / "source"
    logger = logging.getLogger("test.debug-stats.persistence")

    first = DebugForwardStatsStore(
        path,
        source_root,
        "0.2.2",
        logger,
        enabled=True,
    )
    assert await first.start() is True
    assert first.record_observed("private-stream-a", "private-message-a") is True
    assert first.record_observed("private-stream-b", "private-message-b") is True
    assert first.record_requested("private-stream-a", "private-message-a") is True
    await first.stop()

    reloaded = DebugForwardStatsStore(
        path,
        source_root,
        "0.2.2",
        logger,
        enabled=True,
    )
    assert await reloaded.start() is True
    assert reloaded.record_observed("private-stream-a", "private-message-a") is False
    assert reloaded.record_requested("private-stream-a", "private-message-a") is False
    assert reloaded.snapshot().display() == "1/2（50%）"
    await reloaded.stop()

    upgraded = DebugForwardStatsStore(
        path,
        source_root,
        "0.2.3",
        logger,
        enabled=True,
    )
    assert await upgraded.start() is True
    assert upgraded.snapshot().display() == "0/0（0%）"
    assert upgraded.snapshot("0.2.2").display() == "1/2（50%）"
    assert upgraded.record_observed("private-stream-a", "private-message-a") is True
    await upgraded.stop()

    raw_text = path.read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    assert payload["versions"]["0.2.2"]["observed"] == 2
    assert payload["versions"]["0.2.2"]["requested"] == 1
    assert payload["versions"]["0.2.3"]["observed"] == 1
    assert "private-stream" not in raw_text
    assert "private-message" not in raw_text
    assert "10001" not in raw_text
    assert "有趣的消息" not in raw_text


@pytest.mark.asyncio
async def test_stats_display_matches_requested_over_observed(
    tmp_path: Path,
) -> None:
    """验证展示比例由整数集合实时计算且不持久化。

    登记二十五条观察并将其中十三条标记为 Planner 请求后，应展示
    ``13/25（52%）``；序列化数据只保存整数计数和匿名集合，不保存比例字段。
    该测试防止比例舍入或持久化口径偏离设计。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    path = tmp_path / DEBUG_STATS_FILENAME
    store = DebugForwardStatsStore(
        path,
        tmp_path / "source",
        "0.2.2",
        logging.getLogger("test.debug-stats.display"),
        enabled=True,
    )
    assert await store.start() is True
    for index in range(25):
        assert store.record_observed("source-stream", f"message-{index}") is True
    for index in range(13):
        assert store.record_requested("source-stream", f"message-{index}") is True

    assert store.snapshot().display() == "13/25（52%）"
    await store.stop()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "ratio" not in payload["versions"]["0.2.2"]


@pytest.mark.asyncio
async def test_recording_only_wakes_background_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证同步登记阶段不直接执行统计文件 I/O。

    替换同步 writer 后先登记观察量，再通过 ``record_requested_calls`` 登记
    Planner 请求；两次同步调用返回前都不应发生写入，事件循环让出控制后后台
    任务才会保存。该测试防止 EARLY Hook 因统计逻辑违反无 I/O 边界。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
        monkeypatch: pytest 提供的方法替换工具，用于观测 writer 调用时机。
    """

    store = DebugForwardStatsStore(
        tmp_path / DEBUG_STATS_FILENAME,
        tmp_path / "source",
        "0.2.2",
        logging.getLogger("test.debug-stats.writer"),
        enabled=True,
    )
    assert await store.start() is True
    writes: list[dict[str, Any]] = []

    def capture_write(payload: dict[str, Any]) -> None:
        """记录后台 writer 接收的快照，不执行真实文件写入。

        Args:
            payload: worker 准备写入的完整统计快照。
        """

        writes.append(payload)

    monkeypatch.setattr(store, "_write_sync", capture_write)
    assert store.record_observed("source-stream", "message-1") is True
    assert writes == []

    for _ in range(20):
        await asyncio.sleep(0.01)
        if writes:
            break
    assert len(writes) == 1

    writes.clear()
    assert (
        store.record_requested_calls(
            "source-stream",
            [
                {
                    "id": "request-a",
                    "function": {
                        "name": FORWARD_TOOL_NAME,
                        "arguments": {
                            "msg_id": "message-1",
                            FORWARD_CONTEXT_TOKEN_ARGUMENT: "authorized",
                        },
                    },
                }
            ],
        )
        == 1
    )
    assert writes == []

    for _ in range(20):
        await asyncio.sleep(0.01)
        if writes:
            break
    assert len(writes) == 1
    await store.stop()


@pytest.mark.asyncio
async def test_writer_failure_disables_stats_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证后台保存失败只关闭统计而不向调用路径传播。

    writer 模拟不可写错误后，内存登记仍先正常返回；后台任务随后关闭统计并
    记录 warning，``stop`` 也能安全完成。该测试防止本地调试文件故障中断
    正常 Hook、转发或插件卸载。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
        monkeypatch: pytest 提供的方法替换工具，用于模拟磁盘写入失败。
        caplog: pytest 提供的日志捕获器，用于确认安全降级可见。
    """

    caplog.set_level(logging.WARNING)
    path = tmp_path / DEBUG_STATS_FILENAME
    store = DebugForwardStatsStore(
        path,
        tmp_path / "source",
        "0.2.2",
        logging.getLogger("test.debug-stats.write-failure"),
        enabled=True,
    )
    assert await store.start() is True

    def fail_write(payload: dict[str, Any]) -> None:
        """模拟运行时数据目录不可写。

        Args:
            payload: worker 准备写入但本替身不会使用的统计快照。

        Raises:
            OSError: 每次调用都抛出，用于验证后台安全降级。
        """

        del payload
        raise OSError("模拟不可写")

    monkeypatch.setattr(store, "_write_sync", fail_write)
    assert store.record_observed("source-stream", "message-1") is True
    for _ in range(20):
        await asyncio.sleep(0.01)
        if not store.enabled:
            break

    assert store.enabled is False
    await store.stop()
    assert not path.exists()
    assert "调试统计保存失败" in caplog.text


@pytest.mark.asyncio
async def test_unsafe_or_corrupt_stats_files_are_not_overwritten(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证源码树内路径和损坏文件都会安全关闭统计。

    源码树内目标不得被创建；源码树外的损坏 JSON 应保持原样，不得被当作空
    桶覆盖。两种失败均只记录 warning 并让登记方法返回 ``False``。该测试
    防止本地统计污染 Git 工作树或在恢复失败时破坏调试证据。

    Args:
        tmp_path: pytest 提供的隔离目录，同时模拟源码根和外部数据根。
        caplog: pytest 提供的日志捕获器，用于确认安全降级可见。
    """

    caplog.set_level(logging.WARNING)
    unsafe_path = tmp_path / "source" / DEBUG_STATS_FILENAME
    unsafe = DebugForwardStatsStore(
        unsafe_path,
        tmp_path / "source",
        "0.2.2",
        logging.getLogger("test.debug-stats.unsafe"),
        enabled=True,
    )
    assert await unsafe.start() is False
    assert unsafe.record_observed("source-stream", "message-1") is False
    await unsafe.stop()
    assert not unsafe_path.exists()

    corrupt_path = tmp_path / "runtime" / DEBUG_STATS_FILENAME
    corrupt_path.parent.mkdir(parents=True)
    corrupt_path.write_text("{broken", encoding="utf-8")
    corrupt = DebugForwardStatsStore(
        corrupt_path,
        tmp_path / "source",
        "0.2.2",
        logging.getLogger("test.debug-stats.corrupt"),
        enabled=True,
    )
    assert await corrupt.start() is False
    assert corrupt.record_requested("source-stream", "message-1") is False
    await corrupt.stop()

    assert corrupt_path.read_text(encoding="utf-8") == "{broken"
    assert "调试统计加载失败" in caplog.text
