from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forward_messages_auto.config import GroupIdList
from forward_messages_auto.config_recovery import LastKnownGoodConfig
from forward_messages_auto.parsing import ForwardMessageParser, PlannerHistoryParser
from forward_messages_auto.runtime import FORWARD_TOOL_NAME
from forward_messages_auto.streams import GroupStreamRegistry
from plugin import ForwardMessagesAutoPlugin


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


def build_forward_message(*, stream_id: str = "source-stream", group_id: str = "10001") -> dict[str, Any]:
    """构造包含文字、图片和二进制数据的 Host 合并转发消息。

    Args:
        stream_id: 消息所属的 MaiBot 聊天流 ID。
        group_id: 消息所属的 QQ 群号。

    Returns:
        可供 ``FakeMessageCapability`` 返回的消息字典。固定消息 ID 为
        ``forward-message``，并包含一个昵称为“群友甲”的转发节点。
    """

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
        """创建始终返回指定消息的查询能力替身。

        Args:
            message: 每次 ``get_by_id`` 返回的消息字典，模拟当前 SDK 对
                Host 成功响应的自动解包。
        """

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
        """记录消息查询参数并返回 SDK 解包后的预设消息。

        Args:
            message_id: 被查询的消息 ID。
            stream_id: 调用方用于限制消息归属的聊天流 ID。
            include_binary_data: 是否请求媒体二进制字段。
            **kwargs: 本测试替身忽略的其他 capability 参数。

        Returns:
            当前 SDK 解包后的 ``self.message`` 消息字典。
        """

        del kwargs
        self.calls.append((message_id, stream_id, include_binary_data))
        return self.message


class FakeStaticMessageCapability:
    def __init__(self, result: Any) -> None:
        """创建返回任意固定结果的消息能力替身。

        Args:
            result: ``get_by_id`` 应原样返回的结果，用于覆盖旧版包装、失败
                包装、空结果和非法格式。
        """

        self.result = result

    async def get_by_id(
        self,
        message_id: str,
        *,
        stream_id: str = "",
        include_binary_data: bool = False,
        **kwargs: Any,
    ) -> Any:
        """返回预设结果并校验请求保留消息范围和二进制数据参数。

        Args:
            message_id: 被查询的消息 ID，测试要求不能为空。
            stream_id: source 聊天流 ID，测试要求为 ``source-stream``。
            include_binary_data: 是否请求媒体二进制字段，测试要求为 ``True``。
            **kwargs: 本测试替身忽略的其他 capability 参数。

        Returns:
            构造实例时传入的固定结果。
        """

        del kwargs
        assert message_id
        assert stream_id == "source-stream"
        assert include_binary_data is True
        return self.result


class FakeChatCapability:
    def __init__(self, streams: list[dict[str, Any]]) -> None:
        """创建使用可变聊天流列表的能力替身。

        Args:
            streams: 初始 QQ 群聊流列表。``open_session`` 会向该列表追加
                新建会话，便于后续查询复用。
        """

        self.streams = streams

    async def get_group_streams(self, platform: str = "qq") -> list[dict[str, Any]]:
        """返回全部预设 QQ 群聊流。

        Args:
            platform: 待查询平台；测试要求调用方必须传入 ``"qq"``。

        Returns:
            原始 ``streams`` 列表，模拟 SDK 对成功响应的解包结果。
        """

        assert platform == "qq"
        return self.streams

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> dict[str, Any] | None:
        """按群号查找第一条匹配的预设聊天流。

        Args:
            group_id: 要查找的 QQ 群号。
            platform: 待查询平台；测试要求为 ``"qq"``。

        Returns:
            找到时返回原始聊天流字典，未找到时返回 ``None``，模拟 SDK 对成功
            响应的解包结果。
        """

        assert platform == "qq"
        stream = next((item for item in self.streams if item["group_id"] == group_id), None)
        return stream

    async def open_session(
        self,
        platform: str,
        chat_type: str,
        *,
        group_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """模拟为此前未知的 QQ 群创建聊天流。

        Args:
            platform: 会话平台，必须为 ``"qq"``。
            chat_type: 会话类型，必须为 ``"group"``。
            group_id: 要创建聊天流的 QQ 群号。
            **kwargs: 本测试替身忽略的其他建流参数。

        Returns:
            新聊天流字典。新 ID 使用 ``opened-<group_id>``，并同步追加到
            ``streams``，模拟 SDK 对成功响应的解包结果。
        """

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
        return stream


class FakeSendCapability:
    def __init__(self, events: list[tuple[str, str]], failed_streams: set[str] | None = None) -> None:
        """创建记录发送顺序并可定向失败的发送替身。

        Args:
            events: 所有 Fake capability 共享的事件列表。
            failed_streams: 应返回模拟发送失败的目标聊天流集合；省略时全部
                发送成功。
        """

        self.events = events
        self.failed_streams = failed_streams or set()
        self.messages_by_stream: dict[str, list[dict[str, Any]]] = {}

    async def forward(
        self,
        messages: list[dict[str, Any]],
        stream_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """记录一次合并转发调用并按配置返回成功或失败。

        Args:
            messages: 传给 ``ctx.send.forward`` 的规范化转发节点。
            stream_id: 接收消息的目标聊天流 ID。
            **kwargs: 发送选项；测试会断言
                ``sync_to_maisaka_history`` 为 ``False``。

        Returns:
            目标位于 ``failed_streams`` 时返回模拟失败字典，否则返回
            ``{"success": True}``。
        """

        assert kwargs["sync_to_maisaka_history"] is False
        self.events.append(("send", stream_id))
        self.messages_by_stream[stream_id] = messages
        if stream_id in self.failed_streams:
            return {"success": False, "error": "模拟发送失败"}
        return {"success": True}


class FakeMaisakaContextCapability:
    def __init__(
        self,
        events: list[tuple[str, str]],
        failed_streams: set[str] | None = None,
    ) -> None:
        """创建记录上下文写入并可定向失败的能力替身。

        Args:
            events: 所有 Fake capability 共享的事件列表。
            failed_streams: 应返回模拟上下文失败的聊天流集合。
        """

        self.events = events
        self.failed_streams = failed_streams or set()
        self.visible_text_by_stream: dict[str, str] = {}

    async def append(
        self,
        stream_id: str,
        segments: list[dict[str, Any]],
        *,
        visible_text: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """验证并记录一次目标群 Maisaka 上下文追加。

        Args:
            stream_id: 接收上下文的目标聊天流 ID。
            segments: 待写入的消息段；首段必须是 ``forward``。
            visible_text: 目标 Planner 可读取的源群展开文本。
            **kwargs: 本测试替身忽略的来源类型和消息 ID 等参数。

        Returns:
            目标位于 ``failed_streams`` 时返回模拟失败；否则保存
            ``visible_text`` 并返回成功字典。
        """

        del kwargs
        assert segments[0]["type"] == "forward"
        self.events.append(("context", stream_id))
        if stream_id in self.failed_streams:
            return {"success": False, "error": "模拟上下文失败"}
        self.visible_text_by_stream[stream_id] = visible_text
        return {"success": True}


class FakeMaisakaProactiveCapability:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        """创建记录目标 Planner 触发顺序的能力替身。

        Args:
            events: 所有 Fake capability 共享的事件列表。
        """

        self.events = events

    async def trigger(self, stream_id: str, intent: str, **kwargs: Any) -> dict[str, Any]:
        """验证无需重复查看的意图并模拟主动任务入队。

        Args:
            stream_id: 要触发 Planner 的目标聊天流 ID。
            intent: 投递服务生成的 Planner 意图文本。
            **kwargs: 本测试替身忽略的 reason、priority 和 metadata。

        Returns:
            包含 ``success=True``、``queued=True`` 和可预测任务 ID 的字典。
        """

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
        failed_context_streams: set[str] | None = None,
    ) -> None:
        """组装插件测试所需的最小 ``PluginContext`` 替身。

        Args:
            data_dir: 状态文件和运行时目录使用的 pytest 临时路径。
            message: 消息查询能力应返回的 source 消息。
            streams: 初始 QQ 群聊流列表。
            failed_streams: 应模拟物理发送失败的聊天流集合。
            failed_context_streams: 应模拟 Maisaka 上下文失败的聊天流集合。

        Note:
            ``events`` 在发送、上下文和 Planner 三种能力间共享，用于断言
            跨 target 的全局调用顺序。
        """

        self.logger = logging.getLogger("test.forward-plugin")
        self.paths = SimpleNamespace(data_dir=data_dir, runtime_dir=data_dir / "runtime")
        self.events: list[tuple[str, str]] = []
        self.message = FakeMessageCapability(message)
        self.chat = FakeChatCapability(streams)
        self.send = FakeSendCapability(self.events, failed_streams)
        self.maisaka = SimpleNamespace(
            context=FakeMaisakaContextCapability(self.events, failed_context_streams),
            proactive=FakeMaisakaProactiveCapability(self.events),
        )


def build_plugin(
    tmp_path: Path,
    *,
    failed_streams: set[str] | None = None,
    failed_context_streams: set[str] | None = None,
) -> ForwardMessagesAutoPlugin:
    """构造启用状态下、带一个 source 和两个 target 的插件。

    Args:
        tmp_path: pytest 提供的临时目录，用于隔离持久化状态文件。
        failed_streams: 应由发送能力模拟失败的目标聊天流集合。
        failed_context_streams: 应由上下文能力模拟失败的目标聊天流集合。

    Returns:
        已注入强类型配置和 ``FakeContext``、但尚未调用 ``on_load`` 的插件。
    """

    plugin = ForwardMessagesAutoPlugin()
    plugin.set_plugin_config(
        {
            "plugin": {
                "enabled": True,
                "version": "0.1.6",
                "config_version": "0.1.1",
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
            failed_context_streams=failed_context_streams,
        )
    )
    return plugin


async def wait_for_background_tasks(plugin: ForwardMessagesAutoPlugin) -> None:
    """等待当前快照中的全部后台投递任务结束。

    Args:
        plugin: 已加载并可能安排了转发任务的插件实例。

    Note:
        方法先复制任务集合，避免任务完成回调在等待期间修改原集合。
    """

    tasks = list(plugin.runtime.background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def test_normalize_group_ids_preserves_order_and_deduplicates() -> None:
    """验证群号清洗会规范类型、去空、去重并保持首次顺序。

    输入同时覆盖带空格字符串、整数、重复项、空字符串和 ``None``，期望
    返回 ``["100", "200", "300"]``。该测试防止路由清洗改变 target
    投递顺序，或因配置值类型不同产生重复投递。
    """

    assert GroupIdList.normalize([" 100 ", 200, "100", "", None, "300"]) == [
        "100",
        "200",
        "300",
    ]


def test_invalid_toml_reuses_last_valid_config(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """验证保存中的无效 TOML 不会把已启用插件归一化为默认关闭状态。

    首次归一化保存启用状态和白名单，随后让磁盘文件停留在未闭合数组并让
    Runner 按实际预处理路径传入完整默认配置。期望仍返回旧配置且记录警告；
    该测试防止 Host 把短暂解析失败误判为 ``plugin.enabled=false`` 并卸载
    插件。

    Args:
        tmp_path: pytest 提供的临时目录，用于模拟插件 ``config.toml``。
        caplog: pytest 日志捕获 fixture，用于确认恢复路径对用户可观察。
    """

    config_path = tmp_path / "config.toml"
    plugin = ForwardMessagesAutoPlugin()
    plugin._config_recovery = LastKnownGoodConfig(config_path)
    plugin._set_context(SimpleNamespace(logger=logging.getLogger("test.config-recovery")))
    valid_config = plugin.get_default_config()
    valid_config["plugin"]["enabled"] = True
    valid_config["routing"]["source_groups"] = ["10001"]
    valid_config["routing"]["target_groups"] = ["20001"]
    normalized_config, _changed = plugin.normalize_plugin_config(valid_config)
    config_path.write_text('[routing]\nsource_groups = [\n  "10001"\n', encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        recovered_config, changed = plugin.normalize_plugin_config(plugin.get_default_config())

    assert recovered_config == normalized_config
    assert recovered_config["plugin"]["enabled"] is True
    assert recovered_config["routing"]["source_groups"] == ["10001"]
    assert changed is False
    assert "继续使用最近一次有效配置" in caplog.text


def test_deleted_config_uses_sdk_defaults_instead_of_snapshot(tmp_path: Path) -> None:
    """验证配置重置删除文件后不会错误恢复最近有效快照。

    先记住一份启用配置但不创建磁盘文件，再用完整默认配置模拟 Runner 的
    重置事件。期望 SDK 返回默认关闭且空白名单；该测试防止容错逻辑破坏
    WebUI“重置配置”的既有语义。

    Args:
        tmp_path: pytest 提供的临时目录，用于提供一个不存在的配置路径。
    """

    plugin = ForwardMessagesAutoPlugin()
    plugin._config_recovery = LastKnownGoodConfig(tmp_path / "config.toml")
    valid_config = plugin.get_default_config()
    valid_config["plugin"]["enabled"] = True
    valid_config["routing"]["source_groups"] = ["10001"]
    plugin.normalize_plugin_config(valid_config)

    reset_config, _changed = plugin.normalize_plugin_config(plugin.get_default_config())

    assert reset_config["plugin"]["enabled"] is False
    assert reset_config["routing"]["source_groups"] == []


def test_extract_forward_payload_preserves_nodes_and_binary_data() -> None:
    """验证合并转发解析同时生成上下文段和发送节点。

    期望解析结果保留 ``forward`` 类型、节点昵称以及图片
    ``binary_data_base64``。该测试防止消息规范化过程中丢失发送者信息或
    媒体二进制数据，导致目标群收到不完整内容。
    """

    payload = ForwardMessageParser.extract(build_forward_message())
    assert payload is not None
    segment, messages = payload
    assert segment["type"] == "forward"
    assert messages[0]["nickname"] == "群友甲"
    assert messages[0]["segments"][1]["binary_data_base64"] == "aW1hZ2U="


def test_extract_view_forward_results_pairs_tool_call_and_result() -> None:
    """验证 Planner 历史按工具调用 ID 配对消息 ID 与完整结果。

    assistant 消息声明 ``call-1`` 查看 ``message-1``，tool 消息再引用同一
    ID。期望返回唯一二元组 ``("message-1", "完整展开内容")``。该测试
    防止缓存把查看结果关联到错误 source 消息。
    """

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
    assert PlannerHistoryParser.extract_view_results(messages) == [("message-1", "完整展开内容")]


def test_forward_tool_component_is_visible_only_in_group_scope() -> None:
    """验证公开自主转发 Tool 的组件元数据只允许群聊调用。

    期望组件顶层 ``chat_scope`` 为 ``group``，同时保持 ``visibility`` 为
    ``visible``。该测试防止 SDK 元数据重构后 Tool 被私聊调用，或无法被
    source Planner 发现。
    """

    plugin = ForwardMessagesAutoPlugin()
    plugin.set_plugin_config({})
    component = next(item for item in plugin.get_components() if item["name"] == FORWARD_TOOL_NAME)
    assert component["chat_scope"] == "group"
    assert component["metadata"]["visibility"] == "visible"


@pytest.mark.asyncio
async def test_group_stream_registry_accepts_sdk_unwrapped_chat_results() -> None:
    """验证聊天流索引兼容 SDK 解包后的列表和聊天流字典。

    SDK 会把群聊列表响应解包为 ``list``，并把单个聊天流响应解包为字典。
    刷新后 source 会话必须可识别；未预加载的已有 target 必须能被按群号
    查询，未知 target 必须能创建会话。该测试防止把正常 SDK 返回值误判为
    格式错误，或在实际投递时错误地重复创建聊天流。
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
    await registry.refresh(["10001"], enabled=True)
    assert registry.is_source_stream("source-stream") is True

    on_demand_registry = GroupStreamRegistry(context)
    assert await on_demand_registry.resolve("20001") == "target-a"
    assert await on_demand_registry.resolve("30001") == "opened-30001"


@pytest.mark.asyncio
async def test_hook_caches_view_result_and_hides_tool_outside_source(tmp_path: Path) -> None:
    """验证 Hook 仅在 source 缓存结果，并在其他会话隐藏 Tool。

    source 会话应缓存 ``forward-message`` 的完整展开内容且保留全部工具；
    target 会话应只保留无关的 ``reply`` 工具。该测试防止查看缓存跨会话
    污染，也防止非白名单群获得自主转发入口。

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

    source_result = await plugin.capture_view_forward_result(
        session_id="source-stream",
        messages=messages,
        tool_definitions=definitions,
    )
    assert plugin.runtime.view_cache.get("source-stream", "forward-message", 1800) == "完整展开内容"
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
    """验证多目标严格按阶段顺序处理，并对完成任务执行去重。

    首次请求应依次产生 target-a 的 send/context/planner，再处理 target-b，
    且上下文复用源群缓存。第二次相同请求应返回 ``accepted=False``，发送
    事件总数保持为二。该测试防止并行或乱序投递、重复媒体读取和重复转发。

    Args:
        tmp_path: pytest 提供的临时目录，用于检查 ``forward_state.json``。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    plugin.runtime.view_cache.put(
        "source-stream",
        "forward-message",
        "完整展开内容",
        now=10**12,
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
async def test_source_message_query_accepts_legacy_wrapped_success(tmp_path: Path) -> None:
    """验证源消息查询仍兼容旧版 SDK 的成功包装结果。

    将消息 capability 替换为返回 ``success/message`` 包装的旧版替身后，
    Tool 仍应接受任务。该测试防止修复当前 SDK 自动解包格式时破坏旧版
    Host 或测试环境兼容性。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

    plugin = build_plugin(tmp_path)
    plugin.ctx.message = FakeStaticMessageCapability(
        {"success": True, "message": build_forward_message()}
    )
    await plugin.on_load()
    result = await plugin.request_cross_group_forward(
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
    plugin.ctx.message = FakeStaticMessageCapability(
        {"success": False, "error": "消息不属于指定聊天流"}
    )
    await plugin.on_load()
    result = await plugin.request_cross_group_forward(
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
    result = await plugin.request_cross_group_forward(
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
async def test_send_failure_skips_context_but_continues_next_target(tmp_path: Path) -> None:
    """验证单个 target 发送失败不会污染上下文或中断后续 target。

    target-a 模拟物理发送失败，期望不出现其 context/planner 事件；target-b
    仍应完成 send/context/planner。该测试防止失败消息被错误写入 Maisaka
    上下文，也防止一个群的故障阻塞整个路由。

    Args:
        tmp_path: pytest 提供的隔离状态目录。
    """

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
async def test_persisted_stage_resumes_without_sending_again(tmp_path: Path) -> None:
    """验证插件重载后从持久化阶段继续而不重复物理发送。

    第一个实例让 target-a 发送成功但上下文失败，同时完成 target-b；第二个
    实例读取同一状态目录后，应只为 target-a 重试 context/planner，不再
    产生 send 事件。该测试防止进程重启或临时 capability 故障导致群内
    重复消息。

    Args:
        tmp_path: 两个插件实例共享的 pytest 临时状态目录。
    """

    first_plugin = build_plugin(tmp_path, failed_context_streams={"target-a"})
    await first_plugin.on_load()
    first_result = await first_plugin.request_cross_group_forward(
        "forward-message",
        content_summary="降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert first_result["accepted"] is True
    await wait_for_background_tasks(first_plugin)
    assert first_plugin.ctx.events[:3] == [
        ("send", "target-a"),
        ("context", "target-a"),
        ("send", "target-b"),
    ]
    await first_plugin.on_unload()

    resumed_plugin = build_plugin(tmp_path)
    await resumed_plugin.on_load()
    resumed_result = await resumed_plugin.request_cross_group_forward(
        "forward-message",
        content_summary="降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert resumed_result["accepted"] is True
    await wait_for_background_tasks(resumed_plugin)
    assert resumed_plugin.ctx.events == [
        ("context", "target-a"),
        ("planner", "target-a"),
    ]
    await resumed_plugin.on_unload()


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
