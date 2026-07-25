"""
capability_tracker.py — Capability Matrix 演化追踪器（跨轮次历史 + CSV 导出）

[EXTENSION]
Tracks transitions of the paper-defined capability matrix. Visualization and
CSV export are additional evaluation utilities.

分类结论：`CapabilityMatrix` 的 covered/absorbing/broken/gap 四态本身来自论文
Section 4.2.1（见下方引用），但本模块提供的 covered_count / absorbing_count /
broken_count / gap_count 计数、跨轮 transition 曲线、CSV 导出是**评估层的统计
工具**，不是论文定义的算法组件——论文没有规定要如何统计/导出这四态的分布，
这是本项目为了产出报告图表而自建的分析工具。不要写成"实现了论文的能力演化
机制"（implements paper capability evolution）——本模块只追踪/可视化已有的
论文定义状态，不产生新的演化决策。

Phase13 任务4：记录 covered / absorbing / broken / gap 四种状态在每轮的分布，
并生成 capability evolution CSV，对应论文 Section 4.2.1 描述的：

    'Each cell in C^t records how well a client has mastered a specific
     workflow, assigning one of four states: covered, absorbing, broken, or gap.'

与 server/capability.py::CapabilityTracker 的分工：
    server.capability.CapabilityTracker  —— 单轮"当前状态"的可变追踪器，
                                             供 Stage1 EvolutionPlanner 实时查询/更新，
                                             状态定义直接对应论文 Section 4.2.1。
    evaluation.capability_tracker.CapabilityEvolutionTracker —— 只读历史记录器，
                                             每轮结束后摄入一份 CapabilityMatrix 快照，
                                             用于产出跨轮次的 Figure（Capability Matrix
                                             evolution）和 CSV 表格，不参与决策逻辑，
                                             属于 [EXTENSION]。

两者不冲突、不重复：后者的输入正是前者每轮 to_capability_matrix() 的输出。
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.datatypes import CapabilityMatrix, CapabilityState

logger = logging.getLogger(__name__)


@dataclass
class RoundCapabilitySummary:
    """单轮的四态计数快照（全局 + 可选按 workflow / worker 拆分）。"""

    round_idx: int
    family_name: str
    covered: int = 0
    absorbing: int = 0
    broken: int = 0
    gap: int = 0
    per_worker: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.covered + self.absorbing + self.broken + self.gap

    @property
    def coverage_ratio(self) -> float:
        """covered / total —— 论文 Figure 的核心曲线：覆盖率随轮次上升。"""
        if self.total == 0:
            return 0.0
        return self.covered / self.total

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "round_idx": self.round_idx,
            "family_name": self.family_name,
            "covered": self.covered,
            "absorbing": self.absorbing,
            "broken": self.broken,
            "gap": self.gap,
            "total": self.total,
            "coverage_ratio": round(self.coverage_ratio, 4),
        }


class CapabilityEvolutionTracker:
    """
    跨轮次 Capability Matrix 历史记录器。

    用法::

        tracker = CapabilityEvolutionTracker()
        for round_idx in range(rounds):
            ... # 跑一轮，capability_tracker: server.capability.CapabilityTracker
            matrix = capability_tracker.to_capability_matrix(round_idx)
            tracker.record(matrix)
        tracker.to_csv("results/setting4/capability_evolution.csv")
    """

    def __init__(self) -> None:
        self._history: list[RoundCapabilitySummary] = []

    def record(self, matrix: CapabilityMatrix) -> RoundCapabilitySummary:
        """
        摄入一份某轮的 CapabilityMatrix 快照，统计四态计数。

        Args:
            matrix: 通常来自 server.capability.CapabilityTracker.to_capability_matrix()

        Returns:
            本轮的 RoundCapabilitySummary（同时被追加到内部历史）
        """
        counts = {state: 0 for state in CapabilityState}
        per_worker: dict[str, dict[str, int]] = {}

        for cell in matrix.cells:
            counts[cell.state] += 1
            wcounts = per_worker.setdefault(
                cell.worker_id, {s.value: 0 for s in CapabilityState}
            )
            wcounts[cell.state.value] += 1

        summary = RoundCapabilitySummary(
            round_idx=matrix.round_idx,
            family_name=matrix.family_name,
            covered=counts[CapabilityState.COVERED],
            absorbing=counts[CapabilityState.ABSORBING],
            broken=counts[CapabilityState.BROKEN],
            gap=counts[CapabilityState.GAP],
            per_worker=per_worker,
        )
        self._history.append(summary)
        logger.debug(
            "CapabilityEvolutionTracker: round=%d covered=%d absorbing=%d broken=%d gap=%d",
            summary.round_idx, summary.covered, summary.absorbing,
            summary.broken, summary.gap,
        )
        return summary

    @property
    def history(self) -> list[RoundCapabilitySummary]:
        return list(self._history)

    def latest(self) -> RoundCapabilitySummary | None:
        return self._history[-1] if self._history else None

    def coverage_trend(self) -> list[float]:
        """每轮 coverage_ratio 序列，用于绘制能力覆盖率曲线。"""
        return [s.coverage_ratio for s in self._history]

    # ------------------------------------------------------------------
    # CSV 导出（capability evolution csv）
    # ------------------------------------------------------------------

    def to_csv(self, path: str | Path) -> Path:
        """
        导出每轮四态计数为 CSV。

        列：round_idx, family_name, covered, absorbing, broken, gap, total, coverage_ratio
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "round_idx", "family_name", "covered", "absorbing",
            "broken", "gap", "total", "coverage_ratio",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for summary in self._history:
                writer.writerow(summary.to_dict())

        logger.info("Capability evolution CSV 已写入: %s (%d 轮)", path, len(self._history))
        return path

    def per_worker_to_csv(self, path: str | Path) -> Path:
        """
        导出按 worker 拆分的四态计数为 CSV（细粒度版本）。

        列：round_idx, family_name, worker_id, covered, absorbing, broken, gap
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "round_idx", "family_name", "worker_id",
            "covered", "absorbing", "broken", "gap",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for summary in self._history:
                for wid, counts in sorted(summary.per_worker.items()):
                    writer.writerow({
                        "round_idx": summary.round_idx,
                        "family_name": summary.family_name,
                        "worker_id": wid,
                        "covered": counts.get("covered", 0),
                        "absorbing": counts.get("absorbing", 0),
                        "broken": counts.get("broken", 0),
                        "gap": counts.get("gap", 0),
                    })

        logger.info("Per-worker capability evolution CSV 已写入: %s", path)
        return path
