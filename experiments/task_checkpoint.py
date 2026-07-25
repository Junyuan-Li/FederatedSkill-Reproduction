"""任务级实验 checkpoint，不参与 FederatedSkill 算法决策。"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from core.datatypes import Trajectory, WorkerPatch


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "unnamed"


def _json_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class TaskCheckpointStore:
    """在 family 运行期间即时保存每个 worker 的 task 产物。"""

    def __init__(self, family_output_dir: Path | str | None) -> None:
        self._family_output_dir = (
            Path(family_output_dir) if family_output_dir is not None else None
        )

    @property
    def enabled(self) -> bool:
        return self._family_output_dir is not None

    def _task_dir(self, worker_id: str, round_idx: int, task_id: str) -> Path:
        assert self._family_output_dir is not None
        return (
            self._family_output_dir
            / "workers"
            / _safe_component(worker_id)
            / "tasks"
            / f"round_{round_idx:03d}_{_safe_component(task_id)}"
        )

    def trial_artifact_dir(self, worker_id: str, task_id: str) -> Path | None:
        """返回新版 task-level 隔离产物目录；单 worker 时保持 family/task 布局。"""
        if not self.enabled:
            return None
        assert self._family_output_dir is not None
        return self._family_output_dir / "tasks" / _safe_component(task_id)

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def save_success(
        self,
        trajectory: Trajectory,
        patch: WorkerPatch,
        attempt: int,
    ) -> None:
        if not self.enabled:
            return
        self.save_trajectory_reward(trajectory, attempt)
        task_dir = self._task_dir(
            trajectory.worker_id, trajectory.round_idx, trajectory.task_name
        )
        reward = float(trajectory.reward or 0.0)
        self._write_json(task_dir / "patch.json", _json_payload(patch))
        self._write_json(
            task_dir / "task_status.json",
            {
                "status": "completed" if reward >= 1.0 else "completed_unsuccessful",
                "task_id": trajectory.task_name,
                "worker_id": trajectory.worker_id,
                "round_idx": trajectory.round_idx,
                "attempt": attempt,
                "reward": reward,
            },
        )

    def save_trajectory_reward(self, trajectory: Trajectory, attempt: int) -> None:
        """Execute+Verify 返回后立即落盘，不等待 distillation。"""
        if not self.enabled:
            return
        task_dir = self._task_dir(
            trajectory.worker_id, trajectory.round_idx, trajectory.task_name
        )
        self._write_json(task_dir / "trajectory.json", _json_payload(trajectory))
        artifact_dir = self.trial_artifact_dir(
            trajectory.worker_id, trajectory.task_name
        )
        if artifact_dir is not None:
            self._write_json(
                artifact_dir / "trajectory" / _safe_component(trajectory.worker_id)
                / "trajectory.json",
                _json_payload(trajectory),
            )
        self._write_json(
            task_dir / "reward.json",
            {
                "task_id": trajectory.task_name,
                "worker_id": trajectory.worker_id,
                "round_idx": trajectory.round_idx,
                "attempt": attempt,
                "reward": float(trajectory.reward or 0.0),
                "soft_reward": trajectory.soft_reward,
                "verifier_output": trajectory.verifier_output,
                "verifier_subtest_failures": trajectory.verifier_subtest_failures,
            },
        )

    def save_failure(
        self,
        *,
        worker_id: str,
        round_idx: int,
        task_id: str,
        attempt: int,
        failure_reason: str,
        final: bool,
        retryable: bool = False,
        failure_class: str = "non_retryable",
    ) -> None:
        if not self.enabled:
            return
        task_dir = self._task_dir(worker_id, round_idx, task_id)
        payload = {
            "status": "failed" if final else "retrying",
            "task_id": task_id,
            "worker_id": worker_id,
            "round_idx": round_idx,
            "attempt": attempt,
            "failure_reason": failure_reason,
            "retryable": retryable,
            "failure_class": failure_class,
        }
        self._write_json(task_dir / "attempts" / f"attempt_{attempt}.json", payload)
        self._write_json(task_dir / "failure.json", payload)
        if final:
            self._write_json(task_dir / "task_status.json", payload)


def collect_task_checkpoint_stats(
    family_output_dir: Path | str,
    total_tasks: int,
) -> dict[str, int | float]:
    """从即时 checkpoint 计算 task-level 成功率。"""
    root = Path(family_output_dir) / "workers"
    statuses: list[dict[str, Any]] = []
    if root.exists():
        for path in root.glob("*/tasks/*/task_status.json"):
            try:
                statuses.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    completed = sum(item.get("status") == "completed" for item in statuses)
    failed = sum(item.get("status") == "failed" for item in statuses)
    return {
        "completed_tasks": completed,
        "failed_tasks": failed,
        "checkpointed_tasks": len(statuses),
        "total_tasks": total_tasks,
        "success_rate": completed / total_tasks if total_tasks else 0.0,
    }