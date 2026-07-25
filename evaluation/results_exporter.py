"""
results_exporter.py — 论文结果表格与图表生成器

读取：results/<setting_name>/round_*.json（由 run_experiment.py 写入）
生成：
  results/tables/
    success_rate.csv      — 对应论文 Table 1 / Figure 2
    communication.csv     — 对应论文 Appendix C / Table 6
    privacy.csv           — 对应论文 Appendix E / Table 8
    skill_growth.csv      — 对应论文 Figure 3

  results/figures/
    figure_success_curve.png  — Figure 2 复现
    figure_skill_growth.png   — Figure 3 复现
    figure_compression.png    — Figure 4 风格（通信压缩比）

论文指标对应关系：
  Table 1 · Success Rate:    SR = N_success / N_total  (Section 4.1.1)
  Table 6 · Compression:     CR = 1 - |patch| / |traj| (Appendix C)
  Table 8 · Privacy (SELR):  从 evaluation.privacy 模块扫描
  Figure 3· Library Size:    mean(|L_i^t|) per round
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class RoundRecord:
    """单 round 的聚合记录（从 JSON 文件解析）。"""

    setting_name: str
    round_idx: int
    family: str = "unknown"
    worker_id: str = "all"

    # Table 1 / Figure 2
    success_rate: float = 0.0
    # Figure 3
    library_size: float = 0.0
    # Appendix C
    trajectory_tokens: int = 0
    patch_tokens: int = 0
    compression_ratio: float = 0.0
    # Appendix E
    selr: float = 0.0
    # cost
    cost_usd: float = 0.0


@dataclass
class ExportSummary:
    """汇总导出元数据，供 CLI 展示用。"""

    output_dir: Path
    csv_files: list[str] = field(default_factory=list)
    figure_files: list[str] = field(default_factory=list)
    settings_found: list[str] = field(default_factory=list)
    total_rounds: int = 0


# ---------------------------------------------------------------------------
# 主导出器
# ---------------------------------------------------------------------------

class ResultsExporter:
    """
    论文结果导出器。

    读取 results/ 目录下所有 setting 的 round JSON，
    产出 CSV 表格 + matplotlib 图表。

    Usage::

        exporter = ResultsExporter(results_dir=Path("results"), output_dir=Path("results/tables"))
        summary = exporter.export_all()
        print(summary.csv_files)
    """

    def __init__(
        self,
        results_dir: Path | str,
        output_dir: Path | str | None = None,
    ) -> None:
        self.results_dir = Path(results_dir)
        self.output_dir = Path(output_dir) if output_dir else self.results_dir / "tables"
        self.figures_dir = self.output_dir.parent / "figures"

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def export_all(self) -> ExportSummary:
        """
        扫描 results_dir，生成所有 CSV 表格和图表。

        Returns:
            ExportSummary
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.figures_dir.mkdir(parents=True, exist_ok=True)

        records = self._load_all_records()
        if not records:
            logger.warning("未找到任何 round JSON 文件，输入目录：%s", self.results_dir)
            return ExportSummary(output_dir=self.output_dir)

        summary = ExportSummary(output_dir=self.output_dir)
        summary.settings_found = sorted({r.setting_name for r in records})
        summary.total_rounds = len(records)

        # ── CSV 导出 ─────────────────────────────────────────────────────
        csv_files = [
            self._write_success_rate_csv(records),
            self._write_communication_csv(records),
            self._write_privacy_csv(records),
            self._write_skill_growth_csv(records),
        ]
        summary.csv_files = [str(f) for f in csv_files if f]

        # ── 图表导出 ─────────────────────────────────────────────────────
        fig_files = self._write_figures(records)
        summary.figure_files = [str(f) for f in fig_files if f]

        logger.info(
            "导出完成: %d settings, %d rounds → %d CSVs, %d figures",
            len(summary.settings_found), summary.total_rounds,
            len(summary.csv_files), len(summary.figure_files),
        )
        return summary

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _load_all_records(self) -> list[RoundRecord]:
        """
        扫描 results_dir 下所有子目录，读取 round_*.json 文件。

        支持两种目录结构：
          1. results/<setting_name>/round_<N>_summary.json  (run_experiment.py 生成)
          2. logs/<setting_name>/round_summary.json          (备用格式)
        """
        records: list[RoundRecord] = []

        # 模式 1：results/<setting>/round_<N>_summary.json
        for json_file in sorted(self.results_dir.rglob("round_*_summary.json")):
            setting_name = json_file.parent.name
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                records += self._parse_round_json(data, setting_name)
            except Exception as exc:
                logger.warning("无法解析 %s: %s", json_file, exc)

        # 模式 2：直接 experiment_summary.json（含 per_round）
        for json_file in sorted(self.results_dir.rglob("experiment_summary.json")):
            setting_name = json_file.parent.name
            # 避免重复（若已从 round 文件加载过）
            already_loaded = any(r.setting_name == setting_name for r in records)
            if already_loaded:
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                records += self._parse_experiment_summary(data, setting_name)
            except Exception as exc:
                logger.warning("无法解析 %s: %s", json_file, exc)

        return records

    def _parse_round_json(
        self, data: dict[str, Any], setting_name: str
    ) -> list[RoundRecord]:
        """
        从 round_N_summary.json 解析 RoundRecord 列表。

        期望格式（由 run_experiment.py._save_results 写入）：
        {
          "round_idx": 0,
          "setting_name": "SE_Self_Evolution",
          "metrics": {"success_rate": 0.33, "compression_ratio": 0.85, ...},
          "per_worker": {"u0": {"success_rate": 0.33, ...}},
          "snapshots": [{"worker_id": "u0", "task_id": ..., "reward": 0.5, ...}]  (可选)
        }
        """
        round_idx = data.get("round_idx", 0)
        setting = data.get("setting_name", setting_name)
        metrics = data.get("metrics", {})
        per_worker = data.get("per_worker", {})
        snapshots = data.get("snapshots", [])

        records: list[RoundRecord] = []

        # 逐 worker 行
        if per_worker:
            for wid, wmetrics in per_worker.items():
                # 尝试从 snapshots 列表找到对应的 token 信息
                worker_snaps = [s for s in snapshots if s.get("worker_id") == wid]
                traj_tok = sum(s.get("trajectory_tokens", 0) for s in worker_snaps)
                patch_tok = sum(s.get("patch_tokens", 0) for s in worker_snaps)
                cost_usd = sum(s.get("cost_usd", 0.0) for s in worker_snaps)
                lib_size = (
                    sum(s.get("library_size_after", 0) for s in worker_snaps) / len(worker_snaps)
                    if worker_snaps else wmetrics.get("mean_library_size", 0.0)
                )

                # fallback: 从 metrics 取聚合值（若没有 per-snap 数据）
                cr = FederatedMetricsHelper.compression_ratio(patch_tok, traj_tok)
                if traj_tok == 0:
                    cr = wmetrics.get("compression_ratio", 0.0)

                records.append(RoundRecord(
                    setting_name=setting,
                    round_idx=round_idx,
                    family=data.get("family", "unknown"),
                    worker_id=wid,
                    success_rate=wmetrics.get("success_rate", 0.0),
                    library_size=lib_size,
                    trajectory_tokens=traj_tok,
                    patch_tokens=patch_tok,
                    compression_ratio=cr,
                    selr=wmetrics.get("selr", 0.0),
                    cost_usd=cost_usd,
                ))
        else:
            # 仅全局聚合指标，当作 worker="all"
            traj_tok = metrics.get("mean_trajectory_tokens", 0)
            patch_tok = metrics.get("mean_patch_tokens", 0)
            records.append(RoundRecord(
                setting_name=setting,
                round_idx=round_idx,
                worker_id="all",
                success_rate=metrics.get("success_rate", 0.0),
                library_size=metrics.get("mean_library_size", 0.0),
                trajectory_tokens=int(traj_tok),
                patch_tokens=int(patch_tok),
                compression_ratio=metrics.get("compression_ratio", 0.0),
                selr=metrics.get("selr", 0.0),
                cost_usd=metrics.get("total_cost_usd", 0.0),
            ))

        return records

    def _parse_experiment_summary(
        self, data: dict[str, Any], setting_name: str
    ) -> list[RoundRecord]:
        """
        从 experiment_summary.json 解析（ExperimentResult.to_dict() 格式）。
        """
        setting = data.get("setting_name", setting_name)
        sr_list = data.get("per_round_success_rates", [])
        lib_list = data.get("per_round_library_sizes", [])
        records: list[RoundRecord] = []
        for i, sr in enumerate(sr_list):
            lib = lib_list[i] if i < len(lib_list) else 0.0
            records.append(RoundRecord(
                setting_name=setting,
                round_idx=i,
                worker_id="all",
                success_rate=sr,
                library_size=lib,
            ))
        return records

    # ------------------------------------------------------------------
    # CSV 写入
    # ------------------------------------------------------------------

    def _write_success_rate_csv(self, records: list[RoundRecord]) -> Path | None:
        """
        success_rate.csv

        字段: setting, round, family, worker, success_rate

        对应论文 Table 1（最终轮成功率）和 Figure 2（每轮成功率曲线）。
        """
        path = self.output_dir / "success_rate.csv"
        fieldnames = ["setting", "round", "family", "worker", "success_rate"]
        rows = [
            {
                "setting": r.setting_name,
                "round": r.round_idx,
                "family": r.family,
                "worker": r.worker_id,
                "success_rate": f"{r.success_rate:.4f}",
            }
            for r in records
        ]
        _write_csv(path, fieldnames, rows)
        logger.info("success_rate.csv → %s (%d rows)", path, len(rows))
        return path

    def _write_communication_csv(self, records: list[RoundRecord]) -> Path | None:
        """
        communication.csv

        字段: setting, round, worker, trajectory_tokens, patch_tokens, compression_ratio

        对应论文 Appendix C (Table 6)。
        """
        path = self.output_dir / "communication.csv"
        fieldnames = [
            "setting", "round", "worker",
            "trajectory_tokens", "patch_tokens", "compression_ratio",
        ]
        rows = [
            {
                "setting": r.setting_name,
                "round": r.round_idx,
                "worker": r.worker_id,
                "trajectory_tokens": r.trajectory_tokens,
                "patch_tokens": r.patch_tokens,
                "compression_ratio": f"{r.compression_ratio:.4f}",
            }
            for r in records
        ]
        _write_csv(path, fieldnames, rows)
        logger.info("communication.csv → %s (%d rows)", path, len(rows))
        return path

    def _write_privacy_csv(self, records: list[RoundRecord]) -> Path | None:
        """
        privacy.csv

        字段: setting, round, worker, SEL_R

        对应论文 Appendix E (Table 8)：
          SELR = 敏感实体泄露率（patch 中含有轨迹中出现的实体名的比例）

        [已修正 - 最终论文一致性收口 Priority 3] 本方法读取的 `r.selr` 字段
        来自 evaluation/selr.py::compute_selr_from_texts()（论文 Eq.(5) 的
        真实实现：实体级抽取 + 逐一比对是否原样出现在 patch 文本中），
        由 experiments/federated.py / baseline.py 在每轮结束时计算并写入
        RoundRecord.selr——**不是**用 compression_ratio 代理（那个近似值
        单独存放在 evaluation/metrics.py::FederatedMetrics.privacy_gain()，
        仅用于内部参考，从未写入过这份 privacy.csv）。此前本 docstring
        遗留的旧说明（"用 compression_ratio 代理"）已过期，容易让人误以为
        当前导出的 SEL_R 列不是真实 SELR，特此更正，避免 naming/文档产生
        混淆。
        """
        path = self.output_dir / "privacy.csv"
        fieldnames = ["setting", "round", "worker", "SEL_R"]
        rows = [
            {
                "setting": r.setting_name,
                "round": r.round_idx,
                "worker": r.worker_id,
                "SEL_R": f"{r.selr:.4f}",
            }
            for r in records
        ]
        _write_csv(path, fieldnames, rows)
        logger.info("privacy.csv → %s (%d rows)", path, len(rows))
        return path

    def _write_skill_growth_csv(self, records: list[RoundRecord]) -> Path | None:
        """
        skill_growth.csv

        字段: setting, round, worker, library_size

        对应论文 Figure 3。
        """
        path = self.output_dir / "skill_growth.csv"
        fieldnames = ["setting", "round", "worker", "library_size"]
        rows = [
            {
                "setting": r.setting_name,
                "round": r.round_idx,
                "worker": r.worker_id,
                "library_size": f"{r.library_size:.2f}",
            }
            for r in records
        ]
        _write_csv(path, fieldnames, rows)
        logger.info("skill_growth.csv → %s (%d rows)", path, len(rows))
        return path

    # ------------------------------------------------------------------
    # 图表写入
    # ------------------------------------------------------------------

    def _write_figures(self, records: list[RoundRecord]) -> list[Path | None]:
        """生成三张论文风格图表。"""
        out: list[Path | None] = []
        out.append(self._figure_success_curve(records))
        out.append(self._figure_skill_growth(records))
        out.append(self._figure_compression(records))
        return out

    def _figure_success_curve(self, records: list[RoundRecord]) -> Path | None:
        """
        figure_success_curve.png — Figure 2 复现

        每条曲线代表一个 setting（横轴=round，纵轴=SR）。
        用 setting+worker="all" 的行绘制（或各 worker 平均）。
        """
        plt = _import_matplotlib()
        if plt is None:
            return None

        # 按 setting 聚合，worker="all" 优先，否则跨 worker 平均
        per_setting = _aggregate_per_setting(records, "success_rate")
        if not per_setting:
            return None

        fig, ax = plt.subplots(figsize=(7, 5))
        colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
        markers = ["o", "s", "^", "D", "v", "x"]

        for i, (setting, curve) in enumerate(sorted(per_setting.items())):
            rounds = list(range(len(curve)))
            ax.plot(
                rounds, curve,
                label=_shorten_setting(setting),
                color=colors[i % len(colors)],
                marker=markers[i % len(markers)],
                markersize=5, linewidth=1.8,
            )

        ax.set_xlabel("Round", fontsize=11)
        ax.set_ylabel("Success Rate", fontsize=11)
        ax.set_title("Success Rate per Round (Figure 2 reproduction)", fontsize=12)
        ax.set_ylim(0.0, 1.05)
        ax.legend(loc="lower right", fontsize=8, framealpha=0.9)
        ax.grid(alpha=0.3)
        fig.tight_layout()

        path = self.figures_dir / "figure_success_curve.png"
        fig.savefig(str(path), dpi=150)
        plt.close(fig)
        logger.info("figure_success_curve.png → %s", path)
        return path

    def _figure_skill_growth(self, records: list[RoundRecord]) -> Path | None:
        """
        figure_skill_growth.png — Figure 3 复现

        每条曲线代表一个 setting（横轴=round，纵轴=平均技能数）。
        """
        plt = _import_matplotlib()
        if plt is None:
            return None

        per_setting = _aggregate_per_setting(records, "library_size")
        if not per_setting:
            return None

        fig, ax = plt.subplots(figsize=(7, 5))
        colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
        markers = ["o", "s", "^", "D", "v", "x"]

        for i, (setting, curve) in enumerate(sorted(per_setting.items())):
            rounds = list(range(len(curve)))
            ax.plot(
                rounds, curve,
                label=_shorten_setting(setting),
                color=colors[i % len(colors)],
                marker=markers[i % len(markers)],
                markersize=5, linewidth=1.8,
            )

        # 论文 hard cap = 4 skills/family
        ax.axhline(4.0, color="gray", linestyle="--", alpha=0.5, label="Hard cap (4)")
        ax.set_xlabel("Round", fontsize=11)
        ax.set_ylabel("Avg skills per worker", fontsize=11)
        ax.set_title("Skill Library Growth (Figure 3 reproduction)", fontsize=12)
        ax.set_ylim(0.0, 5.0)
        ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
        ax.grid(alpha=0.3)
        fig.tight_layout()

        path = self.figures_dir / "figure_skill_growth.png"
        fig.savefig(str(path), dpi=150)
        plt.close(fig)
        logger.info("figure_skill_growth.png → %s", path)
        return path

    def _figure_compression(self, records: list[RoundRecord]) -> Path | None:
        """
        figure_compression.png — Appendix C 压缩比图

        对应论文 Table 6 / Figure 4 风格：每个 setting 每轮的通信压缩比。
        """
        plt = _import_matplotlib()
        if plt is None:
            return None

        per_setting = _aggregate_per_setting(records, "compression_ratio")
        if not per_setting or all(
            all(v == 0.0 for v in curve) for curve in per_setting.values()
        ):
            logger.info("compression_ratio 数据全为 0，跳过绘图（需真实实验数据）")
            return None

        fig, ax = plt.subplots(figsize=(7, 5))
        colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
        markers = ["o", "s", "^", "D", "v", "x"]

        for i, (setting, curve) in enumerate(sorted(per_setting.items())):
            rounds = list(range(len(curve)))
            ax.plot(
                rounds, curve,
                label=_shorten_setting(setting),
                color=colors[i % len(colors)],
                marker=markers[i % len(markers)],
                markersize=5, linewidth=1.8,
            )

        ax.set_xlabel("Round", fontsize=11)
        ax.set_ylabel("Compression Ratio (1 - |patch|/|traj|)", fontsize=11)
        ax.set_title("Communication Compression (Appendix C)", fontsize=12)
        ax.set_ylim(0.0, 1.05)
        ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
        ax.grid(alpha=0.3)
        fig.tight_layout()

        path = self.figures_dir / "figure_compression.png"
        fig.savefig(str(path), dpi=150)
        plt.close(fig)
        logger.info("figure_compression.png → %s", path)
        return path


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _import_matplotlib():
    """延迟导入 matplotlib，未安装时返回 None。"""
    try:
        import matplotlib
        import matplotlib.pyplot as plt
        matplotlib.use("Agg")
        return plt
    except ImportError:
        logger.warning("matplotlib 未安装，跳过图表生成")
        return None


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """写 UTF-8 with BOM CSV（Excel 可直接打开不乱码）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _aggregate_per_setting(
    records: list[RoundRecord],
    metric: str,
) -> dict[str, list[float]]:
    """
    按 (setting, round) 聚合指标，优先使用 worker="all" 的行，
    否则跨同 round 的所有 worker 求平均。

    Returns:
        {setting_name: [metric_round0, metric_round1, ...]}
    """
    from collections import defaultdict

    # (setting, round) → list[float]
    by_setting_round: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        val = getattr(r, metric, 0.0)
        by_setting_round[r.setting_name][r.round_idx].append(
            (val, r.worker_id == "all")  # (value, is_aggregate)
        )

    result: dict[str, list[float]] = {}
    for setting, round_map in by_setting_round.items():
        if not round_map:
            continue
        max_round = max(round_map.keys())
        curve: list[float] = []
        for r in range(max_round + 1):
            entries = round_map.get(r, [])
            if not entries:
                curve.append(0.0)
                continue
            # 优先 worker="all"
            aggregates = [v for v, is_agg in entries if is_agg]
            if aggregates:
                curve.append(sum(aggregates) / len(aggregates))
            else:
                vals = [v for v, _ in entries]
                curve.append(sum(vals) / len(vals))
        result[setting] = curve

    return result


def _shorten_setting(name: str) -> str:
    """把长 setting 名缩短为图例标签。"""
    mapping = {
        "SE_Self_Evolution": "SE (Setting1)",
        "Homo_Federated": "Homo Fed (Setting2)",
        "Hetero_Backbone": "Hetero Backbone (Setting3)",
        "Full_Hetero": "Full Hetero (Setting4)",
    }
    return mapping.get(name, name[:20])


class FederatedMetricsHelper:
    """简单指标计算（不依赖 evaluation.metrics，避免循环导入）。"""

    @staticmethod
    def compression_ratio(patch_tokens: int, traj_tokens: int) -> float:
        if traj_tokens <= 0:
            return 0.0
        return max(0.0, 1.0 - patch_tokens / traj_tokens)
