"""view_forward_message 结果缓存。"""

from __future__ import annotations

import time

from forward_messages_auto.models import ViewCacheEntry


class ViewResultCache:
    """按聊天流和消息 ID 缓存源群已经展开的内容。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ViewCacheEntry] = {}

    def put(self, stream_id: str, message_id: str, content: str, *, now: float | None = None) -> None:
        self._entries[(stream_id, message_id)] = ViewCacheEntry(
            content=content,
            cached_at=now if now is not None else time.time(),
        )

    def get(self, stream_id: str, message_id: str, ttl_seconds: int) -> str:
        self.cleanup(ttl_seconds)
        entry = self._entries.get((stream_id, message_id))
        return entry.content if entry is not None else ""

    def cleanup(self, ttl_seconds: int, *, now: float | None = None) -> None:
        current_time = now if now is not None else time.time()
        cutoff = current_time - max(60, int(ttl_seconds))
        self._entries = {key: entry for key, entry in self._entries.items() if entry.cached_at >= cutoff}
