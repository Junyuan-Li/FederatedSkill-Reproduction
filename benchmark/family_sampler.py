"""
family_sampler.py — FamilyAwareSampler（依赖感知 / 掌握度驱动的 family 采样器）

[EXTENSION] 分类结论：不是 FederatedSkill 论文方法（论文没有
mastery-based sampling / dependency graph / cycles_completed / solved set
这些机制），也不是 SkillFlow 官方实现行为（官方 partitioning.py 的
TaskPartitioner 是无状态划分函数，没有掌握度回写或依赖判定）。这些机制
纯粹是本项目在 **benchmark 层**自行设计的实验性扩展。
位置约束：本模块及其状态只应存在于 `benchmark/`，不得被 `core/`、`server/`、
`client/` 引用或依赖（当前代码库满足此约束，仅 `benchmark/__init__.py`
导出、仅测试文件引用）。

⚠️ Paper Fidelity 声明（审计后更正，见 docs/SIMPLIFICATIONS.md §2.4）：
    本模块的"依赖图 + 掌握度门控 + 巩固循环"机制是本复现（Phase12 会话）
    自行设计的实验性扩展，**不是论文原文的要求，也不是官方实现的行为**：
      - 论文 Section 5.1 原文只说 "20 diverse task families, each containing
        a sequence of tasks of increasing difficulty that all require the
        SAME underlying skill to be progressively evolved" —— 只描述了
        benchmark 数据本身的结构（family 内任务难度递增、共享同一技能），
        并未规定"采样器要怎么调度"这些任务。
      - 官方实现（skillfl/skillflow_adapter/partitioning.py）里任务分配
        是无状态的 TaskPartitioner（RoundRobin/Block/Replicate/Random），
        纯粹是 (tasks, n_workers) -> shards 的静态划分函数，默认
        RoundRobinPartitioner，完全没有依赖判定、掌握度回写或巩固循环。
    本模块目前未被 experiments/run_experiment.py 或 main_trainer.py 的
    _build_sampler() 引用，不影响任何真实实验结果，属于隔离在 benchmark/
    层的实验性扩展（core/client/server 均不依赖本模块）。若需要更贴近
    论文/官方语义的采样器，请使用 benchmark/curriculum.py::FamilyCurriculumSampler
    （纯按 round_idx 前进，无额外发明的语义）。

与 benchmark/curriculum.py::FamilyCurriculumSampler 的区别：

  FamilyCurriculumSampler（已存在，更贴近论文/官方语义）：
    完全由 round_idx 决定难度（round r -> difficulty r+1），
    不管上一轮任务是否真正被解决，纯粹"按轮次前进"。

  FamilyAwareSampler（本模块，Phase12 自建实验性扩展）：
    掌握度驱动（mastery-based）+ 依赖图感知（均为本复现自行设计，非论文/官方要求）：
      1. 每个 worker 绑定一个 family（同 CurriculumSampler 的 round-robin 分配语义）。
      2. 维护每个 worker 在该 family 内"已解决"的 task_id 集合
         （由调用方在拿到 verifier reward 后调用 record_result() 回写）。
      3. sample() 只返回"依赖已全部满足 (task.dependencies ⊆ solved) 且尚未解决"
         的任务中，难度最低的一个 —— 这是本复现对论文"同一技能的递增难度序列"
         描述的一种**可能的**调度实现，但论文并未明确要求这种依赖门控算法。
      4. 若某任务连续失败（reward < 1.0），sampler 不会跳过它，
         而是原地重复分配同一任务，直到被解决为止——这也是本复现自行设计的
         语义，用于模拟"δ_i^t 在同一 task x 上反复演化 L_i^t"的直觉，
         而不是像 DifficultyAwareSampler 那样只按轮次盲目提升难度。
      5. 当 family 内全部任务都被解决后，支持"同一 skill 多轮演化"：
         重置该 family 的 solved 集合，重新从最高难度任务开始循环
         （模拟"该技能已成熟，继续用高难度变体巩固/回归测试"，同样是自建语义）。

用法::

    from benchmark.family import load_all_families
    from benchmark.family_sampler import FamilyAwareSampler

    families = load_all_families()
    sampler = FamilyAwareSampler(families, seed=42)
    task = sampler.sample("u0", round_idx=0)
    # ... 执行 task，拿到 reward ...
    sampler.record_result("u0", task.task_id, reward=1.0)
    task2 = sampler.sample("u0", round_idx=1)  # 依赖已满足才会前进到下一难度
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from benchmark.curriculum_state import CurriculumState
from benchmark.dependencies import eligible_tasks as _eligible_tasks_fn
from benchmark.sampler import TaskSampler

if TYPE_CHECKING:
    from benchmark.family import TaskFamily
    from benchmark.task import Task

logger = logging.getLogger(__name__)


class FamilyAwareSampler(TaskSampler):
    """
    依赖图感知 + 掌握度驱动的 family 采样器。

    Args:
        families:          {family_id: TaskFamily}
        worker_family_map: {worker_id: family_id}；未显式指定的 worker
                            按首次调用顺序做确定性 round-robin 分配
        seed:               随机种子（同 family 内多个"同等可选"任务时用于打破平局）
    """

    def __init__(
        self,
        families: dict[str, "TaskFamily"],
        worker_family_map: dict[str, str] | None = None,
        seed: int | None = None,
    ) -> None:
        if not families:
            raise ValueError("families 不能为空")
        all_tasks: list["Task"] = [t for fam in families.values() for t in fam.tasks]
        super().__init__(tasks=all_tasks, seed=seed)
        self._families = families
        self._family_ids: list[str] = sorted(families.keys())
        self._worker_family_map: dict[str, str] = dict(worker_family_map or {})
        # 掌握度 + 巩固循环状态委托给独立的 CurriculumState（与本类的采样
        # 策略逻辑解耦，见 benchmark/curriculum_state/__init__.py）。
        self._state = CurriculumState()

    # ------------------------------------------------------------------
    # family 绑定（同 FamilyCurriculumSampler 语义）
    # ------------------------------------------------------------------

    def assign_family(self, worker_id: str, family_id: str) -> None:
        """显式为 worker 绑定 family（可在运行前调用）。"""
        if family_id not in self._families:
            raise KeyError(f"未知 family_id: {family_id}")
        self._worker_family_map[worker_id] = family_id

    def family_for(self, worker_id: str) -> str:
        """返回 worker 绑定的 family_id；未绑定时做确定性 round-robin 分配。"""
        if worker_id not in self._worker_family_map:
            idx = len(self._worker_family_map) % len(self._family_ids)
            self._worker_family_map[worker_id] = self._family_ids[idx]
            logger.info("worker=%s 首次分配 family=%s", worker_id, self._worker_family_map[worker_id])
        return self._worker_family_map[worker_id]

    # ------------------------------------------------------------------
    # 掌握度回写（调用方在拿到 verifier reward 后必须调用）
    # ------------------------------------------------------------------

    def record_result(self, worker_id: str, task_id: str, reward: float) -> None:
        """
        记录某个 worker 在某个 task 上的执行结果。

        对应论文 R_{i,x}(τ) —— reward>=1.0 视为"该任务已掌握"，
        影响下一次 sample() 的依赖判定与难度前进。
        """
        if reward >= 1.0:
            self._state.mark_solved(worker_id, task_id)
            logger.debug("worker=%s task=%s 已标记为掌握", worker_id, task_id)

    def solved_tasks(self, worker_id: str) -> set[str]:
        """返回 worker 当前已掌握的 task_id 集合（只读快照）。"""
        return self._state.solved_snapshot(worker_id)

    # ------------------------------------------------------------------
    # 采样主逻辑
    # ------------------------------------------------------------------

    def sample(self, worker_id: str, round_idx: int) -> "Task":
        family_id = self.family_for(worker_id)
        family = self._families[family_id]
        solved = self._state.solved_ids(worker_id)

        # 依赖图判定委托给独立的 benchmark.dependencies.eligible_tasks()
        # （纯函数，与本类的状态管理解耦，见 benchmark/dependencies/__init__.py）。
        eligible = _eligible_tasks_fn(family, solved)

        if eligible:
            # 依赖已满足且未掌握的任务中，取难度最低的一个
            task = min(eligible, key=lambda t: t.difficulty)
            logger.debug(
                "FamilyAwareSampler: worker=%s round=%d family=%s → %s (diff=%d, 掌握度驱动)",
                worker_id, round_idx, family_id, task.task_id, task.difficulty,
            )
            return task

        # family 内所有任务都已掌握 —— 支持"同一 skill 多轮演化"：
        # 重置 solved 集合，开启新一轮巩固循环，从最高难度任务开始重新演化
        cycle_n = self._state.start_new_cycle(worker_id)
        self._state.reset(worker_id)
        hardest = max(family.tasks, key=lambda t: t.difficulty)
        logger.info(
            "worker=%s family=%s 全部任务已掌握，开启第 %d 轮巩固循环（重置 solved）",
            worker_id, family_id, cycle_n,
        )
        return hardest

    def cycles_completed(self, worker_id: str) -> int:
        """该 worker 在其绑定 family 上完成的完整'掌握循环'次数（跨轮演化次数）。"""
        return self._state.cycles(worker_id)
