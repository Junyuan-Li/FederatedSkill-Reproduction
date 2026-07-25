"""
generate_results.py — 从实验输出 JSON 生成论文级表格与图表

读取 results/ 或 logs/ 目录下由 run_experiment.py 产生的 JSON 文件，
生成标准化 CSV 表格 + matplotlib 论文图表。

用法:
    python experiments/generate_results.py --results results/
    python experiments/generate_results.py --results logs/ --output results/tables/
    python experiments/generate_results.py --results results/ --no-figures

输出结构:
    results/tables/
        success_rate.csv      Table 1 / Figure 2
        communication.csv     Appendix C / Table 6
        privacy.csv           Appendix E / Table 8
        skill_growth.csv      Figure 3
    results/figures/
        figure_success_curve.png
        figure_skill_growth.png
        figure_compression.png
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluation.results_exporter import ResultsExporter


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="generate_results",
        description="从实验 JSON 生成论文级 CSV 表格和图表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从 results/ 目录生成所有表格和图表
  python experiments/generate_results.py --results results/

  # 指定输出目录
  python experiments/generate_results.py --results results/ --output paper_output/

  # 仅生成 CSV，不生成图表
  python experiments/generate_results.py --results results/ --no-figures

论文指标对应:
  success_rate.csv   → Table 1 / Figure 2
  communication.csv  → Appendix C / Table 6  (CR = 1 - |patch| / |traj|)
  privacy.csv        → Appendix E / Table 8  (SELR)
  skill_growth.csv   → Figure 3
""",
    )
    parser.add_argument(
        "--results", required=True, type=Path,
        help="实验结果根目录（含各 setting 子目录）",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="CSV 和图表输出目录（默认：<results>/tables/）",
    )
    parser.add_argument(
        "--no-figures", action="store_true", default=False,
        help="跳过图表生成（仅输出 CSV）",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    results_dir = args.results.resolve()
    if not results_dir.exists():
        print(f"[错误] 目录不存在: {results_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output.resolve() if args.output else results_dir / "tables"
    exporter = ResultsExporter(results_dir=results_dir, output_dir=output_dir)

    print(f"扫描: {results_dir}")
    print(f"输出: {output_dir}")

    if args.no_figures:
        # 仅导出 CSV
        records = exporter._load_all_records()
        if not records:
            print("[警告] 未找到任何 round JSON 文件")
            sys.exit(0)
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
        print(f"\n导出完成:")
        print(f"  Settings: {summary.settings_found}")
        print(f"  Total rounds: {summary.total_rounds}")
        if summary.csv_files:
            print(f"\nCSV 表格:")
            for f in summary.csv_files:
                print(f"  {f}")
        if summary.figure_files:
            print(f"\n图表:")
            for f in summary.figure_files:
                print(f"  {f}")
        elif not args.no_figures:
            print("\n[提示] matplotlib 未安装，跳过图表。安装: pip install matplotlib")


if __name__ == "__main__":
    main()
