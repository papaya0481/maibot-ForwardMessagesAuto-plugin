from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forward_messages_auto.config import GroupIdList
from forward_messages_auto.config_recovery import LastKnownGoodConfig
from forward_messages_auto.models import ViewEligibilityStatus, ViewObservationKind
from forward_messages_auto.parsing import (
    ForwardMessageParser,
    PlannerHistoryParser,
    ViewToolObservation,
)
from forward_messages_auto.runtime import FORWARD_TOOL_NAME
from forward_messages_auto.streams import GroupStreamRegistry
from forward_messages_auto.view_context import ViewEligibilityStore
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
    target_groups: list[str] | None = None,
    view_failure_fallback_threshold: int = 2,
) -> ForwardMessagesAutoPlugin:
    """构造启用状态下、带一个 source 和两个 target 的插件。

    Args:
        tmp_path: pytest 提供的临时目录，用于隔离持久化状态文件。
        failed_streams: 应由发送能力模拟失败的目标聊天流集合。
        failed_context_streams: 应由上下文能力模拟失败的目标聊天流集合。
        target_groups: 可选的 target 群号配置；省略时使用两个默认目标群。
        view_failure_fallback_threshold: 允许摘要或预览降级前，同一消息需要
            连续累计的可重试查看故障次数。

    Returns:
        已注入强类型配置和 ``FakeContext``、但尚未调用 ``on_load`` 的插件。
    """

    plugin = ForwardMessagesAutoPlugin()
    plugin.set_plugin_config(
        {
            "plugin": {
                "enabled": True,
                "version": "0.1.12",
                "config_version": "0.1.4",
            },
            "routing": {
                "source_groups": ["10001"],
                "target_groups": (target_groups if target_groups is not None else ["20001", "20002"]),
            },
            "behavior": {
                "view_failure_fallback_threshold": view_failure_fallback_threshold,
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


def seed_successful_view(
    plugin: ForwardMessagesAutoPlugin,
    *,
    stream_id: str = "source-stream",
    message_id: str = "forward-message",
    content: str = "完整展开内容",
) -> None:
    """为与查看流程无关的测试同步一条当前上下文成功查看资格。

    Args:
        plugin: 已执行 ``on_load`` 并初始化运行时的插件实例。
        stream_id: 查看资格所属的 source 聊天流 ID。
        message_id: 已经完整查看的合并转发消息 ID。
        content: 写入目标群上下文的完整查看文本。
    """

    plugin.runtime.view_eligibility.sync_context(
        stream_id,
        [
            ViewToolObservation(
                call_id=f"seed-{message_id}",
                message_id=message_id,
                content=content,
                kind=ViewObservationKind.SUCCESS,
            )
        ],
    )


def sync_failed_views(
    plugin: ForwardMessagesAutoPlugin,
    kinds: list[ViewObservationKind],
    *,
    stream_id: str = "source-stream",
    message_id: str = "forward-message",
) -> None:
    """把指定失败序列同步为 Planner 当前上下文的查看观察。

    Args:
        plugin: 已执行 ``on_load`` 并初始化运行时的插件实例。
        kinds: 按当前上下文顺序排列的失败分类列表。
        stream_id: 失败观察所属的 source 聊天流 ID。
        message_id: 查看失败对应的合并转发消息 ID。
    """

    plugin.runtime.view_eligibility.sync_context(
        stream_id,
        [
            ViewToolObservation(
                call_id=f"failure-{index}",
                message_id=message_id,
                content=f"分类失败 {index}",
                kind=kind,
            )
            for index, kind in enumerate(kinds, start=1)
        ],
    )


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


def test_legacy_ttl_config_fields_are_removed_during_normalization() -> None:
    """验证旧版两个 TTL 字段不会继续进入新配置。

    将 `v0.1.11` 的查看缓存和去重 TTL 字段加入当前默认配置后执行 SDK
    归一化，结果应删除两个未知字段并保留其他行为配置。该测试防止升级后
    WebUI 继续展示已经失效的过期设计。
    """

    plugin = ForwardMessagesAutoPlugin()
    legacy_config = plugin.get_default_config()
    legacy_config["behavior"]["view_cache_ttl_seconds"] = 1800
    legacy_config["behavior"]["dedupe_ttl_seconds"] = 604800

    normalized_config, _changed = plugin.normalize_plugin_config(legacy_config)

    assert "view_cache_ttl_seconds" not in normalized_config["behavior"]
    assert "dedupe_ttl_seconds" not in normalized_config["behavior"]
    assert normalized_config["behavior"]["view_failure_fallback_threshold"] == 2


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
    防止查看资格把结果关联到错误 source 消息。
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


@pytest.mark.parametrize(
    ("content", "expected_kind"),
    [
        (
            "未找到目标转发消息，msg_id=missing",
            ViewObservationKind.CORRECTABLE_FAILURE,
        ),
        (
            "目标消息不是可展开查看的转发消息，msg_id=plain",
            ViewObservationKind.TERMINAL_FAILURE,
        ),
        (
            "查看转发消息完整内容时发生异常。",
            ViewObservationKind.RETRYABLE_FAILURE,
        ),
        (
            "转发消息内容为空，msg_id=empty",
            ViewObservationKind.EMPTY_CONTENT_FAILURE,
        ),
        (
            "工具 view_forward_message 执行失败。",
            ViewObservationKind.UNKNOWN_FAILURE,
        ),
    ],
)
def test_view_result_parser_classifies_known_host_failures(
    content: str,
    expected_kind: ViewObservationKind,
) -> None:
    """验证已知 Host 失败文本会被分类而不是登记为完整内容。

    每种稳定错误前缀都应映射为可修正、终止、可重试、空内容或未知失败，
    同时成功结果接口返回空列表。该测试防止错误文本写入目标群上下文，也
    防止不可恢复错误被误计入可重试阈值。

    Args:
        content: pytest 参数化提供的 Host ToolResult 文本。
        expected_kind: pytest 参数化提供的预期失败分类。
    """

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "failed-call",
                    "function": {
                        "name": "view_forward_message",
                        "arguments": {"msg_id": "message-1"},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": content,
            "tool_call_id": "failed-call",
        },
    ]

    observations = PlannerHistoryParser.extract_view_observations(messages)
    assert len(observations) == 1
    assert observations[0].call_id == "failed-call"
    assert observations[0].message_id == "message-1"
    assert observations[0].kind is expected_kind
    assert PlannerHistoryParser.extract_view_results(messages) == []


def test_view_eligibility_follows_current_context_and_classifies_failures() -> None:
    """验证查看资格严格跟随当前上下文且失败计数按末尾分类计算。

    成功结果在同一上下文中重复同步后仍应有效但不重复报告为新结果；下一轮
    上下文移除该结果后资格必须立即消失。两个连续可重试故障应计为两次，
    后接可修正错误后计数应清零。该测试防止重新引入时间过期或旧索引残留，
    也防止不同失败类型拼接成降级资格。
    """

    store = ViewEligibilityStore()
    success = ViewToolObservation(
        call_id="success-call",
        message_id="message-1",
        content="完整内容",
        kind=ViewObservationKind.SUCCESS,
    )
    assert store.sync_context("stream-1", [success]) == ["message-1"]
    assert store.sync_context("stream-1", [success]) == []
    ready_lookup = store.lookup("stream-1", "message-1")
    assert ready_lookup.status is ViewEligibilityStatus.READY
    assert ready_lookup.content == "完整内容"

    store.sync_context(
        "stream-1",
        [
            success,
            ViewToolObservation(
                call_id="later-failure",
                message_id="message-1",
                content="稍后的查看失败",
                kind=ViewObservationKind.RETRYABLE_FAILURE,
            ),
        ],
    )
    assert store.lookup("stream-1", "message-1").status is ViewEligibilityStatus.READY

    store.sync_context("stream-1", [])
    assert store.lookup("stream-1", "message-1").status is ViewEligibilityStatus.MISSING

    store.sync_context(
        "stream-1",
        [
            ViewToolObservation(
                call_id=f"failure-{index}",
                message_id="message-2",
                content="查看异常",
                kind=ViewObservationKind.RETRYABLE_FAILURE,
            )
            for index in range(1, 3)
        ],
    )
    failed_lookup = store.lookup("stream-1", "message-2")
    assert failed_lookup.status is ViewEligibilityStatus.MISSING
    assert failed_lookup.retryable_failure_count == 2

    store.sync_context(
        "stream-1",
        [
            ViewToolObservation(
                call_id="failure-1",
                message_id="message-2",
                content="查看异常",
                kind=ViewObservationKind.RETRYABLE_FAILURE,
            ),
            ViewToolObservation(
                call_id="failure-3",
                message_id="message-2",
                content="消息不存在",
                kind=ViewObservationKind.CORRECTABLE_FAILURE,
            ),
        ],
    )
    corrected_lookup = store.lookup("stream-1", "message-2")
    assert corrected_lookup.retryable_failure_count == 0
    assert corrected_lookup.last_observation_kind is ViewObservationKind.CORRECTABLE_FAILURE


def test_forward_tool_component_is_deferred_and_group_scoped() -> None:
    """验证自主转发 Tool 的 deferred 声明、群聊范围和首次暴露描述。

    期望组件顶层 ``chat_scope`` 为 ``group``，同时将 ``visibility`` 明确
    声明为 ``deferred``；简要描述应提示 Planner 根据 ``msg_id`` 分享有意思、
    符合人设且值得转发的内容，不暴露内部目标白名单。该测试防止工具重新
    全量暴露给 Planner、被私聊调用，或首次暴露的用途说明发生语义回退。
    """

    plugin = ForwardMessagesAutoPlugin()
    plugin.set_plugin_config({})
    component = next(item for item in plugin.get_components() if item["name"] == FORWARD_TOOL_NAME)
    assert component["chat_scope"] == "group"
    assert component["metadata"]["visibility"] == "deferred"
    brief_description = component["metadata"]["brief_description"]
    assert brief_description == (
        "根据 msg_id，将已经完整查看且你觉得有意思、符合人设、值得转发的合并转发消息分享到其他群聊。"
    )
    assert "白名单" not in brief_description


@pytest.mark.asyncio
async def test_group_stream_registry_resolves_targets_on_demand() -> None:
    """验证聊天流注册表只在实际投递时按需解析目标群。

    已有 target 应通过 ``get_stream_by_group_id`` 查询，未知 target 应通过
    ``open_session`` 创建。该测试防止重新引入依赖启动期全量聊天流快照的
    时序竞态，也防止把 SDK 解包后的聊天流字典误判为失败。
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
    assert current_messages[-1]["role"] == "user"
    assert "不要等待下一条聊天消息后再作判断" in current_messages[-1]["content"]

    interrupted_retry_messages = deepcopy(messages)
    await plugin.capture_view_forward_result(
        session_id="current-stream",
        messages=interrupted_retry_messages,
        tool_definitions=definitions,
    )
    assert "不要等待下一条聊天消息后再作判断" in interrupted_retry_messages[-1]["content"]

    await plugin.acknowledge_view_forward_judgment(
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


@pytest.mark.asyncio
async def test_tool_rejects_unviewed_and_single_failure_before_fallback(tmp_path: Path) -> None:
    """验证当前上下文没有成功查看和首次失败都不会静默降级转发。

    未查看时即使提供摘要也应拒绝任务；登记一次已知失败后仍应要求 Planner
    再次查看。第二个独立查看调用也失败后，才允许使用摘要创建转发任务。
    该测试防止暂时性缺失绕过完整查看要求。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于运行真实请求和后台投递。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()

    missing_result = await plugin.request_cross_group_forward(
        "forward-message",
        content_summary="不应立即使用的摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert missing_result["success"] is False
    assert "尚未取得成功" in missing_result["content"]
    assert plugin.ctx.events == []

    sync_failed_views(plugin, [ViewObservationKind.RETRYABLE_FAILURE])
    first_failure_result = await plugin.request_cross_group_forward(
        "forward-message",
        content_summary="仍不应使用的摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert first_failure_result["success"] is False
    assert "配置阈值 2 次" in first_failure_result["content"]
    assert plugin.ctx.events == []

    sync_failed_views(
        plugin,
        [
            ViewObservationKind.RETRYABLE_FAILURE,
            ViewObservationKind.RETRYABLE_FAILURE,
        ],
    )
    fallback_result = await plugin.request_cross_group_forward(
        "forward-message",
        content_summary="两次失败后的降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert fallback_result["success"] is True
    assert fallback_result["accepted"] is True
    await wait_for_background_tasks(plugin)
    assert "两次失败后的降级摘要" in plugin.ctx.maisaka.context.visible_text_by_stream["target-a"]
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

    before_threshold = await plugin.request_cross_group_forward(
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
    at_threshold = await plugin.request_cross_group_forward(
        "forward-message",
        content_summary="三次失败后的降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert at_threshold["success"] is True
    assert at_threshold["accepted"] is True
    await wait_for_background_tasks(plugin)
    assert "三次失败后的降级摘要" in plugin.ctx.maisaka.context.visible_text_by_stream["target-a"]
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

    result = await plugin.request_cross_group_forward(
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

    first_result = await plugin.request_cross_group_forward(
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
    second_result = await plugin.request_cross_group_forward(
        "forward-message",
        content_summary="连续空内容后的降级摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )

    assert second_result["success"] is True
    assert second_result["accepted"] is True
    await wait_for_background_tasks(plugin)
    assert "连续空内容后的降级摘要" in plugin.ctx.maisaka.context.visible_text_by_stream["target-a"]
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

    result = await plugin.request_cross_group_forward(
        "forward-message",
        content_summary="不应使用的上下文外摘要",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["success"] is False
    assert "请先完成查看" in result["content"]
    assert plugin.ctx.events == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_tool_sends_targets_in_order_and_deduplicates(tmp_path: Path) -> None:
    """验证多目标严格按阶段顺序处理，并对完成任务执行去重。

    首次请求应依次产生 target-a 的 send/context/planner，再处理 target-b，
    且上下文复用源群当前查看结果。第二次相同请求应返回 ``accepted=False``，发送
    事件总数保持为二。该测试防止并行或乱序投递、重复媒体读取和重复转发。

    Args:
        tmp_path: pytest 提供的临时目录，用于检查 ``forward_state.json``。
    """

    plugin = build_plugin(tmp_path)
    await plugin.on_load()
    seed_successful_view(plugin)

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
    first_result = await first_plugin.request_cross_group_forward(
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
    expanded_result = await expanded_plugin.request_cross_group_forward(
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert expanded_result["accepted"] is True
    await wait_for_background_tasks(expanded_plugin)

    assert expanded_plugin.ctx.events == [
        ("send", "target-b"),
        ("context", "target-b"),
        ("planner", "target-b"),
    ]
    await expanded_plugin.on_unload()


@pytest.mark.asyncio
async def test_legacy_route_jobs_migrate_and_preserve_highest_target_stage(
    tmp_path: Path,
) -> None:
    """验证 v0.1.11 路由级状态会合并且不按旧时间清理。

    状态文件预置两个具有极早时间戳的旧任务：target-a 已完成，target-b
    已发送但未写上下文。加载后请求应只恢复 target-b 的上下文和 Planner，
    不向任何 target 重复物理发送。该测试防止升级时丢失永久防重记录。

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
                "targets": {"20002": "sent"},
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
    result = await plugin.request_cross_group_forward(
        "forward-message",
        platform="qq",
        group_id="10001",
        stream_id="source-stream",
    )
    assert result["accepted"] is True
    await wait_for_background_tasks(plugin)

    assert plugin.ctx.events == [
        ("context", "target-b"),
        ("planner", "target-b"),
    ]
    await plugin.on_unload()
    saved_payload = json.loads((tmp_path / "forward_state.json").read_text(encoding="utf-8"))
    assert saved_payload["version"] == 2
    assert len(saved_payload["jobs"]) == 1


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
    plugin.ctx.message = FakeStaticMessageCapability({"success": False, "error": "消息不属于指定聊天流"})
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
    result = await plugin.request_cross_group_forward(
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
    result = await plugin.request_cross_group_forward(
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
    seed_successful_view(plugin)
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
    seed_successful_view(first_plugin)
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
    seed_successful_view(resumed_plugin)
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
