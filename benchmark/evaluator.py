"""
evaluator.py — Benchmark 轮次评估器（BenchmarkEvaluator）

计算论文 Table 2 / Figure 3 / Figure 4 中报告的核心指标：

  1. Success Rate        — 每轮成功率（论文主要指标）
  2. Skill Growth        — 技能库规模随轮增长
  3. Comm. Compression   — 通信压缩比（patch vs 完整库）
  4. Sample Efficiency   — 累积奖励 / 累积 trial 次数
  5. Heterogeneity Gain  — 联邦增益（联邦 SR - 单机 SR 基线）
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.datatypes import RoundRecord, WorkerPatch, LibrarySnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 单轮指标
# ---------------------------------------------------------------------------


@dataclass
class RoundMetrics:
    """对应论文 Table 2 中每轮的指标快照。"""

    round_idx: int
    family_name: str

    # Success Rate  SR^t = |{δ_i^t : R_{i,x}(τ) ≥ 1}| / N
    success_rate: float = 0.0
    per_worker_reward: dict[str, float] = field(default_factory=dict)

    # Skill Growth  |L_i^t| 平均值
    mean_skill_count: float = 0.0
    per_worker_skill_count: dict[str, int] = field(default_factory=dict)

    # Communication Cost  |δ_i^t| / |L_i^t|（patch 字节数 / 库字节数）
    mean_comm_compression: float = 0.0  # 1.0 = 无压缩，< 1 = 有压缩

    # Sample Efficiency  cumulative reward / cumulative trials
    cumulative_reward: float = 0.0
    cumulative_trials: int = 0
    sample_efficiency: float = 0.0

    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# 全局摘要
# ---------------------------------------------------------------------------


@dataclass
class ExperimentSummary:
    """实验完整运行的汇总指标。"""

    setting_name: str             # e.g. "Setting1_SE" / "Setting3_Hetero"
    family_name: str
    total_rounds: int
    worker_ids: list[str]

    # 每轮指标序列
    per_round: list[RoundMetrics] = field(default_factory=list)

    # 最终指标
    final_success_rate: float = 0.0
    final_mean_skill_count: float = 0.0
    total_cost_usd: float = 0.0
    total_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "setting_name": self.setting_name,
            "family_name": self.family_name,
            "total_rounds": self.total_rounds,
            "worker_ids": self.worker_ids,
            "final_success_rate": self.final_success_rate,
            "final_mean_skill_count": self.final_mean_skill_count,
            "total_cost_usd": self.total_cost_usd,
            "total_tokens": self.total_tokens,
            "per_round_success_rates": [r.success_rate for r in self.per_round],
            "per_round_mean_skill_counts": [r.mean_skill_count for r in self.per_round],
        }


# ---------------------------------------------------------------------------
# BenchmarkEvaluator
# ---------------------------------------------------------------------------


class BenchmarkEvaluator:
    """
    根据 RoundRecord 列表计算实验指标。

    设计为**无状态**：只做计算，不存储数据，便于多次调用。

    使用示例::

        evaluator = BenchmarkEvaluator()
        summary = evaluator.summarize(
            round_records=server.round_records,
            library_snapshots_per_round=snapshots,
            setting_name="Setting3_Hetero",
        )
        print(summary.final_success_rate)
    """

    def compute_round_metrics(
        self,
        record: "RoundRecord",
        library_snapshots: dict[str, "LibrarySnapshot"] | None = None,
        cumulative_reward: float = 0.0,
        cumulative_trials: int = 0,
    ) -> RoundMetrics:
        """
        计算单 round 的指标。

        Args:
            record:              本轮 RoundRecord
            library_snapshots:   本轮各 worker 的库快照（用于 Skill Growth）
            cumulative_reward:   截至本轮之前的累积奖励
            cumulative_trials:   截至本轮之前的累积 trial 次数

        Returns:
            RoundMetrics
        """
        patches = record.worker_patches or []
        n = len(patches) or 1  # 防止除零

        # ---- Success Rate ----
        per_worker_reward = {
            p.metadata.worker_id: (p.reward or 0.0) for p in patches
        }
        sr = sum(1 for r in per_worker_reward.values() if r >= 1.0) / n

        # ---- Skill Growth ----
        per_worker_skill_count: dict[str, int] = {}
        if library_snapshots:
            for wid, snap in library_snapshots.items():
                per_worker_skill_count[wid] = snap.skill_count
        mean_sc = (
            statistics.mean(per_worker_skill_count.values())
            if per_worker_skill_count
            else 0.0
        )

        # ---- Communication Compression Ratio ----
        compression_ratios: list[float] = []
        for p in patches:
            # patch 字节数 = upserts 文本长度总和
            patch_bytes = sum(len(v) for v in (p.upserts or {}).values())
            # 库字节数（从快照获取，fallback = 1 避免除零）
            wid = p.metadata.worker_id
            lib_bytes = (
                library_snapshots[wid].total_size_bytes
                if (library_snapshots and wid in library_snapshots and
                    library_snapshots[wid].total_size_bytes > 0)
                else max(patch_bytes, 1)
            )
            ratio = patch_bytes / lib_bytes
            compression_ratios.append(ratio)
        mean_comp = statistics.mean(compression_ratios) if compression_ratios else 1.0

        # ---- Sample Efficiency ----
        round_reward = sum(per_worker_reward.values())
        new_cum_reward = cumulative_reward + round_reward
        new_cum_trials = cumulative_trials + n
        eff = new_cum_reward / new_cum_trials if new_cum_trials > 0 else 0.0

        return RoundMetrics(
            round_idx=record.round_idx,
            family_name=record.family_name,
            success_rate=sr,
            per_worker_reward=per_worker_reward,
            mean_skill_count=mean_sc,
            per_worker_skill_count=per_worker_skill_count,
            mean_comm_compression=mean_comp,
            cumulative_reward=new_cum_reward,
            cumulative_trials=new_cum_trials,
            sample_efficiency=eff,
            elapsed_seconds=record.elapsed_seconds or 0.0,
        )

    def summarize(
        self,
        round_records: list["RoundRecord"],
        library_snapshots_per_round: list[dict[str, "LibrarySnapshot"]] | None = None,
        setting_name: str = "unknown",
        worker_ids: list[str] | None = None,
    ) -> ExperimentSummary:
        """
        对整个实验（多轮 RoundRecord）生成汇总指标。

        Args:
            round_records:                所有轮的 RoundRecord
            library_snapshots_per_round:  每轮的 {worker_id: LibrarySnapshot}
            setting_name:                 实验设置名称
            worker_ids:                   参与 worker ID 列表

        Returns:
            ExperimentSummary
        """
        if not round_records:
            family = "unknown"
        else:
            family = round_records[0].family_name

        if worker_ids is None and round_records:
            worker_ids = list(round_records[0].rewards.keys()) if round_records[0].rewards else []

        per_round: list[RoundMetrics] = []
        cum_reward = 0.0
        cum_trials = 0

        for i, record in enumerate(round_records):
            snaps = (
                library_snapshots_per_round[i]
                if library_snapshots_per_round and i < len(library_snapshots_per_round)
                else None
            )
            metrics = self.compute_round_metrics(
                record, snaps, cum_reward, cum_trials
            )
            per_round.append(metrics)
            cum_reward = metrics.cumulative_reward
            cum_trials = metrics.cumulative_trials

        # 最终指标
        final_sr = per_round[-1].success_rate if per_round else 0.0
        final_sc = per_round[-1].mean_skill_count if per_round else 0.0

        # 总 cost / tokens（从 patches 累计）
        total_cost = sum(
            p.cost_usd or 0.0
            for r in round_records
            for p in (r.worker_patches or [])
        )
        # 也把 merged patches 的 cost 算进去
        total_cost += sum(
            mp.cost_usd or 0.0
            for r in round_records
            for mp in (r.merged_patches or {}).values()
        )

        summary = ExperimentSummary(
            setting_name=setting_name,
            family_name=family,
            total_rounds=len(round_records),
            worker_ids=worker_ids or [],
            per_round=per_round,
            final_success_rate=final_sr,
            final_mean_skill_count=final_sc,
            total_cost_usd=total_cost,
        )
        logger.info(
            "实验汇总 [%s]: %d 轮, 最终 SR=%.3f, 平均技能数=%.1f, 总 cost=$%.4f",
            setting_name, len(round_records), final_sr, final_sc, total_cost,
        )
        return summary

    def heterogeneity_gain(
        self,
        federated_summary: ExperimentSummary,
        solo_summary: ExperimentSummary,
    ) -> float:
        """
        论文 Section 5.3 Heterogeneity Gain：
            ΔSR = SR_federated - SR_solo

        Args:
            federated_summary: 联邦设置的汇总（Setting 3 / 4）
            solo_summary:      单机基线的汇总（Setting 1）
        """
        gain = federated_summary.final_success_rate - solo_summary.final_success_rate
        logger.info(
            "Heterogeneity Gain: %.3f (fed=%.3f, solo=%.3f)",
            gain, federated_summary.final_success_rate, solo_summary.final_success_rate,
        )
        return gain
