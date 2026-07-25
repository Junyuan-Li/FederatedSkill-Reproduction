"""
scripts/compare_with_paper.py — 官方 paper_logs/ 自动对标脚本

对应用户 checklist 第三优先级 item1："官方 paper_logs 自动对标脚本"：
本地复现结果 vs 官方 `FederatedSkill-main/FederatedSkill-main/paper_logs/`
原始运行日志（对应论文 Table 1 / Table 2），逐 family 计算成功率偏差，
偏差 < 5%（可调）视为基本对齐。

⚠️ 边界声明（与 scripts/compare_official_protocol.py 的"人工审阅结论静态
比对"不同，本脚本做的是**真实读取两侧数据文件后的定量比对**）：
  - 只读取官方 `paper_logs/` 下的 `result.json`（SE baseline 单模型场景）/
    `family_summary.json`（联邦多 worker 场景）里已有的 reward 统计字段，
    不重新计算/不修改这些文件。
  - 本地一侧复用 `experiments/aggregation.py::extract_family_rows()`
    （已有、经过测试的解析逻辑），不重新实现 experiment_summary.json 的
    解析规则，避免出现两套口径不一致的成功率计算。
  - 官方目录结构里同一个 setting（如 `3_fed_hetero_cc/`）下会有多个
    model 子目录（glm-5/kimi-k2.5/qwen3.6-plus），本脚本对同一 family
    在这些子目录下的所有可用记录取算术平均，不假设它们之间的关系
    （多个独立单模型基线 vs 同一次多 worker 联邦实验的不同索引视角）——
    如需精确对齐到单个具体 backbone，见 `--model` 参数。

用法::

    # 对比单个 --family 独立运行的结果（run_root 是 experiment_id 目录，
    # 即 experiment_summary.json 所在的那一层，不是 <family_id>/metrics/）
    python scripts/compare_with_paper.py results/20260723T105019Z_xxx_ab12cd34 --official-setting 1_se

    # 对比一次跑完全部 family 的传统 loop_over_families 实验目录
    python scripts/compare_with_paper.py results/setting1_se --official-setting 1_se

    # 只对齐到某一个具体 backbone（而不是跨 model 子目录取平均）
    python scripts/compare_with_paper.py results/setting1_se --official-setting 1_se --model qwen3.6-plus

    # 自定义达标阈值 + 导出 CSV
    python scripts/compare_with_paper.py results/setting1_se --official-setting 1_se --threshold 0.05 --csv-out results/csv/paper_alignment.csv

官方 setting 目录名（`paper_logs/` 下的子目录，非全部 4 个 Setting 都有
官方日志——`2_fed_homogeneous` 目前未在官方仓库中找到对应目录）：
    1_se                  — Setting1 自进化基线
    3_fed_hetero_cc        — Setting3 异构模型联邦（同构 harness）
    4_fed_hetero_mixed_cli — Setting4 全异构（模型 + CLI harness）
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from experiments.aggregation import AggregationError, extract_family_rows

#: 官方仓库与本仓库是同级的兄弟目录（D:\...\考核题目\FederatedSkill-main\FederatedSkill-main），
#: 与 scripts/compare_official_protocol.py docstring 里引用的路径一致。
DEFAULT_OFFICIAL_ROOT = _REPO_ROOT.parent / "FederatedSkill-main" / "FederatedSkill-main" / "paper_logs"
DEFAULT_THRESHOLD = 0.05


def _official_family_success_rate_from_result_json(data: dict, family_name: str) -> float | None:
    """
    解析 SE baseline 场景下单个 family 的 `result.json`（单 model 子目录）。

    优先读 `stats.evals.<harness>__<family>.metrics[0].mean`（官方已经算好的
    均值），只有该字段缺失时才退化为从 `reward_stats.reward`
    （{reward_value_str: [task_id, ...]}）重新累计 n_passed/n_total——
    reward>=1.0 才计入 n_passed，严格二值判定，与本仓库 SR 定义一致。
    """
    evals = data.get("stats", {}).get("evals", {})
    means: list[float] = []
    for eval_key, eval_data in evals.items():
        if not eval_key.endswith(f"__{family_name}"):
            continue
        metrics = eval_data.get("metrics") or []
        if metrics and "mean" in metrics[0]:
            means.append(float(metrics[0]["mean"]))
            continue
        reward_buckets = eval_data.get("reward_stats", {}).get("reward", {})
        n_total = sum(len(v) for v in reward_buckets.values())
        if n_total == 0:
            continue
        n_passed = sum(len(v) for k, v in reward_buckets.items() if float(k) >= 1.0)
        means.append(n_passed / n_total)
    return mean(means) if means else None


def _official_family_success_rate_from_family_summary(data: dict) -> float | None:
    """解析异构联邦场景下单个 family 的 `family_summary.json`（多 worker 联邦运行）。"""
    stats = data.get("reward_stats", {})
    n = stats.get("n", 0)
    if not n:
        return None
    return float(stats.get("n_passed", 0)) / float(n)


def official_family_success_rate(family_dir: Path) -> float | None:
    """
    从单个 `<official_setting>/<model>/<family_name>/` 目录读出该 family
    的官方成功率；两种文件格式二选一，都不存在时返回 None（该 family 在
    这个 model 子目录下没有官方记录，不参与偏差计算）。
    """
    result_json = family_dir / "result.json"
    if result_json.exists():
        data = json.loads(result_json.read_text(encoding="utf-8"))
        return _official_family_success_rate_from_result_json(data, family_dir.name)

    family_summary = family_dir / "family_summary.json"
    if family_summary.exists():
        data = json.loads(family_summary.read_text(encoding="utf-8"))
        return _official_family_success_rate_from_family_summary(data)

    return None


def load_official_success_rates(
    official_setting_dir: Path, model: str | None = None,
) -> dict[str, float]:
    """
    汇总某个官方 setting 目录（如 `paper_logs/1_se/`）下每个 family 的官方
    成功率。默认跨该 setting 下全部 model 子目录取算术平均；`model` 非
    None 时只读取该指定子目录。
    """
    if not official_setting_dir.exists():
        raise FileNotFoundError(
            f"官方 setting 目录不存在: {official_setting_dir}\n"
            f"（可用子目录见 {official_setting_dir.parent} 下的目录列表，"
            f"或用 --official-root 指向正确的 paper_logs/ 路径）"
        )

    if model is not None:
        model_dirs = [official_setting_dir / model]
        if not model_dirs[0].exists():
            raise FileNotFoundError(f"官方 model 子目录不存在: {model_dirs[0]}")
    else:
        model_dirs = sorted(p for p in official_setting_dir.iterdir() if p.is_dir())

    per_family: dict[str, list[float]] = {}
    for model_dir in model_dirs:
        for family_dir in sorted(p for p in model_dir.iterdir() if p.is_dir()):
            sr = official_family_success_rate(family_dir)
            if sr is not None:
                per_family.setdefault(family_dir.name, []).append(sr)
    return {family: mean(vals) for family, vals in per_family.items()}


def load_local_success_rates(local_dir: Path) -> dict[str, float]:
    """复用 `experiments/aggregation.py::extract_family_rows()` 读出本地 family 级成功率。"""
    rows = extract_family_rows(local_dir)
    result: dict[str, float] = {}
    for row in rows:
        result[row["family_id"]] = row["success_rate"]
    return result


def compare(
    local_dir: Path,
    official_setting_dir: Path,
    model: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict[str, Any]]:
    """逐 family 对比本地 vs 官方成功率，返回排序好的行列表（含 MISSING 行）。"""
    official = load_official_success_rates(official_setting_dir, model=model)
    local = load_local_success_rates(local_dir)

    rows: list[dict[str, Any]] = []
    for family_name in sorted(set(official) | set(local)):
        official_sr = official.get(family_name)
        local_sr = local.get(family_name)
        if official_sr is None or local_sr is None:
            rows.append({
                "family_name": family_name,
                "official_sr": official_sr,
                "local_sr": local_sr,
                "deviation": None,
                "status": "MISSING",
            })
            continue
        deviation = abs(local_sr - official_sr)
        rows.append({
            "family_name": family_name,
            "official_sr": round(official_sr, 4),
            "local_sr": round(local_sr, 4),
            "deviation": round(deviation, 4),
            "status": "PASS" if deviation < threshold else "FAIL",
        })
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["family_name", "official_sr", "local_sr", "deviation", "status"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def print_report(rows: list[dict[str, Any]], threshold: float) -> None:
    def _fmt(v: float | None) -> str:
        return f"{v:.4f}" if v is not None else "N/A"

    print(f"{'family_name':<42}{'official_sr':>12}{'local_sr':>10}{'deviation':>12}{'status':>10}")
    print("-" * 90)
    for row in rows:
        print(
            f"{row['family_name']:<42}"
            f"{_fmt(row['official_sr']):>12}"
            f"{_fmt(row['local_sr']):>10}"
            f"{_fmt(row['deviation']):>12}"
            f"{row['status']:>10}"
        )
    print("-" * 90)

    matched = [r for r in rows if r["status"] in ("PASS", "FAIL")]
    if matched:
        mean_dev = mean(r["deviation"] for r in matched)
        n_pass = sum(1 for r in matched if r["status"] == "PASS")
        print(
            f"匹配 family 数: {len(matched)}  平均偏差: {mean_dev:.4f}  "
            f"达标(< {threshold:.0%}): {n_pass}/{len(matched)}"
        )
    else:
        print("[警告] 本地与官方没有任何共同 family，无法计算偏差。")

    n_missing = len(rows) - len(matched)
    if n_missing:
        print(f"[警告] {n_missing} 个 family 只在本地或官方一侧存在，未参与偏差计算。")


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare_with_paper",
        description="对比本地复现结果与官方 paper_logs/ 原始运行日志，输出逐 family 成功率偏差。",
    )
    parser.add_argument(
        "local_dir",
        help="本地实验结果目录（experiment_summary.json 所在层级，"
             "--family 单次运行对应 experiment_id 根目录，而不是 <family_id>/metrics/ 子目录）",
    )
    parser.add_argument(
        "--official-setting",
        required=True,
        help="paper_logs/ 下的子目录名，如 1_se / 3_fed_hetero_cc / 4_fed_hetero_mixed_cli",
    )
    parser.add_argument(
        "--official-root",
        default=str(DEFAULT_OFFICIAL_ROOT),
        help=f"官方 paper_logs/ 根目录（默认 {DEFAULT_OFFICIAL_ROOT}）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="只对齐到指定 backbone 子目录（如 qwen3.6-plus），默认跨该 setting 下全部 model 子目录取平均",
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="达标阈值（默认 0.05，即 5%%）")
    parser.add_argument("--csv-out", default=None, help="可选：把对比结果写入这个 CSV 路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_cli().parse_args(argv)

    local_dir = Path(args.local_dir)
    if not local_dir.is_absolute():
        local_dir = _REPO_ROOT / local_dir
    official_setting_dir = Path(args.official_root) / args.official_setting

    try:
        rows = compare(local_dir, official_setting_dir, model=args.model, threshold=args.threshold)
    except (AggregationError, FileNotFoundError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    print_report(rows, args.threshold)

    if args.csv_out:
        path = write_csv(rows, Path(args.csv_out))
        print(f"\nCSV 已写入: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
