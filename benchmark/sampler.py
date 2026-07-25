"""
sampler.py — 任务分配采样器

对应论文中不同实验设置下客户端的任务分配策略。

论文中的四种实验设置（Section 5）：
  Setting 1 (SE)             — 单客户端，随机任务
  Setting 2 (Homo-Fed)       — 多客户端同质，随机任务
  Setting 3 (Hetero-backbone)— 多客户端异质 backbone，同类别任务
  Setting 4 (Full-hetero)    — 多客户端完全异质，分类别任务

每种 Sampler 对应一种分配策略，通过 TaskSampler.sample_batch() 批量返回本轮分配。
"""

from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from benchmark.task import Task

logger = logging.getLogger(__name__)


class TaskSampler(ABC):
    """任务采样器抽象基类。"""

    def __init__(self, tasks: list["Task"], seed: int | None = None) -> None:
        if not tasks:
            raise ValueError("任务列表不能为空")
        self.tasks = tasks
        self._rng = random.Random(seed)

    @abstractmethod
    def sample(self, worker_id: str, round_idx: int) -> "Task":
        """为单个 worker 采样本轮任务。"""

    def sample_batch(
        self, worker_ids: list[str], round_idx: int
    ) -> dict[str, "Task"]:
        """
        为所有 worker 批量采样，返回 {worker_id: Task}。

        Args:
            worker_ids: 参与本轮的 worker ID 列表
            round_idx:  当前 round 序号

        Returns:
            每个 worker 的本轮任务分配
        """
        return {wid: self.sample(wid, round_idx) for wid in worker_ids}


class RandomSampler(TaskSampler):
    """
    随机任务采样器。

    对应论文 Setting 1 / Setting 2（同质联邦）。
    每个 worker 每轮独立均匀随机抽取一个任务。
    """

    def sample(self, worker_id: str, round_idx: int) -> "Task":
        task = self._rng.choice(self.tasks)
        logger.debug("RandomSampler: worker=%s round=%d → %s", worker_id, round_idx, task.task_id)
        return task


class HeterogeneousSampler(TaskSampler):
    """
    异质类别采样器。

    对应论文 Setting 3 / Setting 4（异质联邦）。
    将任务按 category 分组，每个 worker 绑定一个（或多个）类别，
    只从绑定类别中采样 —— 模拟真实场景中"每个 client 专注不同类别任务"。

    Args:
        tasks:           所有任务
        worker_categories: {worker_id: [category, ...]}；None → 轮转分配
        seed:            随机种子
    """

    def __init__(
        self,
        tasks: list["Task"],
        worker_categories: dict[str, list[str]] | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__(tasks, seed)
        # 按 category 建立索引
        self._by_category: dict[str, list["Task"]] = defaultdict(list)
        for t in tasks:
            self._by_category[t.category].append(t)
        self._worker_categories: dict[str, list[str]] = worker_categories or {}
        self._all_categories = list(self._by_category.keys())

    def assign_category(self, worker_id: str, categories: list[str]) -> None:
        """为 worker 绑定任务类别（可动态调用）。"""
        self._worker_categories[worker_id] = categories

    def sample(self, worker_id: str, round_idx: int) -> "Task":
        cats = self._worker_categories.get(worker_id)
        if not cats:
            # 未绑定的 worker：轮转分配类别
            idx = abs(hash(worker_id)) % len(self._all_categories)
            cats = [self._all_categories[idx]]
            self._worker_categories[worker_id] = cats

        # 从绑定类别中随机选一个有任务的类别
        valid_cats = [c for c in cats if self._by_category.get(c)]
        if not valid_cats:
            logger.warning("worker=%s 绑定类别无可用任务，回退到全局随机", worker_id)
            task = self._rng.choice(self.tasks)
        else:
            cat = self._rng.choice(valid_cats)
            task = self._rng.choice(self._by_category[cat])

        logger.debug("HeterogeneousSampler: worker=%s round=%d → %s", worker_id, round_idx, task.task_id)
        return task


class DifficultyAwareSampler(TaskSampler):
    """
    难度递进采样器。

    [EXTENSION] 分类结论：论文没有 "difficulty threshold" 概念，也没有
    "if reward > x: increase difficulty" 这类基于 reward 的难度晋升规则；本模块
    的固定轮次阈值调度是本项目自建的 ablation 工具，不是论文或官方实现的一部分。

    随着 round_idx 增大，逐渐提升采样任务的难度下限，
    模拟 skill evolution 后 agent 能够处理更难任务的场景。

    难度调度：
      rounds 0–2   → difficulty 1–2（简单）
      rounds 3–5   → difficulty 1–3（中等）
      rounds 6+    → difficulty 1–5（全部）

    ⚠️ Official Implementation Alignment Audit 标注：`_DIFFICULTY_SCHEDULE`
    的具体阈值是本复现自定的经验值（论文正文未给出逐轮难度调度公式，见
    docs/SIMPLIFICATIONS.md §2.3），且官方 `TaskPartitioner` 系列也没有这种
    "随轮次动态收紧难度上限"的机制。因此本采样器 **仅用于 ablation 研究**
    （对比"难度调度策略"这一个变量对结果的影响），不用于 Setting1-4 主实验——
    主实验请使用 `benchmark.curriculum.SkillFlowFamilySampler`。
    `ABLATION_ONLY = True` 供调用方（如 main_trainer.py::_build_sampler()）
    做运行时守卫，避免被误用于主实验配置。
    """

    ABLATION_ONLY: bool = True

    _DIFFICULTY_SCHEDULE: list[tuple[int, int]] = [
        (3, 2),   # round < 3 → max_difficulty = 2
        (6, 3),   # round < 6 → max_difficulty = 3
        (999, 5), # 其余      → max_difficulty = 5
    ]

    def sample(self, worker_id: str, round_idx: int) -> "Task":
        max_diff = 5
        for threshold, d in self._DIFFICULTY_SCHEDULE:
            if round_idx < threshold:
                max_diff = d
                break

        eligible = [t for t in self.tasks if t.difficulty <= max_diff]
        if not eligible:
            eligible = self.tasks  # 降级保护

        task = self._rng.choice(eligible)
        logger.debug(
            "DifficultyAwareSampler: worker=%s round=%d max_diff=%d → %s",
            worker_id, round_idx, max_diff, task.task_id,
        )
        return task
