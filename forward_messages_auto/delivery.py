"""后台顺序投递服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .config import ForwardMessagesAutoConfig
from .models import ForwardJob, TargetStage
from .state import ForwardStateStore
from .streams import GroupStreamRegistry


class CapabilityResult:
    """统一解释 MaiBot capability 的返回值。"""

    @staticmethod
    def succeeded(result: Any) -> bool:
        """判断 capability 调用结果是否表示成功。

        Args:
            result: MaiBot capability 返回值。兼容直接布尔值和包含
                ``success`` 字段的字典。

        Returns:
            布尔值原样返回；字典的 ``success`` 为真时返回 ``True``；
            其他类型或缺失成功标记时返回 ``False``。
        """

        if isinstance(result, bool):
            return result
        return isinstance(result, dict) and bool(result.get("success", False))

    @staticmethod
    def error(result: Any) -> str:
        """从 capability 失败结果中提取可读原因。

        Args:
            result: MaiBot capability 返回值。

        Returns:
            字典中优先使用 ``error``，其次使用 ``message``，均不存在时返回
            “未知错误”；非字典返回“返回格式错误”。
        """

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
        """创建共享后台任务和阶段状态的顺序投递服务。

        Args:
            context: MaiBot ``PluginContext``，用于发送消息、写入 Maisaka
                上下文、触发 Planner 和记录日志。
            config_provider: 返回最新插件配置的回调，用于读取是否触发目标群
                Planner。
            streams: 负责将 target QQ 群号解析为聊天流的注册表。
            state: 记录每个 target 已完成阶段的持久化存储。
        """

        self._ctx = context
        self._config_provider = config_provider
        self._streams = streams
        self._state = state
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self._active_job_keys: set[str] = set()
        self._is_unloading = False

    def is_active(self, state_key: str) -> bool:
        """判断幂等键对应的任务是否仍在当前进程中执行。

        Args:
            state_key: 转发任务的稳定幂等键。

        Returns:
            任务已安排且尚未从 ``_run_job`` 退出时返回 ``True``。
        """

        return state_key in self._active_job_keys

    def schedule(self, job: ForwardJob) -> None:
        """将一个已验证任务安排到当前事件循环后台执行。

        方法立即登记活动键并创建具名 ``asyncio.Task``，不会等待任何 target
        实际投递。任务结束时回调负责回收引用和记录未处理异常。

        Args:
            job: 已完成权限、消息类型和幂等校验的转发任务快照。

        Raises:
            RuntimeError: 当调用时没有正在运行的 asyncio 事件循环。
        """

        self._active_job_keys.add(job.state_key)
        task = asyncio.create_task(
            self._run_job(job),
            name=f"cross-group-forward:{job.job_id}",
        )
        self.background_tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def stop(self) -> None:
        """进入卸载状态，取消并等待全部后台任务。

        取消结果通过 ``return_exceptions=True`` 收集，因此单个任务的取消或
        清理异常不会阻止其他任务结束。完成后任务集合为空。
        """

        self._is_unloading = True
        tasks = list(self.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()

    def resume(self) -> None:
        """退出卸载状态，允许新任务继续逐目标处理。

        该方法不恢复已经被取消的 ``asyncio.Task``；未完成阶段依靠后续同一
        请求结合持久化状态重新创建任务。
        """

        self._is_unloading = False

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """回收完成任务，并记录未被任务内部处理的异常。

        Args:
            task: ``schedule`` 创建且已经进入完成状态的后台任务。

        Note:
            正常完成和取消不会写错误日志；只有 ``task.exception()`` 返回
            非空异常时记录堆栈。
        """

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
        """按照任务快照中的 target 顺序执行完整投递。

        开始时确保任务状态已持久化。单个 target 的普通异常会记录后继续
        下一个 target；任务取消会向上传播；无论如何退出都会移除活动键。

        Args:
            job: 包含 source、目标顺序、原始节点和展开内容的任务快照。

        Raises:
            asyncio.CancelledError: 插件卸载取消任务时向上保留取消语义。
        """

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
        """从已持久化阶段继续处理单个目标群。

        方法依次解析聊天流、发送合并转发、写入 Maisaka 上下文，并按配置
        触发目标群 Planner。任一步骤返回失败时停止当前 target，不推进后续
        阶段；外层任务仍会继续下一个 target。

        Args:
            job: 当前转发任务快照。
            target_group_id: 当前处理的 target QQ 群号。
        """

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
        """将原始合并转发节点发送到目标聊天流。

        发送时开启 Maisaka 历史同步，使 Host 在目标群 runtime 已存在时把
        带目标平台消息 ID 的真实发送消息写入历史，供 Planner 的 ``reply``
        工具定位。下一阶段仍显式写入源群已经展开的内容；只有 capability
        成功后才持久化 ``SENT`` 阶段。

        Args:
            job: 提供发送节点、任务 ID 和来源元数据的任务快照。
            target_group_id: 用于日志和阶段状态的目标 QQ 群号。
            target_stream_id: 已解析的目标 MaiBot 聊天流 ID。

        Returns:
            发送并记录阶段成功时返回 ``True``；capability 返回失败时记录
            警告并返回 ``False``。
        """

        result = await self._ctx.send.forward(
            job.forward_messages,
            target_stream_id,
            processed_plain_text="[跨群分享的合并转发消息]",
            storage_message=True,
            sync_to_maisaka_history=True,
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
        """把转发消息段及源群展开文本写入目标 Maisaka 上下文。

        显式上下文包含原始 ``forward`` 段和可读 ``visible_text``，使目标群
        Planner 无需再次调用查看工具。成功后持久化
        ``CONTEXT_APPENDED`` 阶段。

        Args:
            job: 提供原始消息段、展开内容和任务 ID 的任务快照。
            target_group_id: 用于构造稳定上下文消息 ID 的目标 QQ 群号。
            target_stream_id: 接收上下文的目标聊天流 ID。

        Returns:
            上下文追加并记录阶段成功时返回 ``True``；capability 失败时记录
            警告并返回 ``False``。
        """

        visible_text = f"[你从其他群聊分享了一则合并转发消息；以下内容已在源群完整展开]\n{job.expanded_content}"
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
        """强制触发目标群 Planner 自主决定评论或沉默。

        意图明确告知 Planner 完整内容已经进入上下文，并要求结合本群语境
        自主判断，而非机械复述。若决定评论，只允许选择目标群上下文中刚刚
        真实发送的合并转发消息作为 ``reply`` 目标；主动任务不暴露源群消息
        ID，避免跨聊天流误用。capability 失败只记录警告；成功后推进到
        ``PLANNER_QUEUED``。

        Args:
            job: 提供分享理由和任务 ID 的任务快照。
            target_group_id: 用于日志和阶段状态的目标 QQ 群号。
            target_stream_id: 要触发主动任务的目标聊天流 ID。
        """

        intent = (
            "你刚刚把一则来自其他群聊的合并转发分享到了本群。"
            "消息完整内容已经写入当前上下文，无需再次调用 view_forward_message。"
            "请结合本群近期聊天、群友关系、记忆和你的表达习惯，自主决定是否发表一句自然的整体看法；"
            "如果决定发表看法，调用 reply 时必须选择当前上下文中由你刚刚实际发送的那则合并转发消息，"
            "使用它在本群中的 msg_id；不要使用源群消息 ID，也不要选择插件追加的 cross-forward 上下文消息。"
            "如果没有合适或有价值的话可说，就保持沉默。不要机械复述消息，也不要暴露插件内部流程。"
        )
        result = await self._ctx.maisaka.proactive.trigger(
            target_stream_id,
            intent=intent,
            reason=job.sharing_reason or "源群 Planner 判断这则内容值得分享",
            priority="normal",
            metadata={"job_id": job.job_id},
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
