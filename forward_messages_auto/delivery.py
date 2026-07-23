"""后台顺序投递服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from forward_messages_auto.config import ForwardMessagesAutoConfig
from forward_messages_auto.models import ForwardJob, TargetStage
from forward_messages_auto.state import ForwardStateStore
from forward_messages_auto.streams import GroupStreamRegistry


class CapabilityResult:
    """统一解释 MaiBot capability 的返回值。"""

    @staticmethod
    def succeeded(result: Any) -> bool:
        if isinstance(result, bool):
            return result
        return isinstance(result, dict) and bool(result.get("success", False))

    @staticmethod
    def error(result: Any) -> str:
        if isinstance(result, dict):
            return str(result.get("error") or result.get("message") or "未知错误")
        return "返回格式错误"


class ForwardDeliveryService:
    """按目标群顺序推进发送、上下文和 Planner 阶段。"""

    def __init__(
        self,
        context: Any,
        config_provider: Callable[[], ForwardMessagesAutoConfig],
        streams: GroupStreamRegistry,
        state: ForwardStateStore,
    ) -> None:
        self._ctx = context
        self._config_provider = config_provider
        self._streams = streams
        self._state = state
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self._active_job_keys: set[str] = set()
        self._is_unloading = False

    def is_active(self, state_key: str) -> bool:
        return state_key in self._active_job_keys

    def schedule(self, job: ForwardJob) -> None:
        self._active_job_keys.add(job.state_key)
        task = asyncio.create_task(
            self._run_job(job),
            name=f"cross-group-forward:{job.job_id}",
        )
        self.background_tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def stop(self) -> None:
        self._is_unloading = True
        tasks = list(self.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()

    def resume(self) -> None:
        self._is_unloading = False

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self.background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is not None:
            self._ctx.logger.error(
                "跨群转发后台任务异常结束: %s",
                exception,
                exc_info=exception,
            )

    async def _run_job(self, job: ForwardJob) -> None:
        try:
            await self._state.ensure_job(job)
            for target_group_id in job.target_group_ids:
                if self._is_unloading:
                    return
                try:
                    await self._process_target(job, target_group_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._ctx.logger.error(
                        "处理目标群失败: job=%s target=%s error=%s",
                        job.job_id,
                        target_group_id,
                        exc,
                        exc_info=True,
                    )
        finally:
            self._active_job_keys.discard(job.state_key)

    async def _process_target(self, job: ForwardJob, target_group_id: str) -> None:
        target_stream_id = await self._streams.resolve(target_group_id)
        if not target_stream_id:
            self._ctx.logger.warning(
                "无法解析 target 群聊流: job=%s target=%s",
                job.job_id,
                target_group_id,
            )
            return

        stage = self._state.get_target_stage(job.state_key, target_group_id)
        if stage < TargetStage.SENT:
            if not await self._send_forward(job, target_group_id, target_stream_id):
                return
            stage = TargetStage.SENT

        if stage < TargetStage.CONTEXT_APPENDED:
            if not await self._append_context(job, target_group_id, target_stream_id):
                return
            stage = TargetStage.CONTEXT_APPENDED

        if not self._config_provider().behavior.trigger_target_planner:
            return
        if stage < TargetStage.PLANNER_QUEUED:
            await self._trigger_planner(job, target_group_id, target_stream_id)

    async def _send_forward(
        self,
        job: ForwardJob,
        target_group_id: str,
        target_stream_id: str,
    ) -> bool:
        result = await self._ctx.send.forward(
            job.forward_messages,
            target_stream_id,
            processed_plain_text="[跨群分享的合并转发消息]",
            storage_message=True,
            sync_to_maisaka_history=False,
            maisaka_source_kind=f"cross_group_forward:{job.job_id}",
        )
        if not CapabilityResult.succeeded(result):
            self._ctx.logger.warning(
                "合并转发发送失败: job=%s target=%s error=%s",
                job.job_id,
                target_group_id,
                CapabilityResult.error(result),
            )
            return False
        await self._state.advance_target(job, target_group_id, TargetStage.SENT)
        return True

    async def _append_context(
        self,
        job: ForwardJob,
        target_group_id: str,
        target_stream_id: str,
    ) -> bool:
        visible_text = f"[麦麦从其他群聊分享了一则合并转发消息；以下内容已在源群完整展开]\n{job.expanded_content}"
        result = await self._ctx.maisaka.context.append(
            target_stream_id,
            segments=[job.forward_segment],
            visible_text=visible_text,
            source_kind=f"cross_group_forward:{job.job_id}",
            message_id=f"cross-forward:{job.job_id}:{target_group_id}",
        )
        if not CapabilityResult.succeeded(result):
            self._ctx.logger.warning(
                "目标群上下文写入失败: job=%s target=%s error=%s",
                job.job_id,
                target_group_id,
                CapabilityResult.error(result),
            )
            return False
        await self._state.advance_target(
            job,
            target_group_id,
            TargetStage.CONTEXT_APPENDED,
        )
        return True

    async def _trigger_planner(
        self,
        job: ForwardJob,
        target_group_id: str,
        target_stream_id: str,
    ) -> None:
        intent = (
            "你刚刚把一则来自其他群聊的合并转发分享到了本群。"
            "消息完整内容已经写入当前上下文，无需再次调用 view_forward_message。"
            "请结合本群近期聊天、群友关系、记忆和你的表达习惯，自主决定是否发表一句自然的整体看法；"
            "如果没有合适或有价值的话可说，就保持沉默。不要机械复述消息，也不要暴露插件内部流程。"
        )
        result = await self._ctx.maisaka.proactive.trigger(
            target_stream_id,
            intent=intent,
            reason=job.sharing_reason or "源群 Planner 判断这则内容值得分享",
            priority="normal",
            metadata={
                "job_id": job.job_id,
                "source_message_id": job.source_message_id,
            },
        )
        if not CapabilityResult.succeeded(result):
            self._ctx.logger.warning(
                "目标群 Planner 触发失败: job=%s target=%s error=%s",
                job.job_id,
                target_group_id,
                CapabilityResult.error(result),
            )
            return
        await self._state.advance_target(
            job,
            target_group_id,
            TargetStage.PLANNER_QUEUED,
        )
        self._ctx.logger.info(
            "目标群处理完成: job=%s target=%s",
            job.job_id,
            target_group_id,
        )
