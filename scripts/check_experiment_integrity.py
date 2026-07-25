"""
scripts/check_experiment_integrity.py — 真实实验运行完整性检查（只读脚本，
不修改任何实验代码 / 不修改任何实验产物）。

背景：
    真实 API 实验（Setting1-4）跑完后，需要一个独立的、只读的核对脚本，
    证明"这次跑的是真实 LLM 调用路径，而不是静默走了 mock/fallback 简化
    分支"——本脚本只读取已落盘的审计产物（experiment_execution_trace.jsonl /
    cost_ledger.jsonl），不触碰、不重新执行任何 Algorithm 1 流程、Stage1
    Planner、Stage2 Merge、Patch Distillation 逻辑本身。

产物来源（与 evaluation/integrity_logs.py::ExecutionTraceRecorder /
evaluation/cost_accounting.py::CostAccountant 的落盘位置一致）：
    <output_dir>/families/<family_id>/experiment_execution_trace.jsonl
    <output_dir>/families/<family_id>/cost_ledger.jsonl
    （见 experiments/run_experiment.py 的 family_output_dir =
    output_dir/"families"/family_id）——本脚本对输入目录做**递归查找**，
    同时兼容"直接落在顶层"和"落在 families/<id>/ 下"两种布局，不假设
    固定层级。

已知的、脚本会如实报告但不強行代入其他 setting 语义的边界情况（不在本脚本
里做任何"某个 setting 应该豁免"的特判，只客观统计并注明可能原因）：
    - experiments/baseline.py::SelfEvolutionRunner（Setting1 Self-Evolution，
      对应论文 Algorithm 1 去掉 server 的客户端部分）只调用
      ExecutionTraceRecorder.record_distillation()，从不调用
      record_stage1()/record_stage2()，因此 Setting1 下 stage1/stage2 的
      样本数天然为 0；同时 baseline.py 没有接入任何 CostAccountant，
      因此 Setting1 下预期就不会有 cost_ledger.jsonl。这是当前代码结构
      决定的，不是本次运行的 bug，也不是本脚本的 bug——脚本仍会如实打印
      "0 条记录" / "未找到该文件"，由使用者自行判断是否符合预期。

用法：
    python scripts/check_experiment_integrity.py results/setting1
    python scripts/check_experiment_integrity.py results/setting2 --json report.json

退出码：0 = PASS，1 = FAIL。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

TRACE_FILENAME = "experiment_execution_trace.jsonl"
COST_FILENAME = "cost_ledger.jsonl"
KNOWN_COMPONENTS = ("client_execution", "patch_distiller", "stage1_planner", "stage2_merge")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """逐行解析 JSONL；跳过空行；解析失败的行原样记录一条 error 标记，不中断整体统计。"""
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            rows.append({"__parse_error__": f"{path}:{line_no}: {exc}"})
    return rows


def _find_files(root: Path, filename: str) -> list[Path]:
    return sorted(root.rglob(filename))


def _scan_mock_markers(obj: Any, path: str, hits: list[str]) -> None:
    """递归扫描任意 JSON 结构，找出字面上的 mock/mock_used == true（当前 schema
    里不应存在这类字段，出现即高度异常，用于兜底防止未来格式变化悄悄引入）。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_lower = str(key).lower()
            if key_lower in ("mock", "mock_used", "is_mock") and value is True:
                hits.append(f"{path}.{key}=true")
            _scan_mock_markers(value, f"{path}.{key}", hits)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _scan_mock_markers(item, f"{path}[{idx}]", hits)


def check_trace_files(trace_files: list[Path]) -> dict[str, Any]:
    stage_counts = {
        "planner": {"total": 0, "llm_called_true": 0},
        "distillation": {"total": 0, "llm_called_true": 0},
        "stage2": {"total": 0, "llm_called_true": 0},
    }
    fallback_hits: list[str] = []
    mock_hits: list[str] = []
    parse_errors: list[str] = []

    for path in trace_files:
        rows = _load_jsonl(path)
        for row in rows:
            if "__parse_error__" in row:
                parse_errors.append(row["__parse_error__"])
                continue

            _scan_mock_markers(row, str(path), mock_hits)

            stage1 = row.get("stage1")
            if stage1 is not None:
                stage_counts["planner"]["total"] += 1
                if stage1.get("llm_called") is True:
                    stage_counts["planner"]["llm_called_true"] += 1
                if stage1.get("fallback_used") is True:
                    fallback_hits.append(
                        f"{path} round={row.get('round')} family={row.get('family')}"
                    )

            for entry in row.get("distillation") or []:
                stage_counts["distillation"]["total"] += 1
                if entry.get("llm_called") is True:
                    stage_counts["distillation"]["llm_called_true"] += 1

            for entry in row.get("stage2") or []:
                stage_counts["stage2"]["total"] += 1
                if entry.get("llm_called") is True:
                    stage_counts["stage2"]["llm_called_true"] += 1

    return {
        "files_found": [str(p) for p in trace_files],
        "stage_counts": stage_counts,
        "fallback_used_hits": fallback_hits,
        "mock_marker_hits": mock_hits,
        "parse_errors": parse_errors,
    }


def check_cost_files(cost_files: list[Path]) -> dict[str, Any]:
    by_component: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"n_calls": 0, "usd_cost_sum": 0.0, "tokens_sum": 0}
    )
    unknown_components: set[str] = set()
    mock_hits: list[str] = []
    parse_errors: list[str] = []

    for path in cost_files:
        rows = _load_jsonl(path)
        for row in rows:
            if "__parse_error__" in row:
                parse_errors.append(row["__parse_error__"])
                continue

            _scan_mock_markers(row, str(path), mock_hits)

            component = row.get("component")
            if component not in KNOWN_COMPONENTS:
                unknown_components.add(str(component))
            bucket = by_component[str(component)]
            bucket["n_calls"] += 1
            bucket["usd_cost_sum"] += float(row.get("usd_cost") or 0.0)
            tokens_in = row.get("tokens_input")
            tokens_out = row.get("tokens_output")
            if tokens_in is not None or tokens_out is not None:
                bucket["tokens_sum"] += (tokens_in or 0) + (tokens_out or 0)
            else:
                bucket["tokens_sum"] += row.get("tokens_total_hint") or 0

    # 确保 4 个已知 component 即使 0 条记录也出现在报告里（而不是悄悄缺失）。
    for comp in KNOWN_COMPONENTS:
        by_component.setdefault(comp, {"n_calls": 0, "usd_cost_sum": 0.0, "tokens_sum": 0})

    return {
        "files_found": [str(p) for p in cost_files],
        "by_component": {k: dict(v) for k, v in by_component.items()},
        "unknown_components": sorted(unknown_components),
        "mock_marker_hits": mock_hits,
        "parse_errors": parse_errors,
    }


def _ratio_str(total: int, llm_called_true: int) -> str:
    if total == 0:
        return "N/A（0 条记录）"
    pct = 100.0 * llm_called_true / total
    return f"{llm_called_true}/{total} = {pct:.1f}%"


def build_report(input_dir: Path) -> dict[str, Any]:
    trace_files = _find_files(input_dir, TRACE_FILENAME)
    cost_files = _find_files(input_dir, COST_FILENAME)

    trace_report = check_trace_files(trace_files)
    cost_report = check_cost_files(cost_files)

    failures: list[str] = []

    if not trace_files:
        failures.append(f"未找到任何 {TRACE_FILENAME} 文件")
    if trace_report["parse_errors"]:
        failures.append(f"{TRACE_FILENAME} 存在 {len(trace_report['parse_errors'])} 处 JSON 解析错误")
    for stage_name, counts in trace_report["stage_counts"].items():
        total = counts["total"]
        called = counts["llm_called_true"]
        if total > 0 and called < total:
            failures.append(
                f"{stage_name}: llm_called=true 占比未达 100%（{called}/{total}），"
                f"说明有调用静默跳过了真实 LLM"
            )
    if trace_report["fallback_used_hits"]:
        failures.append(
            f"发现 {len(trace_report['fallback_used_hits'])} 处 stage1.fallback_used=true"
        )
    if trace_report["mock_marker_hits"] or cost_report["mock_marker_hits"]:
        n = len(trace_report["mock_marker_hits"]) + len(cost_report["mock_marker_hits"])
        failures.append(f"发现 {n} 处字面 mock/mock_used=true 标记")

    if not cost_files:
        failures.append(f"未找到任何 {COST_FILENAME} 文件")
    if cost_report["parse_errors"]:
        failures.append(f"{COST_FILENAME} 存在 {len(cost_report['parse_errors'])} 处 JSON 解析错误")
    if cost_report["unknown_components"]:
        failures.append(f"cost_ledger.jsonl 出现未知 component: {cost_report['unknown_components']}")

    verdict = "FAIL" if failures else "PASS"

    return {
        "input_dir": str(input_dir),
        "trace": trace_report,
        "cost": cost_report,
        "failures": failures,
        "verdict": verdict,
    }


def print_report(report: dict[str, Any]) -> None:
    print("=" * 70)
    print(f"实验完整性检查: {report['input_dir']}")
    print("=" * 70)

    trace = report["trace"]
    print(f"\n[1] {TRACE_FILENAME}（找到 {len(trace['files_found'])} 个文件）")
    stage_label = {"planner": "planner (stage1)", "distillation": "distillation", "stage2": "stage2"}
    for stage_name, counts in trace["stage_counts"].items():
        print(f"    {stage_label[stage_name]:<20} llm_called=true 占比: "
              f"{_ratio_str(counts['total'], counts['llm_called_true'])}")

    cost = report["cost"]
    print(f"\n[2] {COST_FILENAME}（找到 {len(cost['files_found'])} 个文件）按 component 统计")
    for comp in KNOWN_COMPONENTS:
        stats = cost["by_component"].get(comp, {"n_calls": 0, "usd_cost_sum": 0.0, "tokens_sum": 0})
        print(f"    {comp:<18} 调用次数={stats['n_calls']:<6} "
              f"usd_cost 合计=${stats['usd_cost_sum']:.4f}  tokens 合计={stats['tokens_sum']}")
    if cost["unknown_components"]:
        print(f"    ⚠ 未知 component: {cost['unknown_components']}")

    print("\n[3] mock / fallback 异常标记")
    if trace["fallback_used_hits"]:
        print(f"    ✗ stage1.fallback_used=true 共 {len(trace['fallback_used_hits'])} 处:")
        for hit in trace["fallback_used_hits"][:20]:
            print(f"        - {hit}")
        if len(trace["fallback_used_hits"]) > 20:
            print(f"        ...(共 {len(trace['fallback_used_hits'])} 处，仅显示前 20)")
    else:
        print("    ✓ 未发现 stage1.fallback_used=true")

    all_mock_hits = trace["mock_marker_hits"] + cost["mock_marker_hits"]
    if all_mock_hits:
        print(f"    ✗ 字面 mock/mock_used=true 标记共 {len(all_mock_hits)} 处:")
        for hit in all_mock_hits[:20]:
            print(f"        - {hit}")
    else:
        print("    ✓ 未发现字面 mock/mock_used=true 标记")

    print(f"\n[4] 结论: {report['verdict']}")
    if report["failures"]:
        for reason in report["failures"]:
            print(f"    - {reason}")
    print("=" * 70)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_experiment_integrity.py",
        description="核对真实实验产物（experiment_execution_trace.jsonl / cost_ledger.jsonl）的执行完整性，只读，不修改任何实验代码或产物。",
    )
    parser.add_argument("input_dir", type=str, nargs="?", default="results/setting1",
                         help="实验输出目录（默认 results/setting1），脚本会递归查找其中的 "
                              f"{TRACE_FILENAME} / {COST_FILENAME}")
    parser.add_argument("--json", type=str, default=None, help="额外把完整报告写成 JSON 文件")
    args = parser.parse_args(argv)

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        print(f"[FAIL] 输入目录不存在: {input_dir}")
        return 1

    report = build_report(input_dir)
    print_report(report)

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n完整报告已写入: {args.json}")

    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
