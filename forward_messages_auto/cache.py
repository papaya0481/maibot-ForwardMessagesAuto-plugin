"""view_forward_message 结果缓存。"""

from __future__ import annotations

import time

from forward_messages_auto.models import ViewCacheEntry


class ViewResultCache:
    """按聊天流和消息 ID 缓存源群已经展开的内容。"""

    def __init__(self) -> None:
        """创建空的进程内查看结果缓存。

        缓存仅在插件 Runner 当前进程中生效，不进行磁盘持久化。键由
        ``(stream_id, message_id)`` 组成，值同时保存完整内容和写入时间。
        """

        self._entries: dict[tuple[str, str], ViewCacheEntry] = {}

    def put(self, stream_id: str, message_id: str, content: str, *, now: float | None = None) -> None:
        """写入或覆盖一条源群合并转发查看结果。

        Args:
            stream_id: 源群对应的 MaiBot 聊天流 ID。
            message_id: 已调用 ``view_forward_message`` 查看过的消息 ID。
            content: 查看工具返回的完整可读内容。
            now: 可选的 Unix 时间戳。省略时使用当前时间；测试可传入固定值。
        """

        self._entries[(stream_id, message_id)] = ViewCacheEntry(
            content=content,
            cached_at=now if now is not None else time.time(),
        )

    def get(self, stream_id: str, message_id: str, ttl_seconds: int) -> str:
        """读取仍在有效期内的完整查看内容。

        读取前会先清理全部过期条目，因此该方法也会改变缓存状态。TTL
        最低按 60 秒处理。

        Args:
            stream_id: 源群对应的 MaiBot 聊天流 ID。
            message_id: 要查询的合并转发消息 ID。
            ttl_seconds: 缓存有效期秒数；小于 60 的值按 60 处理。

        Returns:
            命中时返回缓存的完整内容；条目不存在或已经过期时返回空字符串。

        Examples:
            ``cache.get("stream-1", "msg-1", 1800)`` 返回最近半小时内缓存
            的查看结果。
        """

        self.cleanup(ttl_seconds)
        entry = self._entries.get((stream_id, message_id))
        return entry.content if entry is not None else ""

    def cleanup(self, ttl_seconds: int, *, now: float | None = None) -> None:
        """移除所有早于有效期截止时间的缓存条目。

        Args:
            ttl_seconds: 缓存有效期秒数；小于 60 的值按 60 处理。
            now: 可选的当前 Unix 时间戳。省略时读取系统时间；测试可传入
                固定值以验证过期边界。
        """

        current_time = now if now is not None else time.time()
        cutoff = current_time - max(60, int(ttl_seconds))
        self._entries = {key: entry for key, entry in self._entries.items() if entry.cached_at >= cutoff}
