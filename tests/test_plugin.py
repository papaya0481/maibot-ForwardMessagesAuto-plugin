from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugin import (
    FORWARD_TOOL_NAME,
    ForwardMessagesAutoPlugin,
    extract_forward_payload,
    extract_view_forward_results,
    normalize_group_ids,
)


def build_forward_message(*, stream_id: str = "source-stream", group_id: str = "10001") -> dict[str, Any]:
    return {
        "message_id": "forward-message",
        "session_id": stream_id,
        "group_id": group_id,
        "platform": "qq",
        "processed_plain_text": "合并转发预览",
        "raw_message": [
            {
                "type": "forward",
                "data": [
                    {
                        "user_id": "42",
                        "user_nickname": "群友甲",
                        "user_cardname": "",
                        "message_id": "node-1",
                        "content": [
                            {"type": "text", "data": "有趣的消息"},
                            {
                                "type": "image",
                                "data": "图片描述",
                                "hash": "image-hash",
                                "binary_data_base64": "aW1hZ2U=",
                            },
                        ],
                    }
                ],
            }
        ],
    }


class FakeMessageCapability:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.calls: list[tuple[str, str, bool]] = []

    async def get_by_id(
        self,
        message_id: str,
        *,
        stream_id: str = "",
        include_binary_data: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        self.calls.append((message_id, stream_id, include_binary_data))
        return {"success": True, "message": self.message}


class FakeChatCapability:
    def __init__(self, streams: list[dict[str, Any]]) -> None:
        self.streams = streams

    async def get_group_streams(self, platform: str = "qq") -> dict[str, Any]:
        assert platform == "qq"
        return {"success": True, "streams": self.streams}

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> dict[str, Any]:
        assert platform == "qq"
        stream = next((item for item in self.streams if item["group_id"] == group_id), None)
        return {"success": True, "stream": stream}

    async def open_session(
        self,
        platform: str,
        chat_type: str,
        *,
        group_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        assert platform == "qq"
        assert chat_type == "group"
        stream = {
            "platform": "qq",
            "group_id": group_id,
            "stream_id": f"opened-{group_id}",
            "session_id": f"opened-{group_id}",
        }
        self.streams.append(stream)
        return {"success": True, "stream": stream}


class FakeSendCapability:
    def __init__(self, events: list[tuple[str, str]], failed_streams: set[str] | None = None) -> None:
        self.events = events
        self.failed_streams = failed_streams or set()
        self.messages_by_stream: dict[str, list[dict[str, Any]]] = {}

    async def forward(
        self,
        messages: list[dict[str, Any]],
        stream_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert kwargs["sync_to_maisaka_history"] is False
        self.events.append(("send", stream_id))
        self.messages_by_stream[stream_id] = messages
        if stream_id in self.failed_streams:
            return {"success": False, "error": "模拟发送失败"}
        return {"success": True}


class FakeMaisakaContextCapability:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.visible_text_by_stream: dict[str, str] = {}

    async def append(
        self,
        stream_id: str,
        segments: list[dict[str, Any]],
        *,
        visible_text: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        assert segments[0]["type"] == "forward"
        self.events.append(("context", stream_id))
        self.visible_text_by_stream[stream_id] = visible_text
        return {"success": True}


class FakeMaisakaProactiveCapability:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events

    async def trigger(self, stream_id: str, intent: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        assert "无需再次调用 view_forward_message" in intent
        self.events.append(("planner", stream_id))
        return {"success": True, "queued": True, "task_id": f"task-{stream_id}"}


class FakeContext:
    def __init__(
        self,
        *,
        data_dir: Path,
        message: dict[str, Any],
        streams: list[dict[str, Any]],
        failed_streams: set[str] | None = None,
    ) -> None:
        self.logger = logging.getLogger("test.forward-plugin")
        self.paths = SimpleNamespace(data_dir=data_dir, runtime_dir=data_dir / "runtime")
        self.events: list[tuple[str, str]] = []
        self.message = FakeMessageCapability(message)
        self.chat = FakeChatCapability(streams)
        self.send = FakeSendCapability(self.events, failed_streams)
        self.maisaka = SimpleNamespace(
            context=FakeMaisakaContextCapability(self.events),
            proactive=FakeMaisakaProactiveCapability(self.events),
        )


def build_plugin(tmp_path: Path, *, failed_streams: set[str] | None = None) -> ForwardMessagesAutoPlugin:
    plugin = ForwardMessagesAutoPlugin()
    plugin.set_plugin_config(
        {
            "plugin": {
                "enabled": True,
                "version": "0.1.0",
                "config_version": "0.1.0",
            },
            "routing": {
                "source_groups": ["10001"],
                "target_groups": ["20001", "20002"],
            },
            "behavior": {
                "view_cache_ttl_seconds": 1800,
                "dedupe_ttl_seconds": 604800,
                "trigger_target_planner": True,
            },
        }
    )
    streams = [
        {"platform": "qq", "group_id": "10001", "stream_id": "source-stream"},
        {"platform": "qq", "group_id": "20001", "stream_id": "target-a"},
        {"platform": "qq", "group_id": "20002", "stream_id": "target-b"},
    ]
    plugin._set_context(
        FakeContext(
            data_dir=tmp_path,
            message=build_forward_message(),
            streams=streams,
            failed_streams=failed_streams,
        )
    )
    return plugin


async def wait_for_background_tasks(plugin: ForwardMessagesAutoPlugin) -> None:
    tasks = list(plugin._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def test_normalize_group_ids_preserves_order_and_deduplicates() -> None:
    assert normalize_group_ids([" 100 ", 200, "100", "", None, "300"]) == ["100", "200", "300"]


def test_extract_forward_payload_preserves_nodes_and_binary_data() -> None:
    payload = extract_forward_payload(build_forward_message())
    assert payload is not None
    segment, messages = payload
    assert segment["type"] == "forward"
    assert messages[0]["nickname"] == "群友甲"
    assert messages[0]["segments"][1]["binary_data_base64"] == "aW1hZ2U="


def test_extract_view_forward_results_pairs_tool_call_and_result() -> None:
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
    assert extract_view_forward_results(messages) == [("message-1", "完整展开内容")]


def test_forward_tool_component_is_visible_only_in_group_scope() -> None:
    plugin = ForwardMessagesAutoPlugin()
    plugin.set_plugin_config({})
    component = next(item for item in plugin.get_components() if item["name"] == FORWARD_TOOL_NAME)
    assert component["chat_scope"] == "group"
    assert component["metadata"]["visibility"] == "visible"


@pytest.mark.asyncio
async def test_hook_caches_view_result_and_hides_tool_outside_source(tmp_path: Path) -> None:
    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "view_forward_message",
                        "arguments": {"msg_id": "forward-message"},
                    },
                }
            ],
        },
        {"role": "tool", "content": "完整展开内容", "tool_call_id": "call-1"},
    ]
    definitions = [
        {"type": "function", "function": {"name": FORWARD_TOOL_NAME}},
        {"type": "function", "function": {"name": "reply"}},
    ]

    source_result = await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=definitions,
    )
    assert plugin._get_cached_view_content("source-stream", "forward-message") == "完整展开内容"
    assert len(source_result["modified_kwargs"]["tool_definitions"]) == 2

    other_result = await plugin.capture_view_forward_result(
        session_id="target-a",
        messages=[],
        tool_definitions=definitions,
    )
    assert [item["function"]["name"] for item in other_result["modified_kwargs"]["tool_definitions"]] == ["reply"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_tool_sends_targets_in_order_and_deduplicates(tmp_path: Path) -> None:
    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    plugin._view_cache[("source-stream", "forward-message")] = SimpleNamespace(
        content="完整展开内容",
        cached_at=10**12,
    )

    result = await plugin.request_cross_group_forward(
        "forward-message",
        sharing_reason="很有意思",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is True
    assert result["accepted"] is True
    await wait_for_background_tasks(plugin)

    assert plugin.ctx.events == [
        ("send", "target-a"),
        ("context", "target-a"),
        ("planner", "target-a"),
        ("send", "target-b"),
        ("context", "target-b"),
        ("planner", "target-b"),
    ]
    assert "完整展开内容" in plugin.ctx.maisaka.context.visible_text_by_stream["target-a"]
    assert plugin.ctx.message.calls == [("forward-message", "source-stream", True)]

    repeated = await plugin.request_cross_group_forward(
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert repeated["success"] is True
    assert repeated["accepted"] is False
    assert len([event for event in plugin.ctx.events if event[0] == "send"]) == 2
    assert (tmp_path / "forward_state.json").is_file()
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_send_failure_skips_context_but_continues_next_target(tmp_path: Path) -> None:
    plugin = build_plugin(tmp_path, failed_streams={"target-a"})
    await plugin.on_load()
    result = await plugin.request_cross_group_forward(
        "forward-message",
        content_summary="降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["accepted"] is True
    await wait_for_background_tasks(plugin)

    assert plugin.ctx.events == [
        ("send", "target-a"),
        ("send", "target-b"),
        ("context", "target-b"),
        ("planner", "target-b"),
    ]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_tool_rejects_non_source_group(tmp_path: Path) -> None:
    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    result = await plugin.request_cross_group_forward(
        "forward-message",
        platform="qq",
        group_id="99999",
        stream_id="other-stream",
    )
    assert result["success"] is False
    assert "source 白名单" in result["content"]
    assert plugin.ctx.message.calls == []
    await plugin.on_unload()
