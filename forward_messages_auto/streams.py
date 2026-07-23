"""QQ 群号与 MaiBot 聊天流的映射。"""

from __future__ import annotations

from typing import Any


class GroupStreamRegistry:
    """维护 QQ 群号到聊天流的索引，并按需创建目标流。"""

    def __init__(self, context: Any) -> None:
        """创建绑定到当前插件上下文的空聊天流索引。

        Args:
            context: MaiBot 注入的 ``PluginContext``。该服务使用其中的
                ``chat`` capability 查询或创建聊天流，并使用 ``logger``
                记录可恢复错误。
        """

        self._ctx = context
        self._streams_by_group: dict[str, str] = {}
        self._source_stream_ids: set[str] = set()

    def is_source_stream(self, stream_id: str) -> bool:
        """判断聊天流是否对应当前配置允许的 source 群。

        Args:
            stream_id: 待检查的 MaiBot 聊天流 ID。

        Returns:
            若最近一次 ``refresh`` 已将该聊天流识别为 source 群则返回
            ``True``，否则返回 ``False``。
        """

        return stream_id in self._source_stream_ids

    async def refresh(self, source_group_ids: list[str], *, enabled: bool) -> None:
        """重新加载 QQ 群聊流，并重建 source 会话索引。

        方法首先清空旧索引。插件未启用时直接保持空索引；查询异常或 Host
        拒绝请求时记录日志并返回，不向调用方传播这些可恢复错误。

        Args:
            source_group_ids: 已规范化的 source QQ 群号列表。
            enabled: 插件是否启用。为 ``False`` 时不会调用聊天能力。
        """

        self._streams_by_group.clear()
        self._source_stream_ids.clear()
        if not enabled:
            return

        try:
            result = await self._ctx.chat.get_group_streams(platform="qq")
        except Exception as exc:
            self._ctx.logger.warning("读取 QQ 群聊流失败: %s", exc)
            return
        if not isinstance(result, dict) or not result.get("success", False):
            error = result.get("error", "未知错误") if isinstance(result, dict) else "返回格式错误"
            self._ctx.logger.warning("读取 QQ 群聊流被拒绝: %s", error)
            return

        streams = result.get("streams")
        if not isinstance(streams, list):
            return
        for stream in streams:
            self._index_stream(stream)
        self._source_stream_ids = {
            stream_id for group_id in source_group_ids if (stream_id := self._streams_by_group.get(group_id))
        }

    async def resolve(self, group_id: str) -> str:
        """按 QQ 群号解析目标聊天流，必要时创建群聊会话。

        解析顺序为：读取内存索引、调用 ``get_stream_by_group_id``、最后调用
        ``open_session``。查询失败允许降级到创建；创建失败会记录日志。

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

    def _index_stream(self, stream: Any) -> None:
        """将一个有效 QQ 群聊流加入内存索引。

        已存在的群号不会被后续重复项覆盖，从而保持 Host 返回列表中第一条
        有效记录的优先级。

        Args:
            stream: ``get_group_streams`` 返回列表中的任意元素。非字典、非 QQ
                平台或缺少群号/聊天流 ID 的记录会被忽略。
        """

        if not isinstance(stream, dict):
            return
        if str(stream.get("platform") or "").strip().lower() != "qq":
            return
        group_id = str(stream.get("group_id") or "").strip()
        stream_id = str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        if group_id and stream_id and group_id not in self._streams_by_group:
            self._streams_by_group[group_id] = stream_id

    @staticmethod
    def _extract_stream_id(result: Any) -> str:
        """从聊天 capability 返回值中读取聊天流 ID。

        Args:
            result: ``get_stream_by_group_id`` 或 ``open_session`` 的返回值。
                支持 ID 位于 ``stream`` 字典内或返回值顶层的两种结构。

        Returns:
            capability 成功且包含 ID 时返回去除空白的 ``stream_id`` 或
            ``session_id``；失败或格式不合法时返回空字符串。
        """

        if not isinstance(result, dict) or not result.get("success", False):
            return ""
        stream = result.get("stream")
        if isinstance(stream, dict):
            return str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        return str(result.get("stream_id") or result.get("session_id") or "").strip()
