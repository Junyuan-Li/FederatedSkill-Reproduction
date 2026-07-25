"""
build_case_analysis.py — Appendix A Per-Cell Case Analysis 生成脚本
（Result Reproduction Readiness Audit TASK5，独立可选脚本，默认不接入 run.py）

用途：
    论文 Appendix A 用具体的跨轮次案例（如"Round 2 Qwen 的 patch 被 Round 6
    Kimi 采纳后成功率提升"）论证联邦学习为什么有效。本脚本把
    evaluation/audit_trace.py 已经落盘的 evolution_trace.jsonl（每条决策的
    source_patch_client/action/skill_path/reward/reason）与
    round_*_summary.json 里已经算好的 per_worker[...][success_rate] 做一次
    只读关联，生成 case_analysis.csv：

        family, round, source_client, target_client, action, skill_path,
        reward_before, reward_after, reason

    reward_before 直接取自 EvolutionTraceRecord.reward（该 worker 提交这条
    patch 时的即时 reward）；reward_after 取自"下一轮"（round+1）
    round_*_summary.json 里 target_client 的 success_rate（该 worker 采纳
    这条更新后的表现）——若下一轮不存在（已是最后一轮）或该 worker 未出现
    在下一轮的 per_worker 里，回退为空字符串，不猜测/不外推未来数据。

    本脚本【只读】evolution_trace.jsonl / round_*_summary.json，不参与、不
    修改 Stage1 规划、Stage2 合并、能力矩阵、memory 等任何决策逻辑，也不
    重新计算 reward/success_rate，纯粹是两份已有 JSON 数据的关联展开。

用法：
    python scripts/build_case_analysis.py --setting-dir results/setting3_hetero_backbone
    python scripts/build_case_analysis.py --setting-dir results/setting1_se --output out/case_analysis.csv

    支持两种目录布局：
      - family-loop 模式（Setting2-4 真实实验）：
        <setting-dir>/families/<family_id>/{evolution_trace.jsonl, round_*_summary.json}
      - 扁平模式（无 families/ 子目录）：直接读 <setting-dir> 本身。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluation.audit_trace import load_trace_jsonl  # noqa: E402

#: case_analysis.csv 列顺序（用户 TASK5 显式指定的字段名）
CASE_ANALYSIS_FIELDS = [
    "family", "round", "source_client", "target_client", "action",
    "skill_path", "reward_before", "reward_after", "reason",
]


def _load_round_records_by_idx(family_dir: Path) -> dict[int, dict]:
    """按 round_idx 建立 <family_dir>/round_*_summary.json 的查找表。"""
    records: dict[int, dict] = {}
    for p in sorted(family_dir.glob("round_*_summary.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        records[rec.get("round_idx", 0)] = rec
    return records


def _reward_after(
    round_records: dict[int, dict], round_idx: int | None, target_client: str | None,
) -> float | str:
    """
    target_client 在 round_idx + 1 的 success_rate（用于衡量"采纳这次更新
    之后"的表现）。下一轮不存在，或该 worker 未出现在下一轮 per_worker 里
    时返回 ""（不外推/不猜测未来数据）。
    """
    if round_idx is None or target_client is None:
        return ""
    next_rec = round_records.get(round_idx + 1)
    if next_rec is None:
        return ""
    per_worker = next_rec.get("per_worker", {}) or {}
    wmetrics = per_worker.get(target_client)
    if wmetrics is None:
        return ""
    return wmetrics.get("success_rate", "")


def build_case_analysis_rows(family_id: str, family_dir: Path) -> list[dict]:
    """为单个 family（或扁平模式下的整个 setting_dir）生成 case_analysis 行。"""
    trace_records = load_trace_jsonl(family_dir / "evolution_trace.jsonl")
    round_records = _load_round_records_by_idx(family_dir)

    rows: list[dict] = []
    for rec in trace_records:
        round_idx = rec.get("round_idx")
        target_client = rec.get("client_id")
        rows.append({
            "family": family_id,
            "round": round_idx,
            "source_client": rec.get("source_patch_client"),
            "target_client": target_client,
            "action": rec.get("action"),
            "skill_path": rec.get("skill_path"),
            "reward_before": rec.get("reward"),
            "reward_after": _reward_after(round_records, round_idx, target_client),
            "reason": rec.get("decision_reason"),
        })
    return rows


def build_case_analysis_csv(setting_dir: str | Path, output_path: str | Path | None = None) -> Path:
    """生成 case_analysis.csv，自动探测 family-loop / 扁平两种目录布局。"""
    setting_dir = Path(setting_dir)
    output_path = Path(output_path) if output_path is not None else setting_dir / "case_analysis.csv"

    families_dir = setting_dir / "families"
    rows: list[dict] = []
    if families_dir.is_dir():
        for family_subdir in sorted(families_dir.iterdir()):
            if family_subdir.is_dir():
                rows.extend(build_case_analysis_rows(family_subdir.name, family_subdir))
    else:
        rows.extend(build_case_analysis_rows("", setting_dir))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CASE_ANALYSIS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="build_case_analysis",
        description=(
            "Appendix A Per-Cell Case Analysis：关联 evolution_trace.jsonl 与 "
            "round_*_summary.json 生成 case_analysis.csv（只读派生，不修改任何 "
            "演化/合并逻辑）。"
        ),
    )
    parser.add_argument(
        "--setting-dir", required=True, type=Path,
        help="setting 输出目录，如 results/setting3_hetero_backbone",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="输出 CSV 路径，默认 <setting-dir>/case_analysis.csv",
    )
    args = parser.parse_args()

    path = build_case_analysis_csv(args.setting_dir, args.output)
    print(f"已写入: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
