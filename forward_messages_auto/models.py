"""跨群转发领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class TargetStage(IntEnum):
    """单个目标群已经完成的处理阶段。"""

    PENDING = 0
    SENT = 1
    CONTEXT_APPENDED = 2
    PLANNER_QUEUED = 3

    @classmethod
    def from_value(cls, value: str) -> TargetStage:
        try:
            return cls[value.upper()]
        except (KeyError, AttributeError):
            return cls.PENDING

    @property
    def storage_value(self) -> str:
        return self.name.lower()


@dataclass(slots=True)
class ViewCacheEntry:
    """一次 view_forward_message 的展开结果。"""

    content: str
    cached_at: float


@dataclass(slots=True)
class ForwardJob:
    """后台转发任务快照。"""

    job_id: str
    state_key: str
    source_stream_id: str
    source_group_id: str
    source_message_id: str
    target_group_ids: list[str]
    forward_segment: dict[str, Any]
    forward_messages: list[dict[str, Any]]
    expanded_content: str
    sharing_reason: str
