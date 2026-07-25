"""
aggregation.py — 跨 family 结果聚合（对应论文 Section 5 evaluation）。

背景：`experiments/run_experiment.py --family <id>` 每次只跑一个 family，
产出一个独立的 `results/<experiment_id>/` 目录（见其
`_run_family_loop()`/`_save_family_loop_summary()` 写出的
`experiment_summary.json`）。本模块只做**只读聚合**，不重新计算任何
round-level 指标、不触碰任何 client/server/evolution 算法逻辑：

  - mean success rate   （每个 family 的任务级成功率，取自
                          `experiment_summary.json["task_metrics_by_family"]`，
                          由 `experiments/task_checkpoint.py::
                          collect_task_checkpoint_stats()` 计算）
  - mean skill growth   （每个 family 技能库大小：末轮 - 首轮，取自
                          `experiment_summary.json["families"][fid]
                          ["library_sizes"]`）
  - mean library size   （每个 family 技能库大小：末轮）

用法::

    # 聚合若干个 --family 单次运行产出的实验目录
    python experiments/aggregation.py results/20260723T101500Z_xxx_ab12cd34

    # 一次传入多个
    python experiments/aggregation.py results/exp1 results/exp2 results/exp3

    # 用 glob 模式批量匹配
    python experiments/aggregation.py --glob "results/2026*"

    # 也支持一个已经跑完多个 family 的传统 loop_over_families 实验目录
    # （如 results/setting1_se/，其 experiment_summary.json 的 "families"
    # 字典里含多个 family_id，本模块会把每一个都当成一行聚合）
    python experiments/aggregation.py results/setting1_se
"""

from __future__ import annotations

import argparse
import glob as glob_module
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class AggregationError(RuntimeError):
    """experiment_summary.json 缺失/不是 family_loop 模式产物时抛出。"""


def _load_experiment_summary(experiment_dir: Path) -> dict[str, Any]:
    summary_path = experiment_dir / "experiment_summary.json"
    if not summary_path.exists():
        raise AggregationError(
            f"{experiment_dir} 下找不到 experiment_summary.json（实验可能未完成，"
            f"或路径不是 run_experiment.py 的输出目录）。"
        )
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    if data.get("mode") != "family_loop":
        raise AggregationError(
            f"{summary_path} 的 mode={data.get('mode')!r}，期望 'family_loop'"
            "（由 experiments/run_experiment.py::_run_family_loop 产出）——"
            "本聚合脚本只支持按 family 循环的实验结果。"
        )
    return data


def extract_family_rows(experiment_dir: Path) -> list[dict[str, Any]]:
    """从单个实验目录的 experiment_summary.json 里提取每个 family 一行指标。"""
    summary = _load_experiment_summary(experiment_dir)
    families = summary.get("families", {})
    task_stats = summary.get("task_metrics_by_family", {})

    rows: list[dict[str, Any]] = []
    for family_id, info in families.items():
        library_sizes = info.get("library_sizes") or []
        initial_library_size = float(library_sizes[0]) if library_sizes else 0.0
        final_library_size = float(library_sizes[-1]) if library_sizes else 0.0
        stats = task_stats.get(family_id, {})
        # 优先用任务级成功率（completed_tasks/total_tasks，覆盖整个 family
        # 全程，而不仅是最后一轮），取不到时回退到最后一轮的 success_rate。
        success_rate = float(stats.get("success_rate", info.get("final_success_rate", 0.0)))
        rows.append({
            "experiment_dir": str(experiment_dir),
            "family_id": family_id,
            "success_rate": success_rate,
            "skill_growth": final_library_size - initial_library_size,
            "final_library_size": final_library_size,
            "n_rounds": info.get("rounds", 0),
            "failed": False,
        })

    # 执行失败的 family 也计入分母（成功率/增长/库大小按 0 记），
    # 使 mean 值能真实反映"跑失败也要算进平均"，而不是悄悄跳过。
    failed_families = summary.get("failed_families", {})
    for family_id, reason in failed_families.items():
        rows.append({
            "experiment_dir": str(experiment_dir),
            "family_id": family_id,
            "success_rate": 0.0,
            "skill_growth": 0.0,
            "final_library_size": 0.0,
            "n_rounds": 0,
            "failed": True,
            "failure_reason": reason,
        })
    return rows


def aggregate(experiment_dirs: list[Path]) -> dict[str, Any]:
    """跨一个或多个实验目录聚合出论文 Section 5 要求的三项均值指标。"""
    all_rows: list[dict[str, Any]] = []
    for experiment_dir in experiment_dirs:
        all_rows.extend(extract_family_rows(experiment_dir))

    if not all_rows:
        raise AggregationError("没有可聚合的 family 结果（experiment_dirs 为空，或全部没有 families）。")

    n_families = len(all_rows)
    n_failed = sum(1 for r in all_rows if r.get("failed"))

    return {
        "n_families": n_families,
        "n_failed_families": n_failed,
        "mean_success_rate": mean(r["success_rate"] for r in all_rows),
        "mean_skill_growth": mean(r["skill_growth"] for r in all_rows),
        "mean_library_size": mean(r["final_library_size"] for r in all_rows),
        "per_family": all_rows,
    }


def _resolve_experiment_dirs(paths: list[str], glob_pattern: str | None) -> list[Path]:
    dirs: list[Path] = []
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = _REPO_ROOT / path
        if not path.exists():
            raise FileNotFoundError(f"实验目录不存在: {path}")
        dirs.append(path)
    if glob_pattern:
        matches = sorted(glob_module.glob(glob_pattern))
        for match in matches:
            match_path = Path(match)
            if not match_path.is_absolute():
                match_path = _REPO_ROOT / match_path
            if match_path.is_dir():
                dirs.append(match_path)
    if not dirs:
        raise ValueError("未指定任何实验目录（位置参数或 --glob 均为空）。")
    return dirs


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aggregation",
        description=(
            "跨 family 结果聚合（对应论文 Section 5 evaluation）："
            "mean success rate / mean skill growth / mean library size。"
        ),
    )
    parser.add_argument(
        "experiment_dirs", nargs="*", default=[],
        help="一个或多个实验结果目录（由 run_experiment.py 生成，含 experiment_summary.json）",
    )
    parser.add_argument(
        "--glob", default=None,
        help="额外用 glob 模式批量匹配实验目录，例如 'results/2026*'",
    )
    parser.add_argument(
        "--json-out", type=Path, default=None,
        help="将聚合结果写入指定 JSON 文件（默认只打印到 stdout）",
    )
    return parser


def main() -> None:
    parser = _build_cli()
    args = parser.parse_args()
    experiment_dirs = _resolve_experiment_dirs(args.experiment_dirs, args.glob)
    result = aggregate(experiment_dirs)

    print(f"聚合 family 数: {result['n_families']}（失败 {result['n_failed_families']}）")
    print(f"mean success rate:  {result['mean_success_rate']:.4f}")
    print(f"mean skill growth:  {result['mean_skill_growth']:.4f}")
    print(f"mean library size:  {result['mean_library_size']:.4f}")
    for row in result["per_family"]:
        tag = "[FAILED] " if row.get("failed") else ""
        print(
            f"  {tag}{row['family_id']} (from {row['experiment_dir']}): "
            f"SR={row['success_rate']:.3f} growth={row['skill_growth']:.1f} "
            f"lib_size={row['final_library_size']:.1f}"
        )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写入: {args.json_out}")


if __name__ == "__main__":
    main()
