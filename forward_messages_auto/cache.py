"""view_forward_message 结果缓存。"""

from __future__ import annotations

from collections import OrderedDict
import time

from .models import (
    ViewCacheEntry,
    ViewCacheLookup,
    ViewCacheStatus,
    ViewFailureState,
    ViewObservationKind,
)

MAX_PROCESSED_VIEW_CALLS = 4096


class ViewResultCache:
    """按聊天流和消息 ID 缓存源群已经展开的内容。"""

    def __init__(self) -> None:
        """创建空的进程内查看结果缓存。

        缓存仅在插件 Runner 当前进程中生效，不进行磁盘持久化。键由
        ``(stream_id, message_id)`` 组成，值同时保存完整内容和写入时间。
        """

        self._entries: dict[tuple[str, str], ViewCacheEntry] = {}
        self._expired_keys: set[tuple[str, str]] = set()
        self._failure_states: dict[tuple[str, str], ViewFailureState] = {}
        self._processed_calls: OrderedDict[tuple[str, str], None] = OrderedDict()

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
        self._expired_keys.discard((stream_id, message_id))
        self._failure_states.pop((stream_id, message_id), None)

    def record_observation(
        self,
        stream_id: str,
        call_id: str,
        message_id: str,
        content: str,
        *,
        kind: ViewObservationKind,
        now: float | None = None,
    ) -> bool:
        """只处理一次查看调用，并更新成功缓存或分类失败状态。

        已处理的 ``call_id`` 不会再次刷新缓存时间或累计失败。成功结果会
        写入完整内容并清除此前失败状态；可重试故障和空内容分别累计连续
        次数，其他故障只保存最近分类。不同失败分类会中断之前的连续计数，
        且所有失败都不会覆盖仍然有效的成功缓存。调用 ID 使用有界集合
        保存，避免长期运行无限增长。

        Args:
            stream_id: 发起查看调用的源聊天流 ID。
            call_id: Planner 工具调用 ID，在同一聊天流内用于去重。
            message_id: 被查看的合并转发消息 ID。
            content: ToolResult 的非空文本；成功时作为完整内容缓存。
            kind: 当前插件根据已知 Host 结果文案判断出的语义分类。
            now: 可选 Unix 时间戳。省略时使用当前时间。

        Returns:
            仅当本次是尚未处理过的成功结果时返回 ``True``，供调用方生成
            一次性 Planner 续轮提醒；重复调用或失败结果返回 ``False``。
        """

        processed_key = (stream_id, call_id)
        if processed_key in self._processed_calls:
            self._processed_calls.move_to_end(processed_key)
            return False

        self._processed_calls[processed_key] = None
        while len(self._processed_calls) > MAX_PROCESSED_VIEW_CALLS:
            self._processed_calls.popitem(last=False)

        if kind is ViewObservationKind.SUCCESS:
            self.put(stream_id, message_id, content, now=now)
            return True

        cache_key = (stream_id, message_id)
        previous = self._failure_states.get(cache_key)
        state = ViewFailureState(last_kind=kind, last_content=content)
        if kind is ViewObservationKind.RETRYABLE_FAILURE:
            state.retryable_count = (
                previous.retryable_count + 1 if previous is not None and previous.last_kind is kind else 1
            )
        elif kind is ViewObservationKind.EMPTY_CONTENT_FAILURE:
            state.empty_content_count = (
                previous.empty_content_count + 1 if previous is not None and previous.last_kind is kind else 1
            )
        self._failure_states[cache_key] = state
        return False

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

        return self.lookup(stream_id, message_id, ttl_seconds).content

    def lookup(self, stream_id: str, message_id: str, ttl_seconds: int) -> ViewCacheLookup:
        """读取完整内容、过期状态和分类失败状态。

        Args:
            stream_id: 源群对应的 MaiBot 聊天流 ID。
            message_id: 要查询的合并转发消息 ID。
            ttl_seconds: 缓存有效期秒数；小于 60 的值按 60 处理。

        Returns:
            ``ViewCacheLookup``。有效缓存返回 ``READY`` 和完整内容；曾经
            成功但已过期返回 ``EXPIRED``；从未成功缓存返回 ``MISSING``。
            三种状态都会携带最近失败分类及对应的连续计数。
        """

        self.cleanup(ttl_seconds)
        key = (stream_id, message_id)
        failure_state = self._failure_states.get(key, ViewFailureState())
        entry = self._entries.get(key)
        if entry is not None:
            return ViewCacheLookup(
                status=ViewCacheStatus.READY,
                content=entry.content,
                retryable_failure_count=failure_state.retryable_count,
                empty_content_failure_count=failure_state.empty_content_count,
                last_observation_kind=failure_state.last_kind,
                last_failure_content=failure_state.last_content,
            )
        status = ViewCacheStatus.EXPIRED if key in self._expired_keys else ViewCacheStatus.MISSING
        return ViewCacheLookup(
            status=status,
            content="",
            retryable_failure_count=failure_state.retryable_count,
            empty_content_failure_count=failure_state.empty_content_count,
            last_observation_kind=failure_state.last_kind,
            last_failure_content=failure_state.last_content,
        )

    def cleanup(self, ttl_seconds: int, *, now: float | None = None) -> None:
        """移除所有早于有效期截止时间的缓存条目。

        Args:
            ttl_seconds: 缓存有效期秒数；小于 60 的值按 60 处理。
            now: 可选的当前 Unix 时间戳。省略时读取系统时间；测试可传入
                固定值以验证过期边界。
        """

        current_time = now if now is not None else time.time()
        cutoff = current_time - max(60, int(ttl_seconds))
        expired_keys = {key for key, entry in self._entries.items() if entry.cached_at < cutoff}
        self._expired_keys.update(expired_keys)
        self._entries = {key: entry for key, entry in self._entries.items() if key not in expired_keys}
