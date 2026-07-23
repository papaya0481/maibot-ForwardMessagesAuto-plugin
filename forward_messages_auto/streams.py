"""QQ 群号与 MaiBot 聊天流的映射。"""

from __future__ import annotations

from typing import Any


class GroupStreamRegistry:
    """维护 QQ 群号到聊天流的索引，并按需创建目标流。"""

    def __init__(self, context: Any) -> None:
        self._ctx = context
        self._streams_by_group: dict[str, str] = {}
        self._source_stream_ids: set[str] = set()

    def is_source_stream(self, stream_id: str) -> bool:
        return stream_id in self._source_stream_ids

    async def refresh(self, source_group_ids: list[str], *, enabled: bool) -> None:
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
        if not isinstance(result, dict) or not result.get("success", False):
            return ""
        stream = result.get("stream")
        if isinstance(stream, dict):
            return str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        return str(result.get("stream_id") or result.get("session_id") or "").strip()
