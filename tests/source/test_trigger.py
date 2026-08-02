"""source 合并转发消息强制触发 Planner 的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forward_messages_auto.core.models import ForwardJob
from tests.support import (
    FakeDelayedMessageCapability,
    build_forward_message,
    build_plugin,
    wait_for_background_tasks,
)


def test_source_trigger_uses_observe_hook_before_frequency_gate() -> None:
    """验证 source 触发组件订阅入站轻量处理后的观察型 Hook。

    期望组件绑定 ``chat.receive.after_process`` 且使用 ``observe`` 模式，
    从而能在普通回复频率门控前识别消息，同时不阻塞或改写入站主链。该测试
    防止误用当前 Host 未接入主链的 ``ON_MESSAGE`` 事件，或退回阻塞 Hook。
    """

    plugin = build_plugin(Path("unused"))
    component = next(
        item for item in plugin.get_components() if item.get("name") == "trigger_source_planner_for_forward_message"
    )
    assert component["type"] == "HOOK_HANDLER"
    assert component["metadata"]["hook"] == "chat.receive.after_process"
    assert component["metadata"]["mode"] == "observe"


@pytest.mark.asyncio
async def test_source_trigger_waits_for_real_message_and_only_queues_planner(tmp_path: Path) -> None:
    """验证开关启用后会等待真实消息就绪，并且只触发 source Planner。

    Hook 首次查询模拟消息尚未落库，第二次才返回同一聊天流的合并转发。
    期望最终只出现 source Planner 事件，不发生物理发送；metadata 保留同流
    ``msg_id``。该测试防止主动任务抢在真实消息入库前运行，或把强制触发
    扩张为自动转发；Planner 可见文本不属于本测试范围。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于保存永久触发防重键。
    """

    plugin = build_plugin(tmp_path, trigger_source_planner=True)
    await plugin.on_load()
    plugin.ctx.message = FakeDelayedMessageCapability(build_forward_message())

    await plugin.trigger_source_planner_for_forward_message(message=build_forward_message())
    await wait_for_background_tasks(plugin)

    assert plugin.ctx.message.calls == [
        ("forward-message", "source-stream", False),
        ("forward-message", "source-stream", False),
    ]
    assert plugin.ctx.events == [("planner", "source-stream")]
    assert plugin.ctx.send.messages_by_stream == {}
    assert plugin.ctx.maisaka.proactive.metadata_by_stream["source-stream"] == {
        "trigger_kind": "source_forward_message",
        "source_message_id": "forward-message",
    }
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_source_trigger_switch_and_source_whitelist_are_independent(tmp_path: Path) -> None:
    """验证显式关闭 source 触发开关或群不在 source 白名单时完全忽略消息。

    默认开启的插件在显式传入 ``False`` 后收到合法 source 消息时不应查询消息
    或触发 Planner；即使开关启用，非 source 群也应保持相同行为。该测试防止
    关闭配置失效，也防止 target 或任意群绕过 source 白名单触发。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于分别加载两个插件实例。
    """

    disabled_plugin = build_plugin(tmp_path / "disabled", trigger_source_planner=False)
    await disabled_plugin.on_load()
    await disabled_plugin.trigger_source_planner_for_forward_message(message=build_forward_message())
    await wait_for_background_tasks(disabled_plugin)
    assert disabled_plugin.ctx.message.calls == []
    assert disabled_plugin.ctx.events == []
    await disabled_plugin.on_unload()

    non_source_plugin = build_plugin(tmp_path / "non-source", trigger_source_planner=True)
    await non_source_plugin.on_load()
    await non_source_plugin.trigger_source_planner_for_forward_message(
        message=build_forward_message(group_id="30001", stream_id="other-stream")
    )
    await wait_for_background_tasks(non_source_plugin)
    assert non_source_plugin.ctx.message.calls == []
    assert non_source_plugin.ctx.events == []
    await non_source_plugin.on_unload()


@pytest.mark.asyncio
async def test_source_trigger_skips_self_and_known_forward_echoes(tmp_path: Path) -> None:
    """验证账号来源标记和目标消息 ID 都能阻止机器人转发回声触发。

    第一条消息的发送者与适配器 ``self_id`` 相同，应在解析阶段忽略；第二条
    消息模拟插件此前发送到同一个 source/target 重叠群的真实目标消息 ID，
    即使来源标记缺失也应由转发状态拦截。该测试防止机器人自己的跨群转发
    回声重新唤醒 source Planner 并形成循环。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于保存转发和触发状态。
    """

    plugin = build_plugin(tmp_path, trigger_source_planner=True)
    await plugin.on_load()

    await plugin.trigger_source_planner_for_forward_message(
        message=build_forward_message(sender_id="99999", self_id="99999")
    )
    echo_job = ForwardJob(
        job_id="echo-job",
        state_key=plugin.runtime.state.build_state_key("origin-stream", "origin-message"),
        source_stream_id="origin-stream",
        source_group_id="40001",
        source_message_id="origin-message",
        target_group_ids=["10001"],
        forward_messages=[{"segments": [{"type": "text", "data": "回声"}]}],
        sharing_reason="测试回声",
    )
    await plugin.runtime.state.record_target_sent(echo_job, "10001", "forward-message")
    await plugin.trigger_source_planner_for_forward_message(message=build_forward_message(self_id=""))
    await wait_for_background_tasks(plugin)

    assert plugin.ctx.message.calls == []
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_source_trigger_is_permanently_deduplicated_across_reload(tmp_path: Path) -> None:
    """验证同一 source stream 与 msg_id 成功触发后跨重载仍只执行一次。

    首个实例连续观察两次同一消息时只应安排一个任务，成功后状态文件保存
    一个匿名稳定键；第二个实例加载同一数据目录后再次观察仍不得查询消息或
    触发 Planner。该测试防止 Hook 重复投递或插件重载打破永久防重。

    Args:
        tmp_path: pytest 提供且在两个插件实例之间复用的持久化目录。
    """

    first_plugin = build_plugin(tmp_path, trigger_source_planner=True)
    await first_plugin.on_load()
    message = build_forward_message()
    await first_plugin.trigger_source_planner_for_forward_message(message=message)
    await first_plugin.trigger_source_planner_for_forward_message(message=message)
    await wait_for_background_tasks(first_plugin)
    assert first_plugin.ctx.events == [("planner", "source-stream")]
    await first_plugin.on_unload()

    state_payload = json.loads((tmp_path / "source_planner_trigger_state.json").read_text(encoding="utf-8"))
    assert state_payload["version"] == 1
    assert len(state_payload["triggered_keys"]) == 1
    assert "source-stream" not in state_payload["triggered_keys"][0]
    assert "forward-message" not in state_payload["triggered_keys"][0]

    reloaded_plugin = build_plugin(tmp_path, trigger_source_planner=True)
    await reloaded_plugin.on_load()
    await reloaded_plugin.trigger_source_planner_for_forward_message(message=message)
    await wait_for_background_tasks(reloaded_plugin)
    assert reloaded_plugin.ctx.message.calls == []
    assert reloaded_plugin.ctx.events == []
    await reloaded_plugin.on_unload()
