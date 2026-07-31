"""Planner 当前上下文查看资格的回归测试。"""

from __future__ import annotations

from forward_messages_auto.source.history import ViewToolObservation
from forward_messages_auto.source.models import ViewEligibilityStatus, ViewObservationKind
from forward_messages_auto.source.view_state import ViewEligibilityStore


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


def test_view_freshness_is_not_evicted_by_other_stream_activity() -> None:
    """验证高活跃聊天流不会驱逐其他流仍在上下文中的 fresh 状态。

    安静流先登记一个成功查看调用，再让另一聊天流同步超过旧全局 LRU 容量
    的独立调用。安静流重新同步相同上下文时不应把旧调用再次报告为 fresh。
    该测试防止跨流容量竞争导致 Planner 重复收到已经消费过的判断提醒。
    """

    store = ViewEligibilityStore()
    quiet_success = ViewToolObservation(
        call_id="quiet-call",
        message_id="quiet-message",
        content="安静流完整内容",
        kind=ViewObservationKind.SUCCESS,
    )
    assert store.sync_context("quiet-stream", [quiet_success]) == ["quiet-message"]

    busy_observations = [
        ViewToolObservation(
            call_id=f"busy-call-{index}",
            message_id=f"busy-message-{index}",
            content=f"活跃流内容 {index}",
            kind=ViewObservationKind.SUCCESS,
        )
        for index in range(4097)
    ]
    store.sync_context("busy-stream", busy_observations)

    assert store.sync_context("quiet-stream", [quiet_success]) == []
    assert store.lookup("quiet-stream", "quiet-message").status is ViewEligibilityStatus.READY
