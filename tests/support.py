"""按测试职责共享的替身与构造辅助函数。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any


from forward_messages_auto.models import ViewObservationKind
from forward_messages_auto.parsing import (
    ViewToolObservation,
)
from plugin import ForwardMessagesAutoPlugin


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
        self.message_ids_by_stream: dict[str, str] = {}

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
            **kwargs: 发送选项；测试会断言历史同步和详细结果均已启用。

        Returns:
            目标位于 ``failed_streams`` 时返回不含消息 ID 的详细失败结果；
            否则返回带平台最终目标消息 ID 的详细成功结果。
        """

        assert kwargs["sync_to_maisaka_history"] is True
        assert kwargs["return_details"] is True
        self.events.append(("send", stream_id))
        self.messages_by_stream[stream_id] = messages
        if stream_id in self.failed_streams:
            return {"sent": False, "message_id": None, "error": "模拟发送失败"}
        message_id = f"sent-{stream_id}"
        self.message_ids_by_stream[stream_id] = message_id
        return {"sent": True, "message_id": message_id}


class BlockingSendCapability(FakeSendCapability):
    """在测试释放闸门前阻塞首次物理发送。"""

    def __init__(self, events: list[tuple[str, str]]) -> None:
        """创建带开始通知和释放闸门的发送替身。

        Args:
            events: 所有 Fake capability 共享的事件列表。
        """

        super().__init__(events)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def forward(
        self,
        messages: list[dict[str, Any]],
        stream_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """等待测试放行后执行父类发送逻辑。

        Args:
            messages: 传给 ``ctx.send.forward`` 的规范化转发节点。
            stream_id: 接收消息的目标聊天流 ID。
            **kwargs: 透传给父类替身的发送选项。

        Returns:
            闸门释放后父类生成的成功结果字典。
        """

        self.started.set()
        await self.release.wait()
        return await super().forward(messages, stream_id, **kwargs)


class FakeMaisakaProactiveCapability:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        """创建记录目标 Planner 触发顺序的能力替身。

        Args:
            events: 所有 Fake capability 共享的事件列表。
        """

        self.events = events
        self.intents_by_stream: dict[str, str] = {}
        self.metadata_by_stream: dict[str, dict[str, Any]] = {}

    async def trigger(self, stream_id: str, intent: str, **kwargs: Any) -> dict[str, Any]:
        """验证真实消息定位约束并模拟主动任务入队。

        Args:
            stream_id: 要触发 Planner 的目标聊天流 ID。
            intent: 投递服务生成的 Planner 意图文本。
            **kwargs: 主动任务的 reason、priority 和 metadata；测试保存
                metadata 以验证不会向目标 Planner 暴露源群消息 ID。

        Returns:
            包含 ``success=True``、``queued=True`` 和可预测任务 ID 的字典。
        """

        assert "目标消息 ID 是" in intent or "无法可靠定位时请保持沉默" in intent
        assert "完整内容已经写入当前上下文" not in intent
        metadata = kwargs.get("metadata")
        assert isinstance(metadata, dict)
        self.intents_by_stream[stream_id] = intent
        self.metadata_by_stream[stream_id] = metadata
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
        """组装插件测试所需的最小 ``PluginContext`` 替身。

        Args:
            data_dir: 状态文件和运行时目录使用的 pytest 临时路径。
            message: 消息查询能力应返回的 source 消息。
            streams: 初始 QQ 群聊流列表。
            failed_streams: 应模拟物理发送失败的聊天流集合。

        Note:
            ``events`` 在发送和 Planner 能力间共享，用于断言跨 target 的
            全局调用顺序。
        """

        self.logger = logging.getLogger("test.forward-plugin")
        self.paths = SimpleNamespace(data_dir=data_dir, runtime_dir=data_dir / "runtime")
        self.events: list[tuple[str, str]] = []
        self.message = FakeMessageCapability(message)
        self.chat = FakeChatCapability(streams)
        self.send = FakeSendCapability(self.events, failed_streams)
        self.maisaka = SimpleNamespace(
            proactive=FakeMaisakaProactiveCapability(self.events),
        )


def build_plugin(
    tmp_path: Path,
    *,
    failed_streams: set[str] | None = None,
    target_groups: list[str] | None = None,
    view_failure_fallback_threshold: int = 2,
) -> ForwardMessagesAutoPlugin:
    """构造启用状态下、带一个 source 和两个 target 的插件。

    Args:
        tmp_path: pytest 提供的临时目录，用于隔离持久化状态文件。
        failed_streams: 应由发送能力模拟失败的目标聊天流集合。
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
                "version": "0.1.17",
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
