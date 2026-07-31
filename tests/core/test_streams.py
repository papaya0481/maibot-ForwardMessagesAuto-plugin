"""群聊流双向解析的回归测试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from forward_messages_auto.core.streams import GroupStreamRegistry
from tests.support import FakeChatCapability


@pytest.mark.asyncio
async def test_group_stream_registry_resolves_targets_on_demand() -> None:
    """验证聊天流注册表只在实际投递时按需解析目标群。

    已有 target 应通过 ``get_stream_by_group_id`` 查询，未知 target 应通过
    ``open_session`` 创建；可信 source 会话应通过群聊流列表反查真实群号。
    该测试防止重新引入启动期预加载依赖、信任消息元数据中的来源群，或把
    SDK 解包后的聊天流结构误判为失败。
    """

    context = SimpleNamespace(
        chat=FakeChatCapability(
            [
                {"platform": "qq", "group_id": "10001", "stream_id": "source-stream"},
                {"platform": "qq", "group_id": "20001", "stream_id": "target-a"},
            ]
        ),
        logger=logging.getLogger("test.forward-plugin"),
    )
    registry = GroupStreamRegistry(context)
    assert await registry.resolve_group("source-stream") == "10001"
    assert await registry.resolve("20001") == "target-a"
    assert await registry.resolve("30001") == "opened-30001"
