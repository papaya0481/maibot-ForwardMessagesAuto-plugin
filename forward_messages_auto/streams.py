"""QQ 群号与 MaiBot 聊天流的映射。"""

from __future__ import annotations

from typing import Any


class GroupStreamRegistry:
    """按需解析 QQ 群号对应的聊天流，并缓存成功结果。"""

    def __init__(self, context: Any) -> None:
        """创建绑定到当前插件上下文的空聊天流缓存。

        Args:
            context: MaiBot 注入的 ``PluginContext``。该服务使用其中的
                ``chat`` capability 查询或创建聊天流，并使用 ``logger``
                记录可恢复错误。
        """

        self._ctx = context
        self._streams_by_group: dict[str, str] = {}

    async def resolve(self, group_id: str) -> str:
        """按 QQ 群号解析目标聊天流，必要时创建群聊会话。

        解析顺序为：读取内存缓存、调用 ``get_stream_by_group_id``、最后调用
        ``open_session``。SDK 会将这两项 capability 的成功响应解包为聊天流
        字典，本方法也兼容旧版成功包装结构。查询失败允许降级到创建；创建
        失败会记录日志。该流程只在实际投递时运行，不依赖插件启动阶段的
        全量聊天流快照。

        Args:
            group_id: target 白名单中的 QQ 群号字符串。

        Returns:
            成功时返回目标 ``stream_id``，无法查询或创建时返回空字符串。
        """

        if stream_id := self._streams_by_group.get(group_id):
            return stream_id

        try:
            result = await self._ctx.chat.get_stream_by_group_id(group_id, platform="qq")
        except Exception as exc:
            self._ctx.logger.debug("按群号查询聊天流失败: group=%s error=%s", group_id, exc)
            result = None
        stream_id = self._extract_stream_id(result)
        if stream_id:
            self._streams_by_group[group_id] = stream_id
            return stream_id

        try:
            result = await self._ctx.chat.open_session(
                platform="qq",
                chat_type="group",
                group_id=group_id,
            )
        except Exception as exc:
            self._ctx.logger.warning("创建目标群聊天流失败: group=%s error=%s", group_id, exc)
            return ""
        stream_id = self._extract_stream_id(result)
        if stream_id:
            self._streams_by_group[group_id] = stream_id
        return stream_id

    @staticmethod
    def _extract_stream_id(result: Any) -> str:
        """从聊天 capability 返回值中读取聊天流 ID。

        Args:
            result: ``get_stream_by_group_id`` 或 ``open_session`` 的返回值。
                支持 SDK 解包后的聊天流字典、旧版 ``stream`` 包装字典和失败
                响应。

        Returns:
            成功响应包含 ID 时返回去除空白的 ``stream_id`` 或 ``session_id``；
            失败或格式不合法时返回空字符串。
        """

        if not isinstance(result, dict):
            return ""
        if result.get("success") is False:
            return ""
        stream = result.get("stream", result)
        if isinstance(stream, dict):
            return str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        return ""
