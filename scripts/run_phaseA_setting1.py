"""运行唯一获准的 Phase A：Setting1 + 一个完整官方 family。"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark.family import load_family  # noqa: E402
from evaluation.paper_export import export_setting_csvs  # noqa: E402
from experiments.run_experiment import run_experiment  # noqa: E402
from harness.cli_utils import check_cli_binary  # noqa: E402

FAMILY_ID = "Compensation-Scenario-Modeling"
OUTPUT_DIR = REPO_ROOT / "results" / "phaseA_setting1"
RUN_LABEL = "PhaseA_Setting1_Self_Evolution"
ANALYSIS_KEY = "phaseA_analysis"
BASE_CONFIG = REPO_ROOT / "experiments" / "configs" / "setting_se.yaml"
FAMILY_PATH = REPO_ROOT / "benchmark" / "families" / f"{FAMILY_ID}.json"
SKILL_EVOLUTION_FIELDS = [
    "family_id", "round_idx", "worker_id", "added_skills", "edited_skills",
    "deleted_skills", "total_skills",
]
TASK_RESULT_FIELDS = [
    "family_id", "task_id", "round_idx", "worker_id", "status", "reward",
]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_empty_output() -> None:
    if OUTPUT_DIR.exists() and any(OUTPUT_DIR.iterdir()):
        raise RuntimeError(f"拒绝覆盖非空 Phase A 结果目录: {OUTPUT_DIR}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _build_effective_config() -> Path:
    config = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8")) or {}
    if config.get("federated") is not False or len(config.get("workers", [])) != 1:
        raise RuntimeError("Phase A 要求 Setting1 非联邦且恰好一个 worker")
    if config.get("max_retry") != 0:
        raise RuntimeError("Phase A 要求 max_retry=0")
    config["setting_name"] = RUN_LABEL
    config["family_subset"] = [FAMILY_ID]
    config["rounds_per_family_mode"] = "family_length"
    config["output_dir"] = str(OUTPUT_DIR)
    path = OUTPUT_DIR / "effective_config.yaml"
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _preflight() -> None:
    family = load_family(FAMILY_PATH)
    sequence = family.get_sequence()
    if len(sequence) != 8 or [task.difficulty for task in sequence] != list(range(1, 9)):
        raise RuntimeError("官方 Compensation family 不是完整的 8-task ranking 序列")
    for task in sequence:
        source = Path(task.metadata.get("source_environment_dir", ""))
        if not source.is_dir():
            raise RuntimeError(f"缺少官方 environment 源目录: {task.task_id}: {source}")
    config = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8")) or {}
    worker = config["workers"][0]
    key_name = worker["api_key_env"]
    if not os.environ.get(key_name):
        raise RuntimeError(f"缺少 API 环境变量: {key_name}")
    check_cli_binary("claude")


def _task_checkpoint_dirs(family_dir: Path) -> list[Path]:
    task_dirs = list((family_dir / "workers").glob("*/tasks/round_*"))
    return sorted(task_dirs, key=lambda path: int(path.name.split("_", 2)[1]))


def _write_trajectory_jsonl(family_dir: Path) -> tuple[Path, list[dict[str, Any]]]:
    output_path = OUTPUT_DIR / "trajectory.jsonl"
    records: list[dict[str, Any]] = []
    with output_path.open("w", encoding="utf-8") as stream:
        for task_dir in _task_checkpoint_dirs(family_dir):
            status = _read_json(task_dir / "task_status.json")
            trajectory_path = task_dir / "trajectory.json"
            if trajectory_path.is_file():
                record = _read_json(trajectory_path)
            else:
                record = {
                    "task_name": status["task_id"],
                    "worker_id": status["worker_id"],
                    "round_idx": status["round_idx"],
                    "reward": 0.0,
                    "actions": [],
                    "exception_info": {"message": status.get("failure_reason", "")},
                }
            record["family_id"] = FAMILY_ID
            record["checkpoint_status"] = status["status"]
            records.append(record)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return output_path, records


def _write_skill_evolution(family_dir: Path) -> Path:
    alive_paths: set[str] = set()
    rows: list[dict[str, Any]] = []
    for task_dir in _task_checkpoint_dirs(family_dir):
        status = _read_json(task_dir / "task_status.json")
        patch_path = task_dir / "patch.json"
        patch = _read_json(patch_path) if patch_path.is_file() else {}
        added = edited = deleted = 0
        for skill_path in patch.get("upserts", {}):
            if skill_path in alive_paths:
                edited += 1
            else:
                alive_paths.add(skill_path)
                added += 1
        for skill_path in patch.get("deletions", []):
            if skill_path in alive_paths:
                alive_paths.remove(skill_path)
                deleted += 1
        rows.append({
            "family_id": FAMILY_ID,
            "round_idx": status["round_idx"],
            "worker_id": status["worker_id"],
            "added_skills": added,
            "edited_skills": edited,
            "deleted_skills": deleted,
            "total_skills": len(alive_paths),
        })
    path = OUTPUT_DIR / "skill_evolution.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SKILL_EVOLUTION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_task_results(family_dir: Path) -> Path:
    """把每个 task checkpoint 汇总为验证要求的逐任务结果表。"""
    rows: list[dict[str, Any]] = []
    for task_dir in _task_checkpoint_dirs(family_dir):
        status = _read_json(task_dir / "task_status.json")
        rows.append({
            "family_id": FAMILY_ID,
            "task_id": status["task_id"],
            "round_idx": status["round_idx"],
            "worker_id": status["worker_id"],
            "status": status["status"],
            "reward": status.get("reward", 0.0),
        })
    path = OUTPUT_DIR / "task_result.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TASK_RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_workspace_manifest(family_dir: Path) -> Path:
    """聚合 task-level manifest，保留各任务完整 manifest 的相对路径。"""
    task_manifests: list[dict[str, Any]] = []
    task_root = family_dir / "tasks"
    for manifest_path in sorted(task_root.rglob("workspace_manifest.json")):
        manifest = _read_json(manifest_path)
        manifest["manifest_path"] = str(
            manifest_path.relative_to(OUTPUT_DIR)
        ).replace("\\", "/")
        task_manifests.append(manifest)
    payload = {
        "family_id": FAMILY_ID,
        "task_count": len(task_manifests),
        "all_workspaces_fresh": bool(task_manifests) and all(
            item.get("workspace_was_fresh") is True for item in task_manifests
        ),
        "all_temporary_workspaces_deleted": bool(task_manifests) and all(
            item.get("temporary_workspace_deleted_after_collection") is True
            for item in task_manifests
        ),
        "tasks": task_manifests,
    }
    path = OUTPUT_DIR / "workspace_manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_summary(
    trajectories: list[dict[str, Any]], family_dir: Path
) -> Path:
    summary = _read_json(OUTPUT_DIR / "experiment_summary.json")
    rounds = [
        _read_json(path)
        for path in sorted(family_dir.glob("round_*_summary.json"))
    ]
    reuse_by_round = {
        str(record.get("round_idx")): next(
            (
                action.get("skill_paths", [])
                for action in record.get("actions", [])
                if action.get("type") == "skill_retrieval"
            ),
            [],
        )
        for record in trajectories
    }
    rewards = [
        float(round_record.get("snapshots", [{}])[0].get("reward", 0.0))
        for round_record in rounds
    ]
    evolution_rows = list(csv.DictReader(
        (OUTPUT_DIR / "skill_evolution.csv").open(encoding="utf-8-sig")
    ))
    final_skill_count = int(evolution_rows[-1]["total_skills"]) if evolution_rows else 0
    summary[ANALYSIS_KEY] = {
        "family_id": FAMILY_ID,
        "complete_family_sequence": len(rounds) == 8,
        "task_ids_in_order": [
            round_record.get("snapshots", [{}])[0].get("task_id")
            for round_record in rounds
        ],
        "skill_library_grew": final_skill_count > 0,
        "final_skill_count": final_skill_count,
        "retrieved_skill_paths_by_round": reuse_by_round,
        "later_tasks_reused_skills": any(
            paths for round_idx, paths in reuse_by_round.items() if int(round_idx) > 0
        ),
        "rewards_by_round": rewards,
        "success_trend_scope": "single-family validation; not a full-paper estimate",
    }
    path = OUTPUT_DIR / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    _assert_empty_output()
    try:
        _preflight()
        config_path = _build_effective_config()
        result = run_experiment(
            config_path=config_path,
            output_dir_override=OUTPUT_DIR,
            execution_mode="cli",
            distillation_failure_mode="strict",
        )
        if result is None:
            raise RuntimeError("Phase A 未返回实验结果")
        export_setting_csvs(OUTPUT_DIR)
        family_dir = OUTPUT_DIR / "families" / FAMILY_ID
        _, trajectories = _write_trajectory_jsonl(family_dir)
        _write_skill_evolution(family_dir)
        _write_task_results(family_dir)
        _write_workspace_manifest(family_dir)
        _write_summary(trajectories, family_dir)
    except Exception:
        if OUTPUT_DIR.exists() and not any(OUTPUT_DIR.glob("families/*/workers/*/tasks/*")):
            shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
        raise
    print(f"Phase A 完成: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())