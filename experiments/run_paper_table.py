"""
run_paper_table.py — 一键跑通 Setting 1-4 并生成论文对照表格（Task3）

不重复实现任何已有逻辑，只是把已经跑通、已测试的两块拼在一起：
  1. experiments/runner.py::ExperimentRunner.for_setting(1..4) — 依次执行
     Setting 1（Self-Evolve）/ 2（Homogeneous Fed）/ 3（Heterogeneous Backbone）/
     4（Full Heterogeneity），复用 experiments/configs/ 下已有的 4 份配置。
  2. evaluation/results_exporter.py::ResultsExporter — 扫描各 setting 的
     round_*.json 产出，生成论文对照 CSV（success_rate / communication /
     privacy / skill_growth）与图表。

用法:
    # 跑全部 4 个 setting（每个 setting 用配置文件里定义的 round 数），再导出表格
    python experiments/run_paper_table.py

    # 只跑 Setting 1 和 3，且每个 setting 强制跑 2 轮（用于快速冒烟测试）
    python experiments/run_paper_table.py --settings 1,3 --rounds 2

    # 跳过实际执行，只对 results/ 目录下已有的 JSON 重新生成表格/图表
    python experiments/run_paper_table.py --skip-run

    # 干跑（只做配置解析 + 组件构建检查，不真正调用 LLM/跑任务）
    python experiments/run_paper_table.py --dry-run

输出:
    results/<setting_name>/round_*.json   （各 setting 原始产出，沿用既有约定）
    results/tables/success_rate.csv        Table 1 / Figure 2
    results/tables/communication.csv       Appendix C / Table 6
    results/tables/privacy.csv             Appendix E / Table 8
    results/tables/skill_growth.csv        Figure 3
    results/figures/*.png                  （matplotlib 已安装时）

⚠ 说明：某一个 setting 执行失败（如没有配置真实 LLM API key）不会中断整体
   流程——脚本会记录该 setting 的失败原因、继续跑剩余 setting，最后仍然对
   已成功产出的部分生成表格，并在结尾汇总打印每个 setting 的成功/失败状态。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from experiments.runner import SETTING_CONFIG_MAP, ExperimentRunner
from evaluation.results_exporter import ResultsExporter

logger = logging.getLogger(__name__)


def _parse_settings(raw: str) -> list[int]:
    settings = [int(x.strip()) for x in raw.split(",") if x.strip()]
    unknown = [s for s in settings if s not in SETTING_CONFIG_MAP]
    if unknown:
        raise ValueError(f"未知 setting 编号 {unknown}，仅支持 {sorted(SETTING_CONFIG_MAP)}")
    return settings


def run_all_settings(
    settings: list[int], rounds: int | None, dry_run: bool,
) -> dict[int, tuple[bool, str]]:
    """
    依次跑完给定的 setting 列表。

    Returns:
        {setting_num: (success, message)}，某个 setting 抛异常时 success=False，
        message 记录异常信息，不会中断后续 setting 的执行。
    """
    status: dict[int, tuple[bool, str]] = {}
    for num in settings:
        config_name = SETTING_CONFIG_MAP[num]
        print(f"\n{'=' * 60}\nSetting {num} ({config_name})\n{'=' * 60}")
        try:
            runner = ExperimentRunner.for_setting(num, rounds=rounds)
            result = runner.run(dry_run=dry_run)
            if dry_run:
                status[num] = (True, "dry_run OK")
            else:
                status[num] = (True, f"完成，rounds={getattr(result, 'total_rounds', '?')}")
        except Exception as exc:  # noqa: BLE001 — 单个 setting 失败不应中断整体流程
            logger.exception("Setting %d 执行失败", num)
            status[num] = (False, f"{type(exc).__name__}: {exc}")
    return status


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run_paper_table",
        description="依次跑通 Setting 1-4 并生成论文对照 CSV 表格/图表",
    )
    parser.add_argument(
        "--settings", default="1,2,3,4",
        help="要运行的 setting 编号列表，逗号分隔，默认 1,2,3,4",
    )
    parser.add_argument(
        "--rounds", type=int, default=None,
        help="覆盖每个 setting 配置文件里的 round 数（默认使用各配置自带值）",
    )
    parser.add_argument(
        "--results-dir", type=Path, default=_REPO_ROOT / "results",
        help="结果根目录（各 setting 输出到 <results-dir>/<setting_name>/），默认 results/",
    )
    parser.add_argument(
        "--skip-run", action="store_true", default=False,
        help="跳过实际执行 Setting 1-4，只对 results-dir 下已有的 JSON 重新生成表格",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="只做配置解析 + 组件构建检查，不真正执行任务/调用 LLM",
    )
    parser.add_argument(
        "--no-figures", action="store_true", default=False,
        help="只生成 CSV，不生成 matplotlib 图表",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = _parse_settings(args.settings)

    if not args.skip_run:
        status = run_all_settings(settings, rounds=args.rounds, dry_run=args.dry_run)
        print(f"\n{'=' * 60}\nSetting 执行汇总\n{'=' * 60}")
        for num in settings:
            ok, msg = status[num]
            flag = "OK  " if ok else "FAIL"
            print(f"  [{flag}] Setting {num} ({SETTING_CONFIG_MAP[num]}): {msg}")
    else:
        print("跳过实际执行（--skip-run），直接使用 results-dir 下已有的 JSON 产出。")

    if args.dry_run:
        print("\n--dry-run 模式：不生成表格/图表（没有真实 round JSON 产出）。")
        return

    results_dir = args.results_dir.resolve()
    output_dir = results_dir / "tables"
    exporter = ResultsExporter(results_dir=results_dir, output_dir=output_dir)

    print(f"\n扫描: {results_dir}")
    print(f"输出: {output_dir}")

    if args.no_figures:
        records = exporter._load_all_records()
        if not records:
            print("[警告] 未找到任何 round JSON 文件，无法生成表格。")
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        csv_files = [
            exporter._write_success_rate_csv(records),
            exporter._write_communication_csv(records),
            exporter._write_privacy_csv(records),
            exporter._write_skill_growth_csv(records),
        ]
        print(f"\n已生成 CSV ({len([f for f in csv_files if f])} 个):")
        for f in csv_files:
            if f:
                print(f"  {f}")
    else:
        summary = exporter.export_all()
        print(f"\n导出完成: settings={summary.settings_found} total_rounds={summary.total_rounds}")
        if summary.csv_files:
            print("\nCSV 表格（论文对照）:")
            print("  success_rate.csv    -> Table 1 / Figure 2")
            print("  communication.csv   -> Appendix C / Table 6")
            print("  privacy.csv         -> Appendix E / Table 8")
            print("  skill_growth.csv    -> Figure 3")
            for f in summary.csv_files:
                print(f"  {f}")
        if summary.figure_files:
            print("\n图表:")
            for f in summary.figure_files:
                print(f"  {f}")
        elif not args.no_figures:
            print("\n[提示] matplotlib 未安装或无产出，跳过图表。安装: pip install matplotlib")


if __name__ == "__main__":
    main()
