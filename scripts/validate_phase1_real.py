"""只读审计 Phase 1 artifacts，并生成 phase1_validation_report.md。"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_federated_fidelity import build_report

EXPECTED_FAMILIES = {
    "Compensation-Scenario-Modeling",
    "Cross-Format-Data-Reconciliation",
    "Distribution-Center-Auditing",
}
REQUIRED_COMPONENTS = {
    "client_execution", "patch_distiller", "stage1_planner", "stage2_merge",
}
REQUIRED_ACTIONS = {"absorb", "repair", "refactor"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return rows


def validate(input_dir: Path) -> tuple[dict[str, Any], bool]:
    failures: list[str] = []
    family_reports: dict[str, Any] = {}
    family_root = input_dir / "families"
    actual_families = {path.name for path in family_root.iterdir() if path.is_dir()} if family_root.is_dir() else set()
    if actual_families != EXPECTED_FAMILIES:
        failures.append(
            f"family 范围不符: expected={sorted(EXPECTED_FAMILIES)}, actual={sorted(actual_families)}"
        )

    all_actions: Counter[str] = Counter()
    cross_client_transfers = 0
    non_empty_libraries: list[str] = []

    for family_id in sorted(EXPECTED_FAMILIES):
        directory = family_root / family_id
        report: dict[str, Any] = {}
        family_reports[family_id] = report
        required_files = {
            "execution_trace": directory / "experiment_execution_trace.jsonl",
            "capability_matrix": directory / "capability_matrix.jsonl",
            "cost_ledger": directory / "cost_ledger.jsonl",
            "evolution_trace": directory / "evolution_trace.jsonl",
        }
        missing = [name for name, path in required_files.items() if not path.is_file()]
        if missing:
            failures.append(f"{family_id}: 缺少 artifacts {missing}")
            report["missing_artifacts"] = missing
            continue

        traces = load_jsonl(required_files["execution_trace"])
        stage1_called = sum(bool(row.get("stage1", {}).get("llm_called")) for row in traces if row.get("stage1"))
        patches = sum(
            bool(item.get("patch_generated"))
            for row in traces for item in row.get("distillation", [])
        )
        merge_actions = [
            str(item.get("merge_action", ""))
            for row in traces for item in row.get("stage2", [])
            if item.get("merge_action")
        ]
        report["execution_trace"] = {
            "rounds": len(traces),
            "stage1_llm_called": stage1_called,
            "patches_generated": patches,
            "stage2_merge_actions": dict(Counter(merge_actions)),
        }
        if len(traces) != 8:
            failures.append(f"{family_id}: execution trace rounds={len(traces)}，预期 8")
        if stage1_called == 0:
            failures.append(f"{family_id}: Stage1 planner 未调用")
        if patches == 0:
            failures.append(f"{family_id}: 未生成任何 distilled patch")
        if not merge_actions:
            failures.append(f"{family_id}: Stage2 无 merge_action")

        matrices = load_jsonl(required_files["capability_matrix"])
        non_empty_matrices = sum(bool(row.get("matrix")) for row in matrices)
        report["capability_matrix"] = {
            "records": len(matrices), "non_empty_records": non_empty_matrices,
        }
        if non_empty_matrices == 0:
            failures.append(f"{family_id}: capability matrix 全为空")

        costs = load_jsonl(required_files["cost_ledger"])
        components = Counter(str(row.get("component")) for row in costs)
        report["cost_components"] = dict(components)
        missing_components = REQUIRED_COMPONENTS - set(components)
        if missing_components:
            failures.append(f"{family_id}: cost ledger 缺少 {sorted(missing_components)}")

        evolution = load_jsonl(required_files["evolution_trace"])
        actions = Counter(str(row.get("action", "")).lower() for row in evolution)
        all_actions.update(actions)
        transfers = sum(
            row.get("source_patch_client") not in (None, row.get("client_id"))
            and str(row.get("action", "")).lower() in {"absorb", "refactor"}
            for row in evolution
        )
        cross_client_transfers += transfers
        report["decision_actions"] = dict(actions)
        report["cross_client_transfers"] = transfers

        library_dirs = [path for path in (directory / "libraries").glob("*") if path.is_dir()]
        for library_dir in library_dirs:
            if any(path.is_file() for path in library_dir.rglob("*")):
                non_empty_libraries.append(str(library_dir.relative_to(input_dir)))

    fidelity = build_report(input_dir)
    if fidelity.get("verdict") != "PASS":
        failures.append(f"Fidelity Checker={fidelity.get('verdict')}: {fidelity.get('failures', [])}")
    missing_actions = REQUIRED_ACTIONS - set(all_actions)
    if missing_actions:
        failures.append(f"decision logs 缺少案例: {sorted(missing_actions)}")
    if cross_client_transfers == 0:
        failures.append("未发现 cross-client ABSORB/REFACTOR transfer")
    if not non_empty_libraries:
        failures.append("所有 worker skill libraries 均为空")

    report = {
        "verdict": "PASS" if not failures else "FAIL",
        "input_dir": str(input_dir),
        "expected_families": sorted(EXPECTED_FAMILIES),
        "family_reports": family_reports,
        "non_empty_libraries": non_empty_libraries,
        "decision_action_counts": dict(all_actions),
        "cross_client_transfers": cross_client_transfers,
        "fidelity_checker_verdict": fidelity.get("verdict"),
        "fidelity_checker_failures": fidelity.get("failures", []),
        "failures": failures,
    }
    return report, not failures


def write_markdown(report: dict[str, Any], output_path: Path) -> None:
    lines = [
        "# Phase 1 Validation Report",
        "",
        f"**Verdict:** {report['verdict']}",
        "",
        f"- Input: `{report['input_dir']}`",
        f"- Fidelity Checker: `{report['fidelity_checker_verdict']}`",
        f"- Cross-client transfers: `{report['cross_client_transfers']}`",
        f"- Decision actions: `{json.dumps(report['decision_action_counts'], ensure_ascii=False)}`",
        f"- Non-empty libraries: `{len(report['non_empty_libraries'])}`",
        "",
        "## Family Checks",
        "",
    ]
    for family_id, family in report["family_reports"].items():
        lines.extend([f"### {family_id}", "", "```json", json.dumps(family, ensure_ascii=False, indent=2), "```", ""])
    lines.extend(["## Failures", ""])
    lines.extend([f"- {failure}" for failure in report["failures"]] or ["- None"])
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir", nargs="?", type=Path, default=ROOT / "results" / "phase1_setting4_real3")
    parser.add_argument("--output", type=Path, default=ROOT / "phase1_validation_report.md")
    args = parser.parse_args()
    report, passed = validate(args.input_dir)
    write_markdown(report, args.output)
    json_path = args.output.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"verdict={report['verdict']}")
    print(f"markdown={args.output}")
    print(f"json={json_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
