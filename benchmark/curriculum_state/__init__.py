"""
benchmark/curriculum_state/ — 掌握度 / 巩固循环状态容器（与采样策略解耦）

对应 Official Implementation Alignment Audit 要求 A："Keep sampler separate
from algorithm"：把 `FamilyAwareSampler` 原本内联管理的
`{worker_id: solved_task_ids}` / `{worker_id: cycle_count}` 两个 dict
抽成一个独立、可单测的 `CurriculumState` 状态容器，让采样器本身只负责
"给定当前 state，按什么策略选任务"，不直接操作原始 dict。

⚠️ Paper Fidelity 声明：与 benchmark/dependencies/ 相同，"掌握度状态" /
"巩固循环"本身仍然是 Phase12 自建、非论文/官方要求的实验性扩展（见
docs/SIMPLIFICATIONS.md §2.4）。本模块只做状态管理与采样策略的解耦，
不改变这个机制的论文/官方依据状态。
"""

from __future__ import annotations

__all__ = ["CurriculumState"]


class CurriculumState:
    """按 worker_id 维护"已掌握任务集合"与"巩固循环次数"的纯状态容器。"""

    def __init__(self) -> None:
        self._solved: dict[str, set[str]] = {}
        self._cycles: dict[str, int] = {}

    def solved_ids(self, worker_id: str) -> set[str]:
        """
        返回该 worker 已掌握的 task_id 集合的**可变引用**（供采样器内部
        读写）。对外暴露只读快照请用 solved_snapshot()。
        """
        return self._solved.setdefault(worker_id, set())

    def solved_snapshot(self, worker_id: str) -> set[str]:
        """只读快照，防止调用方意外修改内部状态。"""
        return set(self._solved.get(worker_id, set()))

    def mark_solved(self, worker_id: str, task_id: str) -> None:
        self.solved_ids(worker_id).add(task_id)

    def reset(self, worker_id: str) -> None:
        """清空该 worker 的已掌握集合，通常在开启新一轮巩固循环时调用。"""
        self.solved_ids(worker_id).clear()

    def start_new_cycle(self, worker_id: str) -> int:
        """记一次巩固循环，返回新的循环计数。"""
        self._cycles[worker_id] = self._cycles.get(worker_id, 0) + 1
        return self._cycles[worker_id]

    def cycles(self, worker_id: str) -> int:
        return self._cycles.get(worker_id, 0)
