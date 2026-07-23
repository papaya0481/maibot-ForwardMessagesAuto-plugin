"""转发任务阶段的持久化。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Any

from forward_messages_auto.models import ForwardJob, TargetStage


class ForwardStateStore:
    """原子保存各目标群阶段，并提供幂等完成判断。"""

    def __init__(self, path: Path, logger: Any) -> None:
        self._path = path
        self._logger = logger
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)

    async def save(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync)

    async def prune(self, ttl_seconds: int) -> None:
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
        job_state = self._jobs.get(state_key)
        if not isinstance(job_state, dict):
            return TargetStage.PENDING
        target_states = job_state.get("targets")
        if not isinstance(target_states, dict):
            return TargetStage.PENDING
        return TargetStage.from_value(str(target_states.get(target_group_id) or "pending"))

    async def ensure_job(self, job: ForwardJob) -> None:
        async with self._lock:
            self._jobs.setdefault(job.state_key, self._new_job_state(job))
            await asyncio.to_thread(self._save_sync)

    async def advance_target(
        self,
        job: ForwardJob,
        target_group_id: str,
        stage: TargetStage,
    ) -> None:
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
