"""转发任务阶段的持久化。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Any

from .models import ForwardJob, TargetStage


class ForwardStateStore:
    """原子保存各目标群阶段，并提供幂等完成判断。"""

    def __init__(self, path: Path, logger: Any) -> None:
        """创建任务状态存储。

        Args:
            path: 状态 JSON 文件的完整路径，通常位于插件数据目录下。
            logger: 用于记录状态文件读取失败的日志对象。

        Note:
            构造方法不会读取磁盘；调用方必须在使用状态前等待 ``load``。
        """

        self._path = path
        self._logger = logger
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """在线程中读取状态文件并替换当前内存状态。

        文件不存在时初始化为空状态；JSON 损坏或读取失败时记录警告并使用
        空状态。磁盘 I/O 通过 ``asyncio.to_thread`` 执行，避免阻塞事件循环。
        """

        await asyncio.to_thread(self._load_sync)

    async def save(self) -> None:
        """在异步锁保护下原子保存当前内存状态。

        方法等待其他状态更新完成，再在线程中将完整快照写入临时文件并替换
        正式文件。
        """

        async with self._lock:
            await asyncio.to_thread(self._save_sync)

    async def prune(self, ttl_seconds: int) -> None:
        """删除超过去重保留期的任务并立即持久化。

        Args:
            ttl_seconds: 状态保留秒数，小于 60 的值按 60 处理。任务以
                ``updated_at`` 为准，缺失时回退到 ``created_at``。
        """

        cutoff = time.time() - max(60, int(ttl_seconds))
        async with self._lock:
            self._jobs = {
                key: value
                for key, value in self._jobs.items()
                if isinstance(value, dict) and float(value.get("updated_at") or value.get("created_at") or 0) >= cutoff
            }
            await asyncio.to_thread(self._save_sync)

    def is_complete(
        self,
        state_key: str,
        target_group_ids: list[str],
        required_stage: TargetStage,
    ) -> bool:
        """判断指定任务的所有目标是否达到要求阶段。

        Args:
            state_key: 由 source stream、消息 ID 和目标路由生成的幂等键。
            target_group_ids: 当前路由要求覆盖的目标 QQ 群号列表。
            required_stage: 每个目标至少需要达到的阶段；是否触发 Planner
                会影响调用方选择的阶段。

        Returns:
            任务存在且每个目标阶段均不低于 ``required_stage`` 时返回
            ``True``。状态缺失、格式异常或任一目标未完成时返回 ``False``。
        """

        job_state = self._jobs.get(state_key)
        if not isinstance(job_state, dict):
            return False
        target_states = job_state.get("targets")
        if not isinstance(target_states, dict):
            return False
        return all(
            TargetStage.from_value(str(target_states.get(group_id) or "pending")) >= required_stage
            for group_id in target_group_ids
        )

    def get_target_stage(self, state_key: str, target_group_id: str) -> TargetStage:
        """读取单个目标群已经完成的最高阶段。

        Args:
            state_key: 转发任务的稳定幂等键。
            target_group_id: 要查询的目标 QQ 群号。

        Returns:
            已持久化的 ``TargetStage``。任务、目标或阶段值不存在或无效时
            返回 ``TargetStage.PENDING``。
        """

        job_state = self._jobs.get(state_key)
        if not isinstance(job_state, dict):
            return TargetStage.PENDING
        target_states = job_state.get("targets")
        if not isinstance(target_states, dict):
            return TargetStage.PENDING
        return TargetStage.from_value(str(target_states.get(target_group_id) or "pending"))

    async def ensure_job(self, job: ForwardJob) -> None:
        """确保任务元数据存在，并将状态写入磁盘。

        已存在的任务不会被覆盖，因此重试或插件重载能够保留各目标已经完成
        的阶段。

        Args:
            job: 即将执行的不可变转发任务快照。
        """

        async with self._lock:
            self._jobs.setdefault(job.state_key, self._new_job_state(job))
            await asyncio.to_thread(self._save_sync)

    async def advance_target(
        self,
        job: ForwardJob,
        target_group_id: str,
        stage: TargetStage,
    ) -> None:
        """单向提升目标群阶段，并在发生变化时持久化。

        该方法不会回退阶段。传入阶段不高于当前阶段时不写磁盘，从而避免
        重复 capability 回调破坏幂等状态。

        Args:
            job: 目标所属的转发任务快照。
            target_group_id: 已完成某一步骤的目标 QQ 群号。
            stage: 本次确认完成的新阶段。
        """

        async with self._lock:
            job_state = self._jobs.setdefault(job.state_key, self._new_job_state(job))
            target_states = job_state.setdefault("targets", {})
            current_stage = TargetStage.from_value(str(target_states.get(target_group_id) or "pending"))
            if stage > current_stage:
                target_states[target_group_id] = stage.storage_value
                job_state["updated_at"] = time.time()
                await asyncio.to_thread(self._save_sync)

    @staticmethod
    def _new_job_state(job: ForwardJob) -> dict[str, Any]:
        """根据任务快照构造新的 JSON 可序列化状态。

        Args:
            job: 用于复制 source、消息和目标路由元数据的转发任务。

        Returns:
            包含创建/更新时间、空目标阶段映射和任务元数据的状态字典。
        """

        now = time.time()
        return {
            "job_id": job.job_id,
            "source_stream_id": job.source_stream_id,
            "source_group_id": job.source_group_id,
            "source_message_id": job.source_message_id,
            "target_group_ids": list(job.target_group_ids),
            "created_at": now,
            "updated_at": now,
            "targets": {},
        }

    def _load_sync(self) -> None:
        """同步读取并校验状态文件的顶层任务映射。

        文件不存在或读取失败时将内存状态设置为空字典。格式合法时只保留
        键可转为字符串且值为字典的任务记录，忽略其他损坏条目。
        """

        if not self._path.is_file():
            self._jobs = {}
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._logger.warning("读取转发状态失败，将使用空状态: %s", exc)
            self._jobs = {}
            return
        raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
        self._jobs = (
            {str(key): value for key, value in raw_jobs.items() if isinstance(value, dict)}
            if isinstance(raw_jobs, dict)
            else {}
        )

    def _save_sync(self) -> None:
        """同步地以临时文件替换方式保存状态快照。

        该方法创建父目录，将 JSON 写入同目录下的 ``.tmp`` 文件，再调用
        ``Path.replace`` 替换正式文件，降低进程中断留下半写文件的概率。

        Raises:
            OSError: 当目录创建、临时文件写入或原子替换失败时向调用方传播。
        """

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(".tmp")
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "jobs": self._jobs,
        }
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self._path)
