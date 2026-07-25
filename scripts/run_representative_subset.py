"""严格按 Setting1 -> Setting2 顺序运行代表性完整-family 子集。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation.paper_export import export_family_loop_csvs  # noqa: E402
from experiments.run_experiment import run_experiment  # noqa: E402
from validate_subset_protocol import validate as validate_subset_protocol  # noqa: E402

SETTING1_CONFIG = REPO_ROOT / "experiments" / "configs" / "subset_setting1_self_evolution.yaml"
SETTING2_CONFIG = REPO_ROOT / "experiments" / "configs" / "subset_setting2_homogeneous_federation.yaml"
SETTING1_OUTPUT = REPO_ROOT / "results" / "subset_setting1_self_evolution"
SETTING2_OUTPUT = REPO_ROOT / "results" / "subset_setting2_homogeneous_federation"


def _require_empty(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise RuntimeError(f"拒绝覆盖非空实验目录: {path}")


def _run(config: Path, output: Path, *, dry_run: bool = False) -> None:
    if dry_run:
        run_experiment(
            config_path=config,
            output_dir_override=output,
            execution_mode="cli",
            distillation_failure_mode="strict",
            dry_run=True,
        )
        return
    _require_empty(output)
    result = run_experiment(
        config_path=config,
        output_dir_override=output,
        execution_mode="cli",
        distillation_failure_mode="strict",
    )
    if result is None:
        raise RuntimeError(f"实验未返回结果: {config}")
    export_family_loop_csvs(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--setting",
        choices=("setting1", "setting2", "both"),
        default="both",
        help="默认严格依次运行 Setting1 和 Setting2；可用于中断后单独续跑 Setting2。",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="验证两份配置与执行计划，不调用 CLI/API、不写实验结果。",
    )
    args = parser.parse_args()

    validate_subset_protocol()

    if args.setting in ("setting1", "both"):
        _run(SETTING1_CONFIG, SETTING1_OUTPUT, dry_run=args.dry_run)
    if args.setting in ("setting2", "both"):
        if (
            not args.dry_run
            and args.setting == "both"
            and not (SETTING1_OUTPUT / "experiment_summary.json").is_file()
        ):
            raise RuntimeError("Setting1 未完整结束，拒绝提前启动 Setting2")
        _run(SETTING2_CONFIG, SETTING2_OUTPUT, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())