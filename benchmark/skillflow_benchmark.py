"""
skillflow_benchmark.py — SkillFlow 风格 family-aware 任务采样器

对应论文 Section 5.1 评估协议：
    "20 diverse task families, each containing a sequence of tasks of
     increasing difficulty that all require the SAME underlying skill
     to be progressively evolved."

本模块提供 FamilyTaskSampler，在 FamilyCurriculumSampler 基础上添加：
  1. 多 worker 多 family 绑定（每个 worker 可绑定不同 family）
  2. 分布统计（family/task distribution report）
  3. 重复访问保证（同一 family 在多轮中被重复访问）
  4. 可通过 `replicate` 模式让所有 worker 访问同一任务（对应官方 partitioner=replicate）

论文对应的 4 种 partitioner 模式：
  - "replicate"   ← 所有 worker 得到完全相同的任务（Settings 2-4 的官方默认）
  - "curriculum"  ← 每个 worker 绑定一个 family，按难度递增（FamilyCurriculumSampler 原有）
  - "random"      ← 随机抽取（RandomSampler）
  - "hetero"      ← 按 category 分配（HeterogeneousSampler）

用法::

    from benchmark.family import load_all_families
    from benchmark.skillflow_benchmark import FamilyTaskSampler

    families = load_all_families()
    sampler = FamilyTaskSampler(families, mode="replicate", seed=42)

    # Round 0: 所有 worker 得到同一任务
    for wid in ["u0", "u1", "u2"]:
        task = sampler.sample(wid, round_idx=0)
        print(task.task_id, task.difficulty)
"""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from typing import TYPE_CHECKING

from benchmark.curriculum import FamilyCurriculumSampler
from benchmark.sampler import TaskSampler

if TYPE_CHECKING:
    from benchmark.family import TaskFamily
    from benchmark.task import Task

logger = logging.getLogger(__name__)


class FamilyTaskSampler(TaskSampler):
    """
    SkillFlow-family-aware 任务采样器，支持多种分配模式。

    与 FamilyCurriculumSampler 的区别：
      - 支持 mode="replicate"：所有 worker 每轮得到相同任务（官方默认）
      - 支持 mode="curriculum"：每个 worker 独立 family 按难度递增
      - 支持 mode="random"：在所有 family 中随机选 task
      - 提供 distribution_report() 统计接口

    Args:
        families:          {family_id: TaskFamily}
        mode:              分配模式（"replicate" | "curriculum" | "random"）
        worker_family_map: {worker_id: family_id}；仅 curriculum 模式使用
        seed:              随机种子
    """

    def __init__(
        self,
        families: dict[str, "TaskFamily"],
        mode: str = "replicate",
        worker_family_map: dict[str, str] | None = None,
        seed: int | None = None,
    ) -> None:
        if not families:
            raise ValueError("families 不能为空")
        all_tasks: list["Task"] = [t for fam in families.values() for t in fam.tasks]
        super().__init__(tasks=all_tasks, seed=seed)

        self._families = families
        self._family_ids: list[str] = sorted(families.keys())
        self._mode = mode
        self._seed = seed
        self._rng = random.Random(seed)

        # replicate 模式：每轮（round_idx, family_id）→ 同一个 task
        self._replicate_cache: dict[tuple[int, str], "Task"] = {}

        # curriculum 模式委托给现有 FamilyCurriculumSampler
        self._curriculum_sampler = FamilyCurriculumSampler(
            families=families,
            worker_family_map=worker_family_map,
            seed=seed,
        )

        # 采样历史：{worker_id: [(round_idx, family_id, task_id, difficulty)]}
        self._history: dict[str, list[tuple]] = defaultdict(list)

        logger.info(
            "FamilyTaskSampler: mode=%s families=%d total_tasks=%d",
            mode, len(families), len(all_tasks),
        )

    # ------------------------------------------------------------------
    # 主接口
    # ------------------------------------------------------------------

    def sample(self, worker_id: str, round_idx: int) -> "Task":
        """
        按模式采样，返回本轮任务。

        所有模式都保证：同一 family 在多个 round 中被重复访问
        （curriculum / replicate 按难度递增；random 有概率重复同一 family）。
        """
        if self._mode == "replicate":
            task = self._sample_replicate(round_idx)
        elif self._mode == "curriculum":
            task = self._curriculum_sampler.sample(worker_id, round_idx)
        else:  # "random" or unknown → uniform random
            task = self._rng.choice(self.tasks)

        family_id = self._find_family(task)
        self._history[worker_id].append(
            (round_idx, family_id, task.task_id, task.difficulty)
        )
        logger.debug(
            "FamilyTaskSampler[%s]: worker=%s round=%d family=%s task=%s diff=%d",
            self._mode, worker_id, round_idx, family_id, task.task_id, task.difficulty,
        )
        return task

    # ------------------------------------------------------------------
    # replicate 模式
    # ------------------------------------------------------------------

    def _sample_replicate(self, round_idx: int) -> "Task":
        """
        replicate 模式：每轮所有 worker 得到同一个任务。

        逻辑：
          round_idx → family = family_ids[round_idx % n_families]
          task = family 中按 round_idx 难度递增的任务
        """
        family_id = self._family_ids[round_idx % len(self._family_ids)]
        cache_key = (round_idx, family_id)
        if cache_key not in self._replicate_cache:
            family = self._families[family_id]
            # 难度随 round_idx 递增（与 FamilyCurriculumSampler 相同逻辑）
            n_families = len(self._family_ids)
            round_within_family = round_idx // n_families  # 在同一 family 内的第几圈
            level = min(round_within_family + 1, len(family.tasks))
            self._replicate_cache[cache_key] = family.get_task_by_difficulty(level)
        return self._replicate_cache[cache_key]

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _find_family(self, task: "Task") -> str:
        """从任务反查 family_id（通过 task_id 前缀 or category 匹配）。"""
        for fid, fam in self._families.items():
            if any(t.task_id == task.task_id for t in fam.tasks):
                return fid
        return "unknown"

    # ------------------------------------------------------------------
    # 分布统计 API
    # ------------------------------------------------------------------

    def distribution_report(self) -> dict:
        """
        生成 family/task 分布统计报告。

        Returns::

            {
              "mode": "replicate",
              "families": {
                "data_cleaning": {
                  "task_count": 5,
                  "difficulty_range": [1, 5],
                  "sampled_workers": ["u0", "u1"],
                  "total_samples": 6,
                }
              },
              "per_worker": {
                "u0": {
                  "total_rounds": 3,
                  "family_visit_counts": {"data_cleaning": 2, "report_generation": 1},
                  "difficulty_progression": [1, 2, 3],
                }
              },
              "same_family_repeat_verified": True,
            }
        """
        family_stats: dict[str, dict] = {}
        for fid, fam in sorted(self._families.items()):
            difficulties = [t.difficulty for t in fam.tasks]
            family_stats[fid] = {
                "task_count": len(fam.tasks),
                "difficulty_range": [min(difficulties), max(difficulties)],
                "sampled_workers": [],
                "total_samples": 0,
            }

        per_worker: dict[str, dict] = {}
        for wid, history in sorted(self._history.items()):
            family_visits: dict[str, int] = defaultdict(int)
            difficulties = []
            for round_idx, family_id, task_id, diff in history:
                family_visits[family_id] += 1
                difficulties.append(diff)
                if family_id in family_stats:
                    family_stats[family_id]["total_samples"] += 1
                    if wid not in family_stats[family_id]["sampled_workers"]:
                        family_stats[family_id]["sampled_workers"].append(wid)
            per_worker[wid] = {
                "total_rounds": len(history),
                "family_visit_counts": dict(family_visits),
                "difficulty_progression": difficulties,
            }

        # 验证 5 轮后 difficulty 递增（curriculum / replicate 模式）
        repeat_verified = False
        if self._mode in ("replicate", "curriculum"):
            for wid, stats in per_worker.items():
                diffs = stats["difficulty_progression"]
                if len(diffs) >= 5:
                    # 允许钳制（难度上限后不再递增），只要非严格递减即可
                    non_decreasing = all(
                        diffs[i] <= diffs[i + 1] for i in range(len(diffs) - 1)
                    )
                    if non_decreasing:
                        repeat_verified = True
                        break
        else:
            # random 模式：检查是否有任何一个 family 被重复访问
            for stats in per_worker.values():
                if any(cnt > 1 for cnt in stats["family_visit_counts"].values()):
                    repeat_verified = True
                    break

        return {
            "mode": self._mode,
            "families": family_stats,
            "per_worker": per_worker,
            "same_family_repeat_verified": repeat_verified,
        }

    def verify_difficulty_progression(
        self,
        worker_id: str,
        min_rounds: int = 5,
    ) -> bool:
        """
        验证：在 min_rounds 轮后，同一 family 的难度是否递增。

        对应论文 Section 5.1：
            "a sequence of tasks of increasing difficulty"

        Returns:
            True  → 难度不严格递减（允许钳制）
            False → 有退步（难度突然下降，说明采样逻辑有误）
        """
        history = self._history.get(worker_id, [])
        if len(history) < min_rounds:
            return False
        # 按 family 分组，各自检查递增
        by_family: dict[str, list[int]] = defaultdict(list)
        for _, fid, _, diff in history:
            by_family[fid].append(diff)

        for fid, diffs in by_family.items():
            if len(diffs) >= 2:
                for i in range(len(diffs) - 1):
                    if diffs[i] > diffs[i + 1]:
                        logger.warning(
                            "难度退步: worker=%s family=%s round=%d diff %d → %d",
                            worker_id, fid, i, diffs[i], diffs[i + 1],
                        )
                        return False
        return True
