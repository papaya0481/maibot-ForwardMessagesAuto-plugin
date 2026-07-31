"""跨群转发永久状态迁移的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import (
    build_plugin,
    invoke_forward_tool,
    seed_successful_view,
    wait_for_background_tasks,
)


@pytest.mark.asyncio
async def test_legacy_route_jobs_migrate_and_preserve_highest_target_stage(
    tmp_path: Path,
) -> None:
    """验证 v0.1.11 路由级状态会合并且不按旧时间清理。

    状态文件预置两个具有极早时间戳的旧任务：target-a 已完成，target-b
    停留在旧版 ``context_appended`` 并带有目标消息 ID。加载后请求应只恢复
    target-b 的 Planner，继续使用已持久化 ID，且不向任何 target 重复物理
    发送。该测试防止迁移时丢失永久防重记录或回复锚点。

    Args:
        tmp_path: pytest 提供的临时状态目录，用于写入旧版状态文件。
    """

    legacy_payload = {
        "version": 1,
        "updated_at": 0,
        "jobs": {
            "source-stream:forward-message:route-a": {
                "job_id": "route-a",
                "source_stream_id": "source-stream",
                "source_group_id": "10001",
                "source_message_id": "forward-message",
                "target_group_ids": ["20001"],
                "created_at": 0,
                "updated_at": 0,
                "targets": {"20001": "planner_queued"},
            },
            "source-stream:forward-message:route-b": {
                "job_id": "route-b",
                "source_stream_id": "source-stream",
                "source_group_id": "10001",
                "source_message_id": "forward-message",
                "target_group_ids": ["20002"],
                "created_at": 0,
                "updated_at": 0,
                "targets": {"20002": "context_appended"},
                "target_message_ids": {"20002": "persisted-target-b"},
            },
        },
    }
    (tmp_path / "forward_state.json").write_text(
        json.dumps(legacy_payload),
        encoding="utf-8",
    )

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    seed_successful_view(plugin)
    result = await invoke_forward_tool(
        plugin,
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["accepted"] is True
    await wait_for_background_tasks(plugin)

    assert plugin.ctx.events == [
        ("planner", "target-b"),
    ]
    assert plugin.ctx.maisaka.proactive.metadata_by_stream["target-b"] == {
        "job_id": result["job_id"],
        "target_message_id": "persisted-target-b",
    }
    assert "目标消息 ID (msg_id) 是 persisted-target-b" in plugin.ctx.maisaka.proactive.intents_by_stream["target-b"]
    await plugin.on_unload()
    saved_payload = json.loads((tmp_path / "forward_state.json").read_text(encoding="utf-8"))
    assert saved_payload["version"] == 3
    assert len(saved_payload["jobs"]) == 1
    saved_job = next(iter(saved_payload["jobs"].values()))
    assert saved_job["target_message_ids"] == {"20002": "persisted-target-b"}
