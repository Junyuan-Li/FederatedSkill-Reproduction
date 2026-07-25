from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_FAMILY_DIR = ROOT / "benchmark" / "families"
FORBIDDEN_OUTPUT_PARTS = {
    "subset_setting1_self_evolution",
    "v2",
    "phase1",
}
FORBIDDEN_STATE_FILES = {"SKILL.md", "patch.json", "task_status.json", "trajectory.json"}


def load_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_family(family_id: str) -> dict[str, Any]:
    path = BENCHMARK_FAMILY_DIR / f"{family_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"family metadata not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def family_difficulty(tasks: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for task in tasks:
        raw = task.get("metadata", {}).get("raw_toml", {}).get("metadata", {}).get("difficulty")
        if raw:
            values.append(str(raw))
    return "+".join(sorted(set(values))) if values else "unknown"


def output_dir_from_config(config: dict[str, Any]) -> Path:
    output = Path(str(config.get("output_dir", "")))
    return output if output.is_absolute() else ROOT / output


def validate_output_policy(output_dir: Path) -> list[str]:
    problems: list[str] = []
    relative = output_dir.relative_to(ROOT) if output_dir.is_relative_to(ROOT) else output_dir
    parts = set(relative.parts)
    if parts & FORBIDDEN_OUTPUT_PARTS:
        problems.append(f"forbidden output path: {relative}")
    expected = Path("results") / "subset_setting1_v3"
    if relative != expected:
        problems.append(f"output_dir must be {expected}, got {relative}")
    if output_dir.exists():
        found = [p for p in output_dir.rglob("*") if p.is_file() and p.name in FORBIDDEN_STATE_FILES]
        if found:
            problems.append("state isolation failed: old state files found: " + ", ".join(str(p.relative_to(output_dir)) for p in found[:20]))
        else:
            problems.append(f"output_dir already exists but has no forbidden state files: {relative}")
    return problems


def dry_run(config_path: Path) -> int:
    config = load_config(config_path)
    families = list(config.get("family_subset") or [])
    output_dir = output_dir_from_config(config)

    print("[subset_setting1_v3 dry-run]")
    print(f"config={config_path.relative_to(ROOT)}")
    print(f"output_dir={output_dir.relative_to(ROOT)}")
    print(f"family_count={len(families)}")
    print(f"worker_count={len(config.get('workers') or [])}")
    for worker in config.get("workers") or []:
        print(f"worker={worker.get('client_id')} model={worker.get('backbone_model')} harness={worker.get('agent_harness')}")

    failures: list[str] = []
    if len(families) != 2:
        failures.append(f"family_count must be 2, got {len(families)}")
    if len(config.get("workers") or []) != 1:
        failures.append("Setting1 must have exactly one worker")
    if config.get("federated") is not False:
        failures.append("Setting1 config must set federated=false")

    for family_id in families:
        data = load_family(family_id)
        tasks = list(data.get("tasks") or [])
        task_ids = [str(task.get("task_id")) for task in tasks]
        difficulty = family_difficulty(tasks)
        print("---")
        print(f"family_id={family_id}")
        print(f"difficulty={difficulty}")
        print(f"task_count={len(tasks)}")
        print("task_ids=" + ",".join(task_ids))
        print("task_order=" + " -> ".join(task_ids))
        if len(tasks) != 8:
            failures.append(f"{family_id}: task_count must be 8, got {len(tasks)}")

    failures.extend(validate_output_policy(output_dir))
    if failures:
        print("[dry-run verdict] FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("[dry-run verdict] PASS")
    print("state_isolation=PASS: target output directory does not contain prior state")
    return 0


ROUND_DIR_RE = re.compile(r"^round_(?P<round>\d{3})_(?P<task>.+)$")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_task_rows(output_dir: Path, families: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family_id in families:
        task_root = output_dir / "families" / family_id / "workers" / "u0" / "tasks"
        if not task_root.is_dir():
            continue
        for directory in sorted(p for p in task_root.iterdir() if p.is_dir()):
            match = ROUND_DIR_RE.match(directory.name)
            round_idx = int(match.group("round")) if match else -1
            status_path = directory / "task_status.json"
            reward_path = directory / "reward.json"
            status = read_json(status_path) if status_path.is_file() else {}
            reward = read_json(reward_path) if reward_path.is_file() else {}
            reward_value = reward.get("reward", status.get("reward"))
            success = bool(reward_value == 1.0 and str(status.get("status", "")).startswith("completed"))
            rows.append({
                "family_id": family_id,
                "task_id": status.get("task_id") or (match.group("task") if match else directory.name),
                "worker_id": status.get("worker_id", "u0"),
                "round": round_idx,
                "reward": "" if reward_value is None else reward_value,
                "status": status.get("status", "missing_status"),
                "success": int(success),
                "failure_reason": status.get("failure_reason", ""),
            })
    return sorted(rows, key=lambda row: (row["family_id"], row["round"]))


def collect_family_rows(task_rows: list[dict[str, Any]], families: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_family: dict[str, list[dict[str, Any]]] = {family_id: [] for family_id in families}
    for row in task_rows:
        by_family.setdefault(str(row["family_id"]), []).append(row)
    for family_id in families:
        rows_for_family = by_family.get(family_id, [])
        successes = sum(int(row["success"]) for row in rows_for_family)
        total = len(rows_for_family)
        rows.append({
            "family": family_id,
            "tasks_recorded": total,
            "expected_tasks": 8,
            "successful_tasks": successes,
            "success_rate": (successes / total) if total else 0.0,
            "complete": int(total == 8),
        })
    return rows


def patch_counts(patch_path: Path, existing_paths: set[str]) -> tuple[int, int, int]:
    if not patch_path.is_file():
        return 0, 0, 0
    patch = read_json(patch_path)
    upserts = patch.get("upserts") or {}
    deletions = patch.get("deletions") or patch.get("delete_paths") or []
    added = 0
    edited = 0
    for path in upserts:
        if path in existing_paths:
            edited += 1
        else:
            added += 1
        existing_paths.add(path)
    deleted = 0
    for path in deletions:
        if path in existing_paths:
            deleted += 1
            existing_paths.remove(path)
    return added, edited, deleted


def collect_skill_rows(output_dir: Path, families: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family_id in families:
        existing_paths: set[str] = set()
        task_root = output_dir / "families" / family_id / "workers" / "u0" / "tasks"
        if not task_root.is_dir():
            continue
        for directory in sorted(p for p in task_root.iterdir() if p.is_dir()):
            match = ROUND_DIR_RE.match(directory.name)
            round_idx = int(match.group("round")) if match else -1
            added, edited, deleted = patch_counts(directory / "patch.json", existing_paths)
            total_skills = sum(1 for path in existing_paths if path.endswith("SKILL.md"))
            rows.append({
                "family_id": family_id,
                "round_idx": round_idx,
                "added_skills": added,
                "edited_skills": edited,
                "deleted_skills": deleted,
                "total_skills": total_skills,
            })
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def trajectory_summary(output_dir: Path, task_rows: list[dict[str, Any]]) -> tuple[int, int]:
    total = 0
    with_steps = 0
    for row in task_rows:
        family_id = str(row["family_id"])
        round_idx = int(row["round"])
        task_root = output_dir / "families" / family_id / "workers" / "u0" / "tasks"
        matches = list(task_root.glob(f"round_{round_idx:03d}_*/trajectory.json"))
        total += 1
        if matches:
            data = read_json(matches[0])
            if data.get("steps"):
                with_steps += 1
    return total, with_steps


def export(config_path: Path) -> int:
    config = load_config(config_path)
    output_dir = output_dir_from_config(config)
    families = list(config.get("family_subset") or [])
    output_dir.mkdir(parents=True, exist_ok=True)

    task_rows = collect_task_rows(output_dir, families)
    family_rows = collect_family_rows(task_rows, families)
    skill_rows = collect_skill_rows(output_dir, families)

    write_csv(output_dir / "task_level_results.csv", task_rows, [
        "family_id", "task_id", "worker_id", "round", "reward", "status", "success", "failure_reason",
    ])
    write_csv(output_dir / "family_results.csv", family_rows, [
        "family", "tasks_recorded", "expected_tasks", "successful_tasks", "success_rate", "complete",
    ])
    write_csv(output_dir / "skill_growth.csv", skill_rows, [
        "family_id", "round_idx", "added_skills", "edited_skills", "deleted_skills", "total_skills",
    ])

    overall_successes = sum(int(row["success"]) for row in task_rows)
    overall_total = len(task_rows)
    overall_sr = overall_successes / overall_total if overall_total else 0.0
    avg_library_size = (
        sum(int(row["total_skills"]) for row in skill_rows) / len(skill_rows)
        if skill_rows else 0.0
    )
    traj_total, traj_with_steps = trajectory_summary(output_dir, task_rows)

    summary = {
        "overall_success_rate": overall_sr,
        "successful_tasks": overall_successes,
        "recorded_tasks": overall_total,
        "expected_tasks": 16,
        "average_skill_library_size": avg_library_size,
        "families": family_rows,
    }
    (output_dir / "family_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# subset_setting1_v3_report",
        "",
        "## 实验配置",
        "",
        f"- families: {', '.join(families)}",
        "- tasks: 2 families x 8 tasks = 16 tasks",
        "- workers: u0",
        f"- rounds: {config.get('rounds')}",
        f"- model: {config.get('workers', [{}])[0].get('backbone_model')}",
        f"- harness: {config.get('workers', [{}])[0].get('agent_harness')}",
        f"- seed: {config.get('seed')}",
        "- protocol: FederatedSkill Setting1 Self-Evolution baseline",
        "",
        "## 核心指标",
        "",
        f"- Overall Success Rate: {overall_sr:.4f} ({overall_successes}/{overall_total})",
        f"- Average Skill Library Size: {avg_library_size:.4f}",
        f"- Trajectory files with steps: {traj_with_steps}/{traj_total}",
        "",
        "## Family Success Rate",
        "",
        "| family | recorded/expected | successful | success_rate | complete |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in family_rows:
        lines.append(
            f"| {row['family']} | {row['tasks_recorded']}/{row['expected_tasks']} | "
            f"{row['successful_tasks']} | {float(row['success_rate']):.4f} | {row['complete']} |"
        )
    lines.extend([
        "",
        "## 与论文对应关系",
        "",
        "当前实验对应 FederatedSkill Experimental Setting 1 / Self-Evolution baseline。",
        "它验证 self-evolution 下的 agent task solving、trajectory retention、patch distillation、local skill library update 与 skill accumulation trend。",
        "它不验证 Federated aggregation improvement，也不包含 Setting2 的 server Stage1 planning 或 Stage2 personalized merge。",
        "",
        "## 生成文件",
        "",
        "- task_level_results.csv",
        "- family_results.csv",
        "- skill_growth.csv",
        "- family_summary.json",
        "- subset_setting1_v3_report.md",
        "",
    ])
    (output_dir / "subset_setting1_v3_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"exported={output_dir.relative_to(ROOT)}")
    print(f"overall_success_rate={overall_sr:.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "experiments" / "configs" / "setting_v3.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    config_path = args.config if args.config.is_absolute() else ROOT / args.config
    if args.dry_run:
        return dry_run(config_path)
    if args.export:
        return export(config_path)
    parser.error("choose --dry-run or --export")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())