"""
curriculum.py — FamilyCurriculumSampler（SkillFlow 风格课程采样器）

对应论文 Section 5.1 的评估协议：每个 client 在一个 task family 内
按难度递增的顺序逐轮执行任务，迫使 L_i^t 针对同一技能持续演化
（而不是像 RandomSampler / DifficultyAwareSampler 那样在互不相关的
任务间随机跳转）。

用法::

    from benchmark.family import load_all_families
    from benchmark.curriculum import FamilyCurriculumSampler

    families = load_all_families()  # 默认读取 benchmark/families/
    sampler = FamilyCurriculumSampler(families, seed=42)
    task = sampler.sample("u0", round_idx=2)   # u0 绑定的 family 中难度=3 的任务
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from benchmark.sampler import TaskSampler

if TYPE_CHECKING:
    from benchmark.family import TaskFamily
    from benchmark.task import Task

logger = logging.getLogger(__name__)


class FamilyCurriculumSampler(TaskSampler):
    """
    每个 worker 固定绑定一个 task family，随 round_idx 递增采样该
    family 内难度递增的任务；超过 family 最大难度后钳制在最高难度任务
    （代表"持续巩固已演化的技能"，避免越界）。

    Args:
        families:          {family_id: TaskFamily}
        worker_family_map: {worker_id: family_id}；未显式指定的 worker
                            按首次调用的顺序轮转分配（确定性 round-robin）
        seed:               随机种子（本采样器本身是确定性的，仅为兼容
                            TaskSampler 基类接口保留）
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

    def assign_family(self, worker_id: str, family_id: str) -> None:
        """显式为 worker 绑定 family（可在运行前调用）。"""
        if family_id not in self._families:
            raise KeyError(f"未知 family_id: {family_id}")
        self._worker_family_map[worker_id] = family_id

    def family_for(self, worker_id: str) -> str:
        """
        返回 worker 绑定的 family_id；若未绑定，按"已绑定 worker 数量"
        对 family 列表取模，做确定性 round-robin 分配（同一批 worker_id
        调用顺序不变时结果可复现，不依赖 hash 随机化）。
        """
        if worker_id not in self._worker_family_map:
            idx = len(self._worker_family_map) % len(self._family_ids)
            self._worker_family_map[worker_id] = self._family_ids[idx]
            logger.info("worker=%s 首次分配 family=%s", worker_id, self._worker_family_map[worker_id])
        return self._worker_family_map[worker_id]

    def sample(self, worker_id: str, round_idx: int) -> "Task":
        family_id = self.family_for(worker_id)
        family = self._families[family_id]
        level = min(round_idx + 1, len(family.tasks))
        task = family.get_task_by_difficulty(level)
        logger.debug(
            "FamilyCurriculumSampler: worker=%s round=%d family=%s level=%d → %s",
            worker_id, round_idx, family_id, level, task.task_id,
        )
        return task


# Phase12：按用户要求的命名对外暴露 CurriculumSampler（等同 FamilyCurriculumSampler，
# 不重命名/不改动已通过测试的原类，纯别名，保持向后兼容）。
CurriculumSampler = FamilyCurriculumSampler

# Official Implementation Alignment Audit（本轮新增）：
# `SkillFlowFamilySampler` 是 FamilyCurriculumSampler 的纯别名——推荐用于
# Setting1-4 主实验的采样器。之所以推荐它而不是 FamilyAwareSampler：
#   - 纯粹按 round_idx 前进，不引入依赖图/掌握度门控/巩固循环这些
#     Phase12 自建、非论文/官方要求的机制（对比见 docs/SIMPLIFICATIONS.md §2.4）。
#   - 语义上最贴近论文 Section 5.1"family 内难度递增任务序列"的原始描述，
#     以及官方 `skillfl/skillflow_adapter/partitioning.py` 的静态划分精神
#     （只是本复现用 round_idx 驱动，而非 worker 数量做划分，属于合理的
#     "per-worker curriculum" 变体，与官方 BlockPartitioner 的设计意图一致）。
# 不重新实现，纯别名，避免维护两份相同逻辑。
SkillFlowFamilySampler = FamilyCurriculumSampler

