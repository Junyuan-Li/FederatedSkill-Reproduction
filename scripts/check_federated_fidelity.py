"""
scripts/check_federated_fidelity.py — FederatedSkill Artifact Fidelity
Hardening TASK3：Federated (Setting2-4) 保真度检查器（只读脚本，不修改任何
实验代码 / 不修改任何实验产物 / 不重新执行任何 Stage1/Stage2 逻辑）。

背景：
    单看某一个产物文件（例如 cost_ledger.jsonl 有记录）并不能证明 server
    evolution agent 真的按论文 Section 4.2.2 跑了"个性化演化"——它可能只是
    对每个 worker 都 keep 原样（no_update）、从不真正吸收/修复/重写任何
    技能。本脚本把"这次跑的是真实、有实质决策产出的联邦演化管线"这句话
    拆成 6 条可独立核对、可读取已有落盘产物验证的具体断言。

产物来源（与已有各 recorder 的落盘位置一致，见对应模块 docstring）：
    <output_dir>/families/<family_id>/experiment_execution_trace.jsonl
        （evaluation/integrity_logs.py::ExecutionTraceRecorder）
    <output_dir>/families/<family_id>/evolution_trace.jsonl
        （evaluation/audit_trace.py::AuditTraceRecorder）
    <output_dir>/families/<family_id>/capability_matrix.jsonl
        （evaluation/integrity_logs.py::CapabilityMatrixRecorder，TASK1 新增）
    <output_dir>/families/<family_id>/libraries/<worker_id>/**
        （client 真实技能库 library_root，见 experiments/run_experiment.py）
本脚本对输入目录做**递归查找**，不假设固定层级，同时兼容"直接落在顶层"和
"落在 families/<id>/ 下"两种布局。

六项检查（对应用户显式给出的编号，下方 build_report() 里同时以 stage1/stage2/
    directive/transfer/library/capability 六个命名布尔值形式暴露，与
    "FederatedSkill Faithful Mock Validation" TASK4 要求的 "Stage1 PASS/
    Stage2 PASS/directive PASS/transfer PASS/library PASS/capability PASS"
    报告格式一致）：
    1. Stage1 planner 真实调用次数 > 0
       —— experiment_execution_trace.jsonl 里 stage1.llm_called=true 的行数。
    2. Stage2 evolution agent 真实调用次数 > 0
       —— experiment_execution_trace.jsonl 里 stage2[].llm_called=true 的条数。
    3. 是否存在非空 directive
       —— evolution_trace.jsonl 里 action != "no_update"（absorb/repair/
          refactor 三者之一，即 Stage1 真的下发过 directive 且被 Stage2
          真实执行）的记录数 > 0。
    4. 是否存在 source_client != target_client
       —— evolution_trace.jsonl 里 source_patch_client 不为空且不等于
          client_id 的记录数 > 0（真实发生过跨 client 的 ABSORB，而不是
          "server 只会原地合并自己的 patch"）。
    5. 是否存在 skill library 变化
       —— libraries/<worker_id>/ 下存在至少一个真实文件（技能库真的被写
          入过，不是全程空目录）。
    6. capability_matrix 是否存在
       —— 至少找到一个 capability_matrix.jsonl，且每一行都能解析出
          round_idx/timestamp/matrix 三个字段，matrix 非空。

附加追踪（"FederatedSkill Faithful Mock Validation" TASK3 新增，仅信息报告，
不影响上面六项 PASS/FAIL 判定）：
    skill_count_before/after —— 从 <output_dir>/.../round_XX_summary.json 的
        "snapshots" 列表（experiments/run_experiment.py::_save_results() 写入）
        里读取真实 technology library_size_before/after 快照，汇总出整个
        实验跪度上技能库从“开始”到“结束”的技能数变化，作为辅助证据，
        不作为硬性 gate（因为合法的 repair/absorb 结果不一定会增加数量，
        只是“追踪”，不是“验证”）。

用法：
    python scripts/check_federated_fidelity.py results/setting4
    python scripts/check_federated_fidelity.py results/setting2 --json report.json

退出码：0 = PASS，1 = FAIL。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

TRACE_FILENAME = "experiment_execution_trace.jsonl"
EVOLUTION_TRACE_FILENAME = "evolution_trace.jsonl"
CAPABILITY_MATRIX_FILENAME = "capability_matrix.jsonl"
LIBRARIES_DIRNAME = "libraries"

NO_UPDATE_ACTION = "no_update"


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


def check_stage_invocations(trace_files: list[Path]) -> dict[str, Any]:
    """检查项 1/2：Stage1 planner / Stage2 evolution agent 的真实调用次数。"""
    stage1_llm_called = 0
    stage1_total = 0
    stage2_llm_called = 0
    stage2_total = 0
    parse_errors: list[str] = []

    for path in trace_files:
        for row in _load_jsonl(path):
            if "__parse_error__" in row:
                parse_errors.append(row["__parse_error__"])
                continue
            stage1 = row.get("stage1")
            if stage1 is not None:
                stage1_total += 1
                if stage1.get("llm_called") is True:
                    stage1_llm_called += 1
            for entry in row.get("stage2") or []:
                stage2_total += 1
                if entry.get("llm_called") is True:
                    stage2_llm_called += 1

    return {
        "files_found": [str(p) for p in trace_files],
        "stage1_total": stage1_total,
        "stage1_llm_called": stage1_llm_called,
        "stage2_total": stage2_total,
        "stage2_llm_called": stage2_llm_called,
        "parse_errors": parse_errors,
    }


def check_directive_and_transfer(evolution_trace_files: list[Path]) -> dict[str, Any]:
    """检查项 3/4：非空 directive（真实 merge action）+ 跨 client transfer。"""
    non_empty_directive_hits: list[str] = []
    cross_client_hits: list[str] = []
    action_counts: dict[str, int] = {}
    parse_errors: list[str] = []
    total_records = 0

    for path in evolution_trace_files:
        for row in _load_jsonl(path):
            if "__parse_error__" in row:
                parse_errors.append(row["__parse_error__"])
                continue
            total_records += 1
            action = str(row.get("action"))
            action_counts[action] = action_counts.get(action, 0) + 1

            if action != NO_UPDATE_ACTION:
                non_empty_directive_hits.append(
                    f"{path} round={row.get('round_idx')} client={row.get('client_id')} "
                    f"action={action} workflow/skill_path={row.get('skill_path')}"
                )

            source = row.get("source_patch_client")
            target = row.get("client_id")
            if source is not None and source != target:
                cross_client_hits.append(
                    f"{path} round={row.get('round_idx')} source={source} target={target} "
                    f"action={action}"
                )

    return {
        "files_found": [str(p) for p in evolution_trace_files],
        "total_records": total_records,
        "action_counts": action_counts,
        "non_empty_directive_hits": non_empty_directive_hits,
        "cross_client_hits": cross_client_hits,
        "parse_errors": parse_errors,
    }


def check_library_changes(root: Path) -> dict[str, Any]:
    """检查项 5：libraries/<worker_id>/ 下是否存在真实写入过的技能库文件。"""
    non_empty_dirs: list[str] = []
    empty_dirs: list[str] = []

    # "libraries/*" 精确枚举每一个 libraries/<worker_id>（或 libraries/shared）
    # 目录本身（root.rglob 会匹配任意深度下名为 libraries 的目录的直接子项）。
    worker_dirs = sorted({p for p in root.rglob(f"{LIBRARIES_DIRNAME}/*") if p.is_dir()})

    for wdir in worker_dirs:
        files = [f for f in wdir.rglob("*") if f.is_file()]
        if files:
            non_empty_dirs.append(str(wdir))
        else:
            empty_dirs.append(str(wdir))

    return {
        "worker_library_dirs_found": [str(d) for d in worker_dirs],
        "non_empty_dirs": non_empty_dirs,
        "empty_dirs": empty_dirs,
    }


ROUND_SUMMARY_GLOB = "round_*_summary.json"


def check_skill_growth(root: Path) -> dict[str, Any]:
    """
    ["FederatedSkill Faithful Mock Validation" TASK3 新增] skill_count_before/
    after 追踪（仅信息性统计，不参与 PASS/FAIL 判定）。

    数据来源：<output_dir>/.../round_XX_summary.json 的 "snapshots" 列表
    （experiments/run_experiment.py::_save_results() 写入，字段来自
    evaluation/metrics.py::TrialSnapshot，最终取值是 client/library.py::
    SkillLibrary.skill_count() 的真实快照，本脚本只读取，不重新计算）。

    对每个 worker_id：取其出现过的最小 round_idx 记录的 library_size_before
    作为该 worker 实验开始时的技能数，最大 round_idx 记录的
    library_size_after 作为实验结束时的技能数；skill_count_before/after
    为所有 worker 起点/终点之和。
    """
    round_files = sorted(root.rglob(ROUND_SUMMARY_GLOB))
    first_seen: dict[str, tuple[int, int]] = {}  # worker_id -> (round_idx, before)
    last_seen: dict[str, tuple[int, int]] = {}   # worker_id -> (round_idx, after)
    parse_errors: list[str] = []

    for path in round_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            parse_errors.append(f"{path}: {exc}")
            continue
        for snap in data.get("snapshots") or []:
            wid = snap.get("worker_id")
            r_idx = snap.get("round_idx")
            before = snap.get("library_size_before")
            after = snap.get("library_size_after")
            if wid is None or r_idx is None:
                continue
            if before is not None and (wid not in first_seen or r_idx < first_seen[wid][0]):
                first_seen[wid] = (r_idx, before)
            if after is not None and (wid not in last_seen or r_idx > last_seen[wid][0]):
                last_seen[wid] = (r_idx, after)

    skill_count_before = sum(v[1] for v in first_seen.values())
    skill_count_after = sum(v[1] for v in last_seen.values())

    return {
        "files_found": [str(p) for p in round_files],
        "per_worker_before": {wid: v[1] for wid, v in first_seen.items()},
        "per_worker_after": {wid: v[1] for wid, v in last_seen.items()},
        "skill_count_before": skill_count_before,
        "skill_count_after": skill_count_after,
        "parse_errors": parse_errors,
    }


def check_capability_matrix(capability_matrix_files: list[Path]) -> dict[str, Any]:
    """检查项 6：capability_matrix.jsonl 是否存在，且每行 schema 完整。"""
    total_rows = 0
    schema_ok_rows = 0
    non_empty_matrix_rows = 0
    parse_errors: list[str] = []
    schema_errors: list[str] = []

    for path in capability_matrix_files:
        for row in _load_jsonl(path):
            if "__parse_error__" in row:
                parse_errors.append(row["__parse_error__"])
                continue
            total_rows += 1
            has_fields = (
                "round_idx" in row and "timestamp" in row and "matrix" in row
            )
            if not has_fields:
                schema_errors.append(f"{path}: 缺少 round_idx/timestamp/matrix 字段")
                continue
            schema_ok_rows += 1
            matrix = row.get("matrix") or {}
            if isinstance(matrix, dict) and len(matrix) > 0:
                non_empty_matrix_rows += 1

    return {
        "files_found": [str(p) for p in capability_matrix_files],
        "total_rows": total_rows,
        "schema_ok_rows": schema_ok_rows,
        "non_empty_matrix_rows": non_empty_matrix_rows,
        "parse_errors": parse_errors,
        "schema_errors": schema_errors,
    }


def build_report(input_dir: Path) -> dict[str, Any]:
    trace_files = _find_files(input_dir, TRACE_FILENAME)
    evolution_trace_files = _find_files(input_dir, EVOLUTION_TRACE_FILENAME)
    capability_matrix_files = _find_files(input_dir, CAPABILITY_MATRIX_FILENAME)

    stage_report = check_stage_invocations(trace_files)
    directive_report = check_directive_and_transfer(evolution_trace_files)
    library_report = check_library_changes(input_dir)
    capability_report = check_capability_matrix(capability_matrix_files)
    # ["FederatedSkill Faithful Mock Validation" TASK3 新增] 仅信息性统计，
    # 不参与下面任何 PASS/FAIL 判定。
    growth_report = check_skill_growth(input_dir)

    failures: list[str] = []

    # 1. Stage1 planner 真实调用次数 > 0
    stage1_ok = bool(trace_files) and stage_report["stage1_llm_called"] > 0
    if not stage1_ok:
        failures.append(
            f"[1/Stage1] Stage1 planner 真实调用次数 = {stage_report['stage1_llm_called']}（要求 > 0）"
        )
    # 2. Stage2 evolution agent 真实调用次数 > 0
    stage2_ok = bool(trace_files) and stage_report["stage2_llm_called"] > 0
    if not stage2_ok:
        failures.append(
            f"[2/Stage2] Stage2 evolution agent 真实调用次数 = {stage_report['stage2_llm_called']}（要求 > 0）"
        )
    # 3. 是否存在非空 directive
    directive_ok = bool(evolution_trace_files) and bool(directive_report["non_empty_directive_hits"])
    if not directive_ok:
        failures.append("[3/directive] 未发现任何非空 directive（所有记录 action 均为 no_update）")
    # 4. 是否存在 source_client != target_client
    transfer_ok = bool(evolution_trace_files) and bool(directive_report["cross_client_hits"])
    if not transfer_ok:
        failures.append("[4/transfer] 未发现任何 source_client != target_client 的跨 client 记录")
    # 5. 是否存在 skill library 变化
    library_ok = bool(library_report["non_empty_dirs"])
    if not library_ok:
        failures.append("[5/library] 未发现任何非空 libraries/<worker_id>/ 技能库目录")
    # 6. capability_matrix 是否存在且非空（TASK3：明确作为独立的
    #    capability 检查项，schema 错误同样导致该项 FAIL）
    capability_ok = (
        bool(capability_matrix_files)
        and capability_report["non_empty_matrix_rows"] > 0
        and not capability_report["schema_errors"]
    )
    if not capability_matrix_files:
        failures.append(f"[6/capability] 未找到任何 {CAPABILITY_MATRIX_FILENAME} 文件")
    elif capability_report["non_empty_matrix_rows"] <= 0:
        failures.append("[6/capability] capability_matrix.jsonl 存在，但没有任何一行包含非空 matrix")
    if capability_report["schema_errors"]:
        failures.append(
            f"[6/capability] capability_matrix.jsonl 存在 {len(capability_report['schema_errors'])} 处 schema 错误"
        )

    checks: dict[str, bool] = {
        "stage1": stage1_ok,
        "stage2": stage2_ok,
        "directive": directive_ok,
        "transfer": transfer_ok,
        "library": library_ok,
        "capability": capability_ok,
    }

    # 解析错误（任意来源）一律视为 FAIL，不静默忽略。
    all_parse_errors = (
        stage_report["parse_errors"]
        + directive_report["parse_errors"]
        + capability_report["parse_errors"]
        + growth_report["parse_errors"]
    )
    if all_parse_errors:
        failures.append(f"发现 {len(all_parse_errors)} 处 JSON 解析错误")

    if not trace_files:
        failures.append(f"未找到任何 {TRACE_FILENAME} 文件")
    if not evolution_trace_files:
        failures.append(f"未找到任何 {EVOLUTION_TRACE_FILENAME} 文件")

    verdict = "FAIL" if failures else "PASS"

    return {
        "input_dir": str(input_dir),
        "stage_invocations": stage_report,
        "directive_and_transfer": directive_report,
        "library_changes": library_report,
        "capability_matrix": capability_report,
        "skill_growth": growth_report,
        "checks": checks,
        "failures": failures,
        "verdict": verdict,
    }


def print_report(report: dict[str, Any]) -> None:
    print("=" * 74)
    print(f"FederatedSkill Fidelity Check: {report['input_dir']}")
    print("=" * 74)

    stage = report["stage_invocations"]
    print(f"\n[1/2] {TRACE_FILENAME}（找到 {len(stage['files_found'])} 个文件）")
    print(f"    Stage1 planner  llm_called=true: {stage['stage1_llm_called']} / {stage['stage1_total']}")
    print(f"    Stage2 evolution agent llm_called=true: {stage['stage2_llm_called']} / {stage['stage2_total']}")

    directive = report["directive_and_transfer"]
    print(f"\n[3/4] {EVOLUTION_TRACE_FILENAME}（找到 {len(directive['files_found'])} 个文件，"
          f"共 {directive['total_records']} 条记录）")
    print(f"    action 分布: {directive['action_counts']}")
    print(f"    非空 directive（action != no_update）记录数: {len(directive['non_empty_directive_hits'])}")
    for hit in directive["non_empty_directive_hits"][:10]:
        print(f"        - {hit}")
    if len(directive["non_empty_directive_hits"]) > 10:
        print(f"        ...(共 {len(directive['non_empty_directive_hits'])} 条，仅显示前 10)")
    print(f"    source_client != target_client 记录数: {len(directive['cross_client_hits'])}")
    for hit in directive["cross_client_hits"][:10]:
        print(f"        - {hit}")
    if len(directive["cross_client_hits"]) > 10:
        print(f"        ...(共 {len(directive['cross_client_hits'])} 条，仅显示前 10)")

    library = report["library_changes"]
    print(f"\n[5] {LIBRARIES_DIRNAME}/<worker_id>/ 技能库变化"
          f"（找到 {len(library['worker_library_dirs_found'])} 个 worker 目录）")
    print(f"    非空目录: {len(library['non_empty_dirs'])}  空目录: {len(library['empty_dirs'])}")
    for d in library["empty_dirs"][:10]:
        print(f"        - (空) {d}")

    capability = report["capability_matrix"]
    print(f"\n[6] {CAPABILITY_MATRIX_FILENAME}（找到 {len(capability['files_found'])} 个文件，"
          f"共 {capability['total_rows']} 行）")
    print(f"    schema 正确行数: {capability['schema_ok_rows']}  非空 matrix 行数: {capability['non_empty_matrix_rows']}")

    growth = report["skill_growth"]
    print(f"\n[TASK3] skill_count_before/after追踪（找到 {len(growth['files_found'])} 个 "
          f"{ROUND_SUMMARY_GLOB} 文件，仅信息报告，不参与 PASS/FAIL 判定）")
    print(f"    skill_count_before(全部 worker 汇总) = {growth['skill_count_before']}")
    print(f"    skill_count_after (全部 worker 汇总) = {growth['skill_count_after']}")
    for wid in sorted(set(growth["per_worker_before"]) | set(growth["per_worker_after"])):
        before = growth["per_worker_before"].get(wid, "?")
        after = growth["per_worker_after"].get(wid, "?")
        print(f"        - {wid}: before={before} -> after={after}")

    checks = report["checks"]
    print("\n[六项命名检查]")
    for name in ("stage1", "stage2", "directive", "transfer", "library", "capability"):
        print(f"    {name} {'PASS' if checks[name] else 'FAIL'}")

    print(f"\n[结论] {report['verdict']}")
    if report["failures"]:
        for reason in report["failures"]:
            print(f"    - {reason}")
    print("=" * 74)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_federated_fidelity.py",
        description=(
            "检查 Federated (Setting2-4) 实验产物是否证明 server evolution agent "
            "真的执行了论文 Section 4.2.1/4.2.2 定义的能力矩阵 + 个性化演化，"
            "而不是退化成 global_skill=merge(all)。只读，不修改任何实验代码或产物。"
        ),
    )
    parser.add_argument(
        "input_dir", type=str, nargs="?", default="results/setting4",
        help="实验输出目录（默认 results/setting4），脚本会递归查找其中的产物文件",
    )
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
