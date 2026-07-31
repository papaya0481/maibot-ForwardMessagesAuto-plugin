"""插件入口、组件声明和 Hook 接入的回归测试。"""

from __future__ import annotations

from copy import deepcopy
import logging
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

from forward_messages_auto.core.streams import GroupStreamRegistry
from forward_messages_auto.source.authorization import FORWARD_TOOL_NAME
from forward_messages_auto.source.models import ViewEligibilityStatus
from plugin import ForwardMessagesAutoPlugin
from tests.support import FakeChatCapability, build_plugin


def test_plugin_loads_with_runner_package_layout() -> None:
    """验证入口能按 MaiBot Runner 的合成包布局完成隔离加载。

    子进程仅把 ``plugins/`` 父目录加入导入路径，并使用
    ``submodule_search_locations`` 将 ``plugin.py`` 注册为合成包。期望入口
    及内部业务子包均能导入且工厂返回插件实例；该测试防止重新使用
    ``from forward_messages_auto`` 顶层导入而导致 Host 启动失败。
    """

    plugin_dir = Path(__file__).resolve().parents[1]
    script = """
import importlib.util
import pathlib
import sys

plugin_dir = pathlib.Path(sys.argv[1]).resolve()
sys.path = [
    str(plugin_dir.parent),
    *[
        entry
        for entry in sys.path
        if entry and pathlib.Path(entry).resolve() != plugin_dir
    ],
]
module_name = "_maibot_plugin_papaya0481_forward_messages_auto"
spec = importlib.util.spec_from_file_location(
    module_name,
    plugin_dir / "plugin.py",
    submodule_search_locations=[str(plugin_dir)],
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)
assert module.create_plugin().__class__.__name__ == "ForwardMessagesAutoPlugin"
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(plugin_dir)],
        cwd=plugin_dir.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_forward_tool_component_is_deferred_group_scoped_and_has_long_timeout() -> None:
    """验证自主转发 Tool 的发现方式、群聊范围和执行超时。

    期望组件顶层 ``chat_scope`` 为 ``group``，将 ``visibility`` 声明为
    ``deferred``，并为真实顺序投递保留两分钟 RPC 时间。该测试防止工具
    重新全量暴露给 Planner、被私聊调用或退回默认短超时，不校验任何
    Planner 可见文本。
    """

    plugin = ForwardMessagesAutoPlugin()
    plugin.set_plugin_config({})
    component = next(item for item in plugin.get_components() if item["name"] == FORWARD_TOOL_NAME)
    assert component["chat_scope"] == "group"
    assert component["metadata"]["visibility"] == "deferred"
    assert component["metadata"]["timeout_ms"] == 120000


def test_context_authorization_hook_precedes_async_orchestration() -> None:
    """验证无 I/O 的会话授权先于可能失败的自动查看编排。

    两个处理器必须订阅同一 ``after_response`` Hook，但上下文清洗与一次性
    授权位于 EARLY，包含 capability 预检的路径 B 编排位于 LATE。该测试
    防止未来合并或调换顺序，使异步预检超时后 Host 重新执行未清洗调用。
    """

    plugin = ForwardMessagesAutoPlugin()
    plugin.set_plugin_config({})
    components = {item["name"]: item for item in plugin.get_components()}
    authorization = components["authorize_forward_request_context"]
    orchestration = components["orchestrate_view_before_forward"]
    assert authorization["metadata"]["hook"] == "maisaka.planner.after_response"
    assert orchestration["metadata"]["hook"] == "maisaka.planner.after_response"
    assert authorization["metadata"]["order"] == "early"
    assert orchestration["metadata"]["order"] == "late"
    assert orchestration["metadata"]["timeout_ms"] == 6000


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


@pytest.mark.asyncio
async def test_hook_syncs_view_eligibility_without_rewriting_tool_definitions(tmp_path: Path) -> None:
    """验证 Hook 同步上下文资格、保持工具定义并可靠维持续轮提醒。

    即使启动阶段聊天流列表为空，当前会话仍应登记 ``forward-message`` 的
    完整展开内容，且工具定义保持原样。Planner 正常响应前重复请求应继续
    收到判断提醒，响应后旧历史不再触发提醒；另一会话的资格必须隔离，
    当前上下文移除查看结果后资格必须消失。该测试防止新消息打断导致判断
    任务丢失、旧结果反复提醒或上下文外结果继续生效。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于插件加载和卸载状态隔离。
    """

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

    plugin.ctx.chat.streams.clear()
    current_messages = deepcopy(messages)
    current_result = await plugin.capture_view_forward_result(
        session_id="current-stream",
        messages=current_messages,
        tool_definitions=definitions,
    )
    current_lookup = plugin.runtime.view_eligibility.lookup(
        "current-stream",
        "forward-message",
    )
    assert current_lookup.status is ViewEligibilityStatus.READY
    assert current_lookup.content == "完整展开内容"
    assert current_result["modified_kwargs"]["tool_definitions"] == definitions
    assert len(current_messages) == len(messages) + 1
    assert current_messages[-1]["role"] == "user"
    assert isinstance(current_messages[-1]["content"], str)
    assert current_messages[-1]["content"]

    interrupted_retry_messages = deepcopy(messages)
    await plugin.capture_view_forward_result(
        session_id="current-stream",
        messages=interrupted_retry_messages,
        tool_definitions=definitions,
    )
    assert len(interrupted_retry_messages) == len(messages) + 1
    assert interrupted_retry_messages[-1]["role"] == "user"
    assert isinstance(interrupted_retry_messages[-1]["content"], str)
    assert interrupted_retry_messages[-1]["content"]

    await plugin.orchestrate_view_before_forward(
        session_id="current-stream",
        response="我会现在判断",
        tool_calls=[],
    )
    acknowledged_messages = deepcopy(messages)
    await plugin.capture_view_forward_result(
        session_id="current-stream",
        messages=acknowledged_messages,
        tool_definitions=definitions,
    )
    assert acknowledged_messages == messages

    other_messages = deepcopy(messages)
    other_result = await plugin.capture_view_forward_result(
        session_id="target-a",
        messages=other_messages,
        tool_definitions=definitions,
    )
    assert (
        plugin.runtime.view_eligibility.lookup(
            "target-a",
            "forward-message",
        ).status
        is ViewEligibilityStatus.READY
    )
    assert other_result["modified_kwargs"]["tool_definitions"] == definitions

    await plugin.capture_view_forward_result(
        session_id="current-stream",
        messages=[],
        tool_definitions=definitions,
    )
    assert (
        plugin.runtime.view_eligibility.lookup(
            "current-stream",
            "forward-message",
        ).status
        is ViewEligibilityStatus.MISSING
    )
    await plugin.on_unload()
