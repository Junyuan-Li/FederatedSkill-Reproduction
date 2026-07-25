"""
evaluator.py — 实验评估器（ExperimentEvaluator）

收集每轮 trial 数据，调用 FederatedMetrics 计算指标，
输出标准化的 RoundEvalResult / ExperimentResult 结构。

与 benchmark/evaluator.py 的分工：
  benchmark/evaluator.py → 从已有 RoundRecord 对象计算指标（离线）
  evaluation/evaluator.py → 在实验运行时实时收集数据（在线），
                           并支持多 Setting 对比（SE vs FederatedSkill）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from evaluation.federated_score import weighted_global_score
from evaluation.metrics import FederatedMetrics, TrialSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass
class RoundEvalResult:
    """单 round 的指标快照。"""

    round_idx: int
    setting_name: str
    metrics: dict[str, float] = field(default_factory=dict)         # 汇总指标
    per_worker: dict[str, dict[str, float]] = field(default_factory=dict)  # 各 worker 指标
    snapshots: list[TrialSnapshot] = field(default_factory=list)


@dataclass
class ExperimentResult:
    """
    完整实验结果（多轮累积）。

    对应论文 Table 1 中一列（e.g., Qwen3.6-Plus FedSkill 的完整 8 轮）。
    """

    setting_name: str
    rounds: list[RoundEvalResult] = field(default_factory=list)
    final_metrics: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # 便捷属性
    @property
    def success_rates(self) -> list[float]:
        """每轮成功率序列，对应 Figure 2 / Table 1。"""
        return [r.metrics.get("success_rate", 0.0) for r in self.rounds]

    @property
    def library_sizes(self) -> list[float]:
        """每轮平均技能库大小，对应 Figure 3。"""
        return [r.metrics.get("mean_library_size", 0.0) for r in self.rounds]

    @property
    def final_success_rate(self) -> float:
        return self.final_metrics.get("success_rate", 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setting_name": self.setting_name,
            "final_success_rate": self.final_success_rate,
            "per_round_success_rates": self.success_rates,
            "per_round_library_sizes": self.library_sizes,
            "final_metrics": self.final_metrics,
            "total_rounds": len(self.rounds),
        }


# ---------------------------------------------------------------------------
# ExperimentEvaluator
# ---------------------------------------------------------------------------


class ExperimentEvaluator:
    """
    实验评估器：在 Runner 每轮调用 record_round()，实验结束调用 finalize()。

    用法示例::

        evaluator = ExperimentEvaluator(setting_name="Setting3_Hetero")
        for t in range(rounds):
            # ... 执行 trial ...
            evaluator.record_round(round_idx=t, snapshots=this_round_snapshots)
        result = evaluator.finalize()
    """

    def __init__(self, setting_name: str = "unknown") -> None:
        self.setting_name = setting_name
        self._round_results: list[RoundEvalResult] = []

    def record_round(
        self,
        round_idx: int,
        snapshots: list[TrialSnapshot],
    ) -> RoundEvalResult:
        """
        记录一轮数据并计算该轮指标。

        Args:
            round_idx:  当前 round 序号
            snapshots:  本轮所有 worker 的 TrialSnapshot 列表

        Returns:
            RoundEvalResult（已计入内部记录）
        """
        # 全局指标
        metrics = FederatedMetrics.compute_all(snapshots)

        # Per-worker 指标
        per_worker: dict[str, dict[str, float]] = {}
        worker_ids = set(s.worker_id for s in snapshots)
        for wid in worker_ids:
            w_snaps = [s for s in snapshots if s.worker_id == wid]
            per_worker[wid] = FederatedMetrics.compute_all(w_snaps)

        # Phase14 新增：论文 Eq.(3) J̄^t = Σ_i q_i·J_i(L_i^t)，默认均分权重
        # （q_i=1/n，J_i 取该 worker 本轮 success_rate）。作为 metrics 字典的
        # 新增键，不替代、不影响已有的 "success_rate" 等全局汇总指标。
        if per_worker:
            local_scores = {wid: m.get("success_rate", 0.0) for wid, m in per_worker.items()}
            metrics["weighted_global_score"] = weighted_global_score(local_scores)

        result = RoundEvalResult(
            round_idx=round_idx,
            setting_name=self.setting_name,
            metrics=metrics,
            per_worker=per_worker,
            snapshots=snapshots,
        )
        self._round_results.append(result)

        logger.info(
            "Round %d [%s]: SR=%.3f  CR=%.3f  PrivGain=%.3f  MeanLibSz=%.1f",
            round_idx, self.setting_name,
            metrics.get("success_rate", 0),
            metrics.get("compression_ratio", 0),
            metrics.get("privacy_gain", 0),
            metrics.get("mean_library_size", 0),
        )
        return result

    def finalize(self) -> ExperimentResult:
        """
        汇总所有轮次，计算最终指标（取末轮 SR / 全程累积 cost 等）。

        Returns:
            ExperimentResult
        """
        if not self._round_results:
            return ExperimentResult(setting_name=self.setting_name)

        # 最终轮次指标作为 final_metrics 基础
        last = self._round_results[-1]
        final = dict(last.metrics)

        # 补充全程累积指标
        all_snaps = [s for r in self._round_results for s in r.snapshots]
        final["total_cost_usd"] = sum(s.cost_usd for s in all_snaps)
        final["total_n_solved"] = float(sum(s.reward >= 1.0 for s in all_snaps))
        final["total_n_trials"] = float(len(all_snaps))
        final["overall_success_rate"] = (
            final["total_n_solved"] / max(final["total_n_trials"], 1)
        )

        result = ExperimentResult(
            setting_name=self.setting_name,
            rounds=list(self._round_results),
            final_metrics=final,
            metadata={"total_rounds": len(self._round_results)},
        )
        logger.info(
            "实验结束 [%s]: 共 %d 轮, 最终 SR=%.3f, 总 cost=$%.4f",
            self.setting_name, len(self._round_results),
            final.get("success_rate", 0), final.get("total_cost_usd", 0),
        )
        return result

    # ------------------------------------------------------------------
    # 多 Setting 对比（用于 Reporter.print_comparison）
    # ------------------------------------------------------------------

    @staticmethod
    def compare(
        results: dict[str, ExperimentResult],
        baseline_key: str | None = None,
    ) -> dict[str, dict[str, float]]:
        """
        对比多个设置的最终指标，可选计算相对基线的 Heterogeneity Gain。

        Args:
            results:       {setting_name: ExperimentResult}
            baseline_key:  SE 基线的 key（用于计算 ΔSR）

        Returns:
            {setting_name: {metric: value, "delta_sr": ...}}
        """
        comparison: dict[str, dict[str, float]] = {}
        baseline_sr = (
            results[baseline_key].final_success_rate
            if baseline_key and baseline_key in results
            else None
        )
        for name, res in results.items():
            row = dict(res.final_metrics)
            if baseline_sr is not None:
                row["delta_sr"] = FederatedMetrics.heterogeneity_gain(
                    res.final_success_rate, baseline_sr
                )
            comparison[name] = row
        return comparison
