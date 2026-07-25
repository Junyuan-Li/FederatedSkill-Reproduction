"""按官方 task metadata 确定性选择 Easy/Medium/Hard 完整 family。"""

from __future__ import annotations

import json
import statistics
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = REPO_ROOT / "benchmark" / "cache" / "SkillFlow-Task" / "test_tasks"
REPORT_PATH = REPO_ROOT / "family_selection_report.md"
SELECTION_PATH = REPO_ROOT / "experiments" / "configs" / "representative_subset.yaml"

DIFFICULTY_SCORE = {
    "easy": 1.0,
    "medium": 2.0,
    "medium-hard": 2.5,
    "hard": 3.0,
    "expert": 4.0,
}


@dataclass(frozen=True)
class FamilyMetadata:
    family_id: str
    task_ids: tuple[str, ...]
    difficulty_labels: tuple[str, ...]
    mean_difficulty: float
    unranked_task_ids: tuple[str, ...] = ()
    missing_ranked_task_ids: tuple[str, ...] = ()

    @property
    def counts(self) -> Counter[str]:
        return Counter(self.difficulty_labels)


def _ordered_task_dirs(family_dir: Path) -> tuple[list[Path], list[str], list[str]]:
    ranking_path = family_dir / "ALL_TASK_DIFFICULTY_RANKING.json"
    if not ranking_path.is_file():
        raise RuntimeError(f"缺少官方 ranking: {ranking_path}")
    ranking = json.loads(ranking_path.read_text(encoding="utf-8"))
    if not isinstance(ranking, list) or not all(isinstance(item, str) for item in ranking):
        raise RuntimeError(f"无效官方 ranking: {ranking_path}")
    if len(ranking) != len(set(ranking)):
        raise RuntimeError(f"官方 ranking 含重复 task: {ranking_path}")
    discovered = {
        path.name for path in family_dir.iterdir()
        if path.is_dir() and (path / "task.toml").is_file()
    }
    missing = [task_id for task_id in ranking if task_id not in discovered]
    ranked_dirs = [family_dir / task_id for task_id in ranking if task_id in discovered]
    unranked = sorted(discovered - set(ranking))
    task_dirs = ranked_dirs + [family_dir / task_id for task_id in unranked]
    return task_dirs, unranked, missing


def load_metadata() -> list[FamilyMetadata]:
    families: list[FamilyMetadata] = []
    for family_dir in sorted(path for path in DATASET_ROOT.iterdir() if path.is_dir()):
        task_dirs, unranked, missing = _ordered_task_dirs(family_dir)
        labels: list[str] = []
        for task_dir in task_dirs:
            raw = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
            label = str((raw.get("metadata") or {}).get("difficulty", "")).strip().lower()
            if label not in DIFFICULTY_SCORE:
                raise RuntimeError(
                    f"未知 difficulty={label!r}: {task_dir / 'task.toml'}"
                )
            labels.append(label)
        families.append(FamilyMetadata(
            family_id=family_dir.name,
            task_ids=tuple(path.name for path in task_dirs),
            difficulty_labels=tuple(labels),
            mean_difficulty=sum(DIFFICULTY_SCORE[label] for label in labels) / len(labels),
            unranked_task_ids=tuple(unranked),
            missing_ranked_task_ids=tuple(missing),
        ))
    task_count = sum(len(family.task_ids) for family in families)
    if len(families) != 20 or task_count != 166:
        raise RuntimeError(
            f"官方 benchmark 完整性失败: families={len(families)} tasks={task_count}"
        )
    return families


def select_subset(families: list[FamilyMetadata]) -> dict[str, FamilyMetadata]:
    ordered = sorted(families, key=lambda item: (item.mean_difficulty, item.family_id))
    easy = ordered[0]
    hard = sorted(families, key=lambda item: (-item.mean_difficulty, item.family_id))[0]
    median_score = statistics.median(item.mean_difficulty for item in families)
    medium = min(
        (item for item in families if item.family_id not in {easy.family_id, hard.family_id}),
        key=lambda item: (abs(item.mean_difficulty - median_score), item.family_id),
    )
    return {"easy": easy, "medium": medium, "hard": hard}


def _counts_text(counts: Counter[str]) -> str:
    return ", ".join(
        f"{label}={counts[label]}" for label in DIFFICULTY_SCORE if counts[label]
    )


def write_outputs(
    families: list[FamilyMetadata], selected: dict[str, FamilyMetadata]
) -> None:
    selection = {
        "selection_method": "official_task_metadata_difficulty_medoids",
        "full_benchmark": {
            "family_count": len(families),
            "task_count": sum(len(item.task_ids) for item in families),
            "unranked_tasks_appended_by_loader_rule": {
                item.family_id: list(item.unranked_task_ids)
                for item in families if item.unranked_task_ids
            },
            "missing_ranking_references_skipped_by_loader_rule": {
                item.family_id: list(item.missing_ranked_task_ids)
                for item in families if item.missing_ranked_task_ids
            },
        },
        "family_subset": [selected[tier].family_id for tier in ("easy", "medium", "hard")],
        "tiers": {
            tier: {
                "family_id": family.family_id,
                "task_count": len(family.task_ids),
                "mean_difficulty": round(family.mean_difficulty, 4),
                "task_ids_in_official_order": list(family.task_ids),
                "difficulty_labels_in_official_order": list(family.difficulty_labels),
            }
            for tier, family in selected.items()
        },
    }
    SELECTION_PATH.write_text(
        yaml.safe_dump(selection, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    rows = []
    selected_ids = {item.family_id for item in selected.values()}
    for family in sorted(families, key=lambda item: (item.mean_difficulty, item.family_id)):
        rows.append(
            f"| {family.family_id} | {len(family.task_ids)} | "
            f"{family.mean_difficulty:.3f} | {_counts_text(family.counts)} | "
            f"{'是' if family.family_id in selected_ids else '否'} |"
        )
    selected_rows = []
    for tier in ("easy", "medium", "hard"):
        family = selected[tier]
        selected_rows.append(
            f"| {tier.title()} | `{family.family_id}` | {len(family.task_ids)} | "
            f"{family.mean_difficulty:.3f} | {_counts_text(family.counts)} |"
        )

    report = f"""# FederatedSkill Representative Family Selection Report

## Scope

This is a **representative subset reproduction**, not a full benchmark reproduction.
The full SkillFlow benchmark contains **20 families and 166 sequential tasks**. The
subset reduces only the number of families; every selected family retains every task,
the official `ALL_TASK_DIFFICULTY_RANKING.json` order, and the complete difficulty
sequence.

## Deterministic Selection Rule

The selector reads `[metadata].difficulty` from every official `task.toml` and maps
the ordered labels as `easy=1`, `medium=2`, `medium-hard=2.5`, `hard=3`, and
`expert=4`. A family score is the arithmetic mean across all tasks in that complete
family.

- **Easy**: lowest family mean; ties are resolved by family ID.
- **Medium**: family closest to the median of all 20 family means, excluding the
  already selected endpoints; ties are resolved by family ID.
- **Hard**: highest family mean; ties are resolved by family ID.

This is metadata-driven and reproducible. It does not sample tasks, inspect model
outcomes, or choose families based on expected success.

Task order uses the repository's existing official-loader rule: listed tasks follow
`ALL_TASK_DIFFICULTY_RANKING.json`; any task directory omitted by that file is retained
and appended by task ID. This preserves all 166 tasks rather than silently deleting
unranked dataset entries. Ranking references absent from the downloaded dataset are
skipped, matching the loader. The machine-readable selection file records every append
and missing reference.

## Selected Families

| Tier | Family | Tasks | Mean score | Official difficulty labels |
|---|---|---:|---:|---|
{chr(10).join(selected_rows)}

Selected task count: **{sum(len(item.task_ids) for item in selected.values())}**.

## Full Metadata Inventory

| Family | Tasks | Mean score | Difficulty counts | Selected |
|---|---:|---:|---|---|
{chr(10).join(rows)}

## Representativeness and Limits

The subset spans the observed lower endpoint, central tendency, and upper endpoint
of official task-level difficulty metadata. It therefore tests whether sequential
skill evolution and federation trends persist across three distinct difficulty
regimes while preserving the paper's within-family learning process.

It does **not** estimate the full-benchmark aggregate with the same statistical
coverage as all 20 families. Domain coverage, rare tool dependencies, and the exact
paper-wide success rate remain outside this subset. Full reproduction remains the
20-family, 166-task extension.

## Protocol Guardrails

- No task sampling.
- No task removed from a selected family.
- No task order or difficulty sequence changed.
- Every family starts with an empty skill library.
- Setting1 and Setting2 must use the same selected family list.
- Setting3 and Setting4 are not part of this phase.
"""
    REPORT_PATH.write_text(report, encoding="utf-8")


def main() -> int:
    families = load_metadata()
    selected = select_subset(families)
    write_outputs(families, selected)
    print(json.dumps({tier: item.family_id for tier, item in selected.items()}, indent=2))
    print(f"report={REPORT_PATH}")
    print(f"selection={SELECTION_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())