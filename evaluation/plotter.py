"""
plotter.py — 论文 Figure 2/3/4 绘图工具

生成复现论文所需的三类图：
  Figure 2: Success Rate curves (per-worker per-setting per-round)
  Figure 3: Library Size evolution (per-worker per-round)
  Figure 4: Cost per task (cumulative cost vs solved tasks)

依赖: matplotlib (可选；若未安装则 save_* 方法静默跳过)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _import_matplotlib():
    try:
        import matplotlib
        import matplotlib.pyplot as plt
        matplotlib.use("Agg")  # 无 GUI 后端，适合 CI/服务器环境
        return plt
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Figure 2: Success Rate curves
# ---------------------------------------------------------------------------

def plot_success_rate_curves(
    per_setting_curves: dict[str, list[float]],
    output_path: Path | str,
    *,
    title: str = "Figure 2: Success Rate per Round",
    xlabel: str = "Round",
    ylabel: str = "Success Rate",
    paper_baseline_se: float | None = None,
) -> bool:
    """
    绘制论文 Figure 2 风格的成功率曲线图。

    Args:
        per_setting_curves:  {setting_label: [sr_round0, sr_round1, ...]}
        output_path:         保存路径（.png 或 .pdf）
        paper_baseline_se:   论文中 SE baseline 的最终 SR（画参考虚线用）

    Returns:
        True 成功保存；False 跳过（matplotlib 未安装）
    """
    plt = _import_matplotlib()
    if plt is None:
        logger.warning("matplotlib 未安装，跳过 Figure 2 绘图")
        return False

    fig, ax = plt.subplots(figsize=(7, 5))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    linestyles = ["-", "--", "-.", ":", "-"]

    for i, (label, curve) in enumerate(per_setting_curves.items()):
        rounds = list(range(len(curve)))
        ax.plot(
            rounds, curve,
            label=label,
            color=colors[i % len(colors)],
            linestyle=linestyles[i % len(linestyles)],
            marker="o", markersize=4,
        )

    if paper_baseline_se is not None:
        ax.axhline(
            paper_baseline_se, color="gray", linestyle=":",
            label=f"Paper SE Baseline ({paper_baseline_se:.0%})",
        )

    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(loc="best", fontsize=8)
    ax.set_ylim(0.0, 1.05)
    ax.grid(alpha=0.3)
    fig.tight_layout()

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)
    logger.info("Figure 2 保存至 %s", output_path)
    return True


# ---------------------------------------------------------------------------
# Figure 3: Library Size evolution
# ---------------------------------------------------------------------------

def plot_library_size_curves(
    per_worker_sizes: dict[str, list[float]],
    output_path: Path | str,
    *,
    title: str = "Figure 3: Library Size per Round",
    paper_cap: int = 4,
) -> bool:
    """
    绘制论文 Figure 3 风格的技能库大小曲线。

    Args:
        per_worker_sizes:  {worker_label: [lib_size_round0, ...]}
        paper_cap:         论文 hard cap = 4 skills/family（画参考线）
    """
    plt = _import_matplotlib()
    if plt is None:
        logger.warning("matplotlib 未安装，跳过 Figure 3 绘图")
        return False

    fig, ax = plt.subplots(figsize=(7, 4))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]

    for i, (label, sizes) in enumerate(per_worker_sizes.items()):
        rounds = list(range(len(sizes)))
        ax.plot(
            rounds, sizes,
            label=label,
            color=colors[i % len(colors)],
            marker="s", markersize=4,
        )

    ax.axhline(paper_cap, color="red", linestyle="--", alpha=0.6,
               label=f"Hard cap ({paper_cap} skills/family)")

    ax.set_xlabel("Round")
    ax.set_ylabel("Avg skills per family")
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_ylim(0, paper_cap + 1)
    ax.grid(alpha=0.3)
    fig.tight_layout()

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)
    logger.info("Figure 3 保存至 %s", output_path)
    return True


# ---------------------------------------------------------------------------
# Figure 4: Cost per task
# ---------------------------------------------------------------------------

def plot_cost_per_task(
    per_setting_costs: dict[str, list[float]],
    output_path: Path | str,
    *,
    title: str = "Figure 4: Cumulative Cost vs Solved Tasks",
) -> bool:
    """
    绘制论文 Figure 4 风格的每任务费用曲线。

    Args:
        per_setting_costs:  {setting_label: [cost_round0, cost_round1, ...]}
                            每个元素为该 round 所有 trial 的 total cost_usd
    """
    plt = _import_matplotlib()
    if plt is None:
        logger.warning("matplotlib 未安装，跳过 Figure 4 绘图")
        return False

    fig, ax = plt.subplots(figsize=(7, 4))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]

    for i, (label, costs) in enumerate(per_setting_costs.items()):
        cumulative = []
        acc = 0.0
        for c in costs:
            acc += c
            cumulative.append(acc)
        rounds = list(range(1, len(cumulative) + 1))
        ax.plot(
            rounds, cumulative,
            label=label,
            color=colors[i % len(colors)],
            marker="^", markersize=4,
        )

    ax.set_xlabel("Round")
    ax.set_ylabel("Cumulative Cost (USD)")
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)
    logger.info("Figure 4 保存至 %s", output_path)
    return True


# ---------------------------------------------------------------------------
# 辅助：从 experiment results JSON 提取绘图数据
# ---------------------------------------------------------------------------

def load_experiment_results(results_dir: Path | str) -> dict[str, Any]:
    """
    读取 results/ 目录下所有 setting 的 metrics.json，构造绘图数据结构。

    Returns:
        {
          "success_curves": {setting_name: [sr_per_round]},
          "library_curves": {setting_name: [lib_size_per_round]},
          "cost_curves":    {setting_name: [cost_per_round]},
        }
    """
    results_dir = Path(results_dir)
    output: dict[str, Any] = {
        "success_curves": {},
        "library_curves": {},
        "cost_curves": {},
    }

    for metrics_file in sorted(results_dir.glob("*/metrics.json")):
        setting_name = metrics_file.parent.name
        try:
            data = json.loads(metrics_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("无法读取 %s: %s", metrics_file, exc)
            continue

        # 期望结构：{"rounds": [{success_rate, mean_library_size, total_cost_usd, ...}]}
        rounds = data.get("rounds", [])
        output["success_curves"][setting_name] = [r.get("success_rate", 0.0) for r in rounds]
        output["library_curves"][setting_name] = [r.get("mean_library_size", 0.0) for r in rounds]
        output["cost_curves"][setting_name] = [r.get("total_cost_usd", 0.0) for r in rounds]

    return output


def generate_all_figures(results_dir: Path | str, output_dir: Path | str) -> None:
    """
    从 results/ 目录一次性生成 Figure 2、3、4。

    Args:
        results_dir:  实验结果根目录（含各 setting 子目录）
        output_dir:   图片输出目录
    """
    data = load_experiment_results(results_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if data["success_curves"]:
        plot_success_rate_curves(data["success_curves"], out / "figure2_success_rate.png")
    if data["library_curves"]:
        plot_library_size_curves(data["library_curves"], out / "figure3_library_size.png")
    if data["cost_curves"]:
        plot_cost_per_task(data["cost_curves"], out / "figure4_cost_per_task.png")
