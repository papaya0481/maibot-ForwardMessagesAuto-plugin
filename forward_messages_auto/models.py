"""跨群转发领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any


class TargetStage(IntEnum):
    """单个目标群已经完成的处理阶段。"""

    PENDING = 0
    SENT = 1
    CONTEXT_APPENDED = 2
    PLANNER_QUEUED = 3

    @classmethod
    def from_value(cls, value: str) -> TargetStage:
        """将持久化阶段字符串转换为枚举值。

        Args:
            value: 阶段名称，不区分大小写，例如 ``"sent"``。未知值、
                空值或非字符串兼容值会降级为 ``PENDING``。

        Returns:
            对应的 ``TargetStage``；无法识别时返回 ``TargetStage.PENDING``。
        """

        try:
            return cls[value.upper()]
        except (KeyError, AttributeError):
            return cls.PENDING

    @property
    def storage_value(self) -> str:
        """返回适合写入 JSON 状态文件的小写阶段名称。

        Returns:
            当前枚举成员的小写名称，例如 ``TargetStage.SENT`` 返回
            ``"sent"``。
        """

        return self.name.lower()


@dataclass(slots=True)
class ViewEligibilityEntry:
    """当前 Planner 上下文中一次成功查看的展开结果。"""

    call_id: str
    content: str


class ViewEligibilityStatus(str, Enum):
    """指定消息在当前 Planner 上下文中的查看资格状态。"""

    READY = "ready"
    MISSING = "missing"


class ViewObservationKind(str, Enum):
    """一次 view_forward_message 结果的语义分类。"""

    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    EMPTY_CONTENT_FAILURE = "empty_content_failure"
    CORRECTABLE_FAILURE = "correctable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    UNKNOWN_FAILURE = "unknown_failure"


@dataclass(slots=True)
class ViewFailureState:
    """同一消息最近一段连续查看失败的分类状态。"""

    retryable_count: int = 0
    empty_content_count: int = 0
    last_kind: ViewObservationKind | None = None
    last_content: str = ""


@dataclass(frozen=True, slots=True)
class ViewEligibilityLookup:
    """一次当前上下文查看资格查询结果。"""

    status: ViewEligibilityStatus
    content: str
    retryable_failure_count: int
    empty_content_failure_count: int
    last_observation_kind: ViewObservationKind | None
    last_failure_content: str


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
