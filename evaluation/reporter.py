"""
reporter.py — 实验结果输出器（ResultReporter）

将 ExperimentResult / 对比表 格式化为终端文本或 CSV 文件，
对应论文 Table 1 / Figure 2 / Figure 3 / Figure 4 的输出格式。
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from statistics import mean
from typing import Any

from evaluation.evaluator import ExperimentResult, RoundEvalResult

logger = logging.getLogger(__name__)


class ResultReporter:
    """
    实验结果格式化输出。

    使用示例::

        reporter = ResultReporter(verbose=True)
        reporter.print_round(round_result)
        reporter.print_summary(experiment_result)
        reporter.print_comparison({"SE": se_result, "FedSkill": fed_result})
        reporter.to_csv(experiment_result, "results/setting3.csv")
    """

    def __init__(self, verbose: bool = True) -> None:
        self._verbose = verbose

    # ------------------------------------------------------------------
    # 单轮输出
    # ------------------------------------------------------------------

    def print_round(self, result: RoundEvalResult) -> None:
        """打印单 round 指标（对应 Table 1 一行）。"""
        m = result.metrics
        lines = [
            f"── Round {result.round_idx:>2d} [{result.setting_name}] ──────────────",
            f"  Success Rate:        {m.get('success_rate', 0):.1%}  "
            f"({int(m.get('n_solved',0))}/{int(m.get('n_total',0))} tasks)",
            f"  Compression Ratio:   {m.get('compression_ratio', 0):.3f}  "
            f"(patch tokens / traj tokens)",
            f"  Privacy Gain:        {m.get('privacy_gain', 0):.3f}  "
            f"(token 压缩代理，非 SELR)",
            f"  Mean Library Size:   {m.get('mean_library_size', 0):.2f} skills",
            f"  Total Cost:          ${m.get('total_cost_usd', 0):.4f}",
        ]
        if self._verbose and result.per_worker:
            lines.append("  Per-worker SR:")
            for wid, wm in sorted(result.per_worker.items()):
                lines.append(f"    {wid}: {wm.get('success_rate', 0):.1%}")
        print("\n".join(lines))

    # ------------------------------------------------------------------
    # 实验汇总
    # ------------------------------------------------------------------

    def print_summary(self, result: ExperimentResult) -> None:
        """打印完整实验汇总（对应论文 Table 1 底行 + Figure 3 趋势）。"""
        fm = result.final_metrics
        header = f"\n{'='*55}"
        lines = [
            header,
            f"  实验设置: {result.setting_name}",
            f"  轮数:     {result.metadata.get('total_rounds', len(result.rounds))}",
            "─" * 55,
            f"  最终成功率:          {fm.get('success_rate', 0):.1%}",
            f"  全程成功率:          {fm.get('overall_success_rate', 0):.1%}",
            f"  通信压缩比 (CR):     {fm.get('compression_ratio', 0):.3f}",
            f"  隐私增益 (token代理,非SELR): {fm.get('privacy_gain', 0):.3f}",
            f"  最终平均库大小:      {fm.get('mean_library_size', 0):.2f} skills",
            f"  全程总成本:          ${fm.get('total_cost_usd', 0):.4f}",
            f"  每解决任务成本:      ${fm.get('cost_per_solved_task', 0):.4f}",
            "─" * 55,
            "  各轮成功率趋势:",
            "  " + " → ".join(f"{sr:.0%}" for sr in result.success_rates),
            header,
        ]
        print("\n".join(lines))

    # ------------------------------------------------------------------
    # 多 Setting 对比表（对应 Table 1）
    # ------------------------------------------------------------------

    def print_comparison(
        self,
        results: dict[str, ExperimentResult],
        baseline_key: str | None = None,
    ) -> None:
        """
        打印多 Setting 对比表，格式对应论文 Table 1。

        Args:
            results:       {setting_name: ExperimentResult}
            baseline_key:  SE 基线 key，用于计算 ΔSR
        """
        from evaluation.evaluator import ExperimentEvaluator
        comparison = ExperimentEvaluator.compare(results, baseline_key)

        # 表头
        keys = ["success_rate", "compression_ratio", "privacy_gain",
                "mean_library_size", "total_cost_usd", "delta_sr"]
        col_labels = ["SR", "CR", "PrivGain", "LibSz", "Cost($)", "ΔSR"]

        # 列宽
        name_w = max(len(n) for n in comparison) + 2
        col_w = 10

        header = f"{'Setting':<{name_w}}" + "".join(f"{c:>{col_w}}" for c in col_labels)
        sep = "─" * (name_w + col_w * len(col_labels))

        print(f"\n{'='*len(sep)}")
        print("  论文 Table 1 / Figure 2 / Figure 4 对比指标")
        print(f"  baseline: {baseline_key or '(none)'}")
        print(sep)
        print(header)
        print(sep)
        for name, row in sorted(comparison.items()):
            vals = []
            for k in keys:
                v = row.get(k)
                if v is None:
                    vals.append("─" * 6)
                elif k == "total_cost_usd":
                    vals.append(f"${v:.4f}")
                elif k == "mean_library_size":
                    vals.append(f"{v:.2f}")
                else:
                    vals.append(f"{v:+.3f}" if "delta" in k else f"{v:.3f}")
            print(f"{name:<{name_w}}" + "".join(f"{v:>{col_w}}" for v in vals))
        print(f"{'='*len(sep)}\n")

    # ------------------------------------------------------------------
    # CSV 导出（可用于 matplotlib 绘图）
    # ------------------------------------------------------------------

    def to_csv(
        self,
        result: ExperimentResult,
        path: str | Path,
    ) -> None:
        """
        导出每轮指标为 CSV，便于用 matplotlib 复现论文 Figure 2/3/4。

        列：round_idx, success_rate, compression_ratio, privacy_gain,
             mean_library_size, total_cost_usd, n_solved, n_total
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "round_idx", "setting_name",
            "success_rate", "compression_ratio", "privacy_gain",
            "mean_library_size", "mean_skill_growth",
            "total_cost_usd", "cost_per_solved_task",
            "n_solved", "n_total",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in result.rounds:
                row = {"round_idx": r.round_idx, "setting_name": r.setting_name}
                row.update(r.metrics)
                writer.writerow(row)

        logger.info("指标 CSV 已写入: %s (%d 轮)", path, len(result.rounds))

    # ------------------------------------------------------------------
    # 多 Setting 对比 CSV
    # ------------------------------------------------------------------

    def comparison_to_csv(
        self,
        results: dict[str, ExperimentResult],
        path: str | Path,
        baseline_key: str | None = None,
    ) -> None:
        """导出对比表为 CSV（对应论文 Table 1 格式）。"""
        from evaluation.evaluator import ExperimentEvaluator
        comparison = ExperimentEvaluator.compare(results, baseline_key)

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if not comparison:
            return
        fieldnames = ["setting_name"] + list(next(iter(comparison.values())).keys())
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for name, row in comparison.items():
                writer.writerow({"setting_name": name, **row})

        logger.info("对比 CSV 已写入: %s", path)

    # ------------------------------------------------------------------
    # Family 级跨 Setting 论文表格导出（对应论文 Table 1 / Table 2）
    #
    # 与上面 print_comparison/comparison_to_csv 不同：那两个方法消费的是
    # 单次实验运行内存中的 ExperimentResult（setting 粒度，一个 setting
    # 一条轮次序列）。这里的两个新方法消费的是**多个** family_loop 模式
    # 的 experiment_summary.json（每个 setting 一个实验目录，目录内含多
    # 个 family），产出 family × setting 矩阵，对应论文 Table 1（每个
    # family 在 4 个 Setting 下的成功率）和 Table 2（每个 Setting 相对
    # SE baseline 的增益与 Win/Tie/Lose 统计）。
    #
    # 复用 experiments/aggregation.py::extract_family_rows()（已有、有
    # 测试覆盖的 family 级成功率解析逻辑），不重新实现
    # experiment_summary.json 解析规则，避免出现两套口径不一致的成功率。
    # ------------------------------------------------------------------

    def export_paper_table1(
        self,
        setting_dirs: dict[str, str | Path],
        path: str | Path,
    ) -> Path:
        """
        导出论文 Table 1 格式的 family x setting 成功率矩阵。

        Args:
            setting_dirs: {setting_key: experiment_dir}。experiment_dir
                必须是 experiments/aggregation.py::extract_family_rows()
                能识别的实验结果目录（--family 单次运行对应的 experiment_id
                根目录，或一次跑完全部 family 的 loop_over_families 实验
                目录）。setting_key 建议使用语义化名字，例如
                "se_baseline" / "fed_homogeneous" / "fed_hetero_model" /
                "fed_hetero_full"，会原样作为 CSV 列名。
            path: 输出 CSV 路径。

        Returns:
            实际写入的 CSV 路径。

        CSV 列：family_name, <每个 setting_key 一列>, _note。
        末尾追加一行 family_name="average_all"，每个 setting 列取该列
        （跨全部 family）的算术平均；某个 family 在某个 setting 下缺失
        结果时该单元格留空，并在 _note 列标注缺失了哪些 setting。
        """
        from experiments.aggregation import extract_family_rows

        per_setting: dict[str, dict[str, float]] = {}
        for setting_key, exp_dir in setting_dirs.items():
            rows = extract_family_rows(Path(exp_dir))
            per_setting[setting_key] = {r["family_id"]: r["success_rate"] for r in rows}

        setting_keys = list(setting_dirs.keys())
        all_families = sorted(set().union(*(d.keys() for d in per_setting.values()))) if per_setting else []

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["family_name", *setting_keys, "_note"]

        column_sums = {k: 0.0 for k in setting_keys}
        column_counts = {k: 0 for k in setting_keys}

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for family in all_families:
                row: dict[str, Any] = {"family_name": family}
                missing_in: list[str] = []
                for key in setting_keys:
                    sr = per_setting[key].get(family)
                    if sr is None:
                        row[key] = ""
                        missing_in.append(key)
                    else:
                        row[key] = round(sr, 4)
                        column_sums[key] += sr
                        column_counts[key] += 1
                row["_note"] = f"missing: {','.join(missing_in)}" if missing_in else ""
                writer.writerow(row)

            avg_row: dict[str, Any] = {"family_name": "average_all", "_note": ""}
            for key in setting_keys:
                avg_row[key] = round(column_sums[key] / column_counts[key], 4) if column_counts[key] else ""
            writer.writerow(avg_row)

        logger.info(
            "论文 Table 1 CSV 已写入: %s (%d families x %d settings)",
            path, len(all_families), len(setting_keys),
        )
        return path

    def export_paper_table2(
        self,
        setting_dirs: dict[str, str | Path],
        baseline_key: str,
        path: str | Path,
    ) -> Path:
        """
        导出论文 Table 2 格式的增益 / Win-Tie-Lose 对比表。

        Args:
            setting_dirs: 同 export_paper_table1。
            baseline_key: setting_dirs 中作为 SE baseline 的 key（如
                "se_baseline"），用于计算 gain_vs_se 和逐 family 的
                Win/Tie/Lose。
            path: 输出 CSV 路径。

        Returns:
            实际写入的 CSV 路径。

        CSV 列：setting_name, avg_success_rate, gain_vs_se, win_count,
        tie_count, lose_count, wtl_string。baseline 自身不输出一行
        （其余每个 setting 都是相对 baseline 比较后的一行）。
        Win/Tie/Lose 只统计该 setting 与 baseline 都有结果的 family（按
        逐 family 成功率相比：高于 baseline 记 Win，等于记 Tie，低于
        记 Lose）。
        """
        from experiments.aggregation import extract_family_rows

        if baseline_key not in setting_dirs:
            raise ValueError(f"baseline_key={baseline_key!r} 不在 setting_dirs 中")

        per_setting: dict[str, dict[str, float]] = {}
        for setting_key, exp_dir in setting_dirs.items():
            rows = extract_family_rows(Path(exp_dir))
            per_setting[setting_key] = {r["family_id"]: r["success_rate"] for r in rows}

        baseline_rates = per_setting[baseline_key]
        baseline_avg = mean(baseline_rates.values()) if baseline_rates else 0.0

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "setting_name", "avg_success_rate", "gain_vs_se",
            "win_count", "tie_count", "lose_count", "wtl_string",
        ]

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for setting_key, rates in per_setting.items():
                if setting_key == baseline_key:
                    continue
                avg_sr = mean(rates.values()) if rates else 0.0
                common_families = sorted(set(rates) & set(baseline_rates))
                win = tie = lose = 0
                for family in common_families:
                    if rates[family] > baseline_rates[family]:
                        win += 1
                    elif rates[family] < baseline_rates[family]:
                        lose += 1
                    else:
                        tie += 1
                writer.writerow({
                    "setting_name": setting_key,
                    "avg_success_rate": round(avg_sr, 4),
                    "gain_vs_se": round(avg_sr - baseline_avg, 4),
                    "win_count": win,
                    "tie_count": tie,
                    "lose_count": lose,
                    "wtl_string": f"{win}/{tie}/{lose}",
                })

        logger.info("论文 Table 2 CSV 已写入: %s", path)
        return path
