"""
run.py — 论文实验统一入口（Phase14 任务5）

用法::

    python run.py --setting 1                  # 跑 Setting1 并导出该 setting 的四张 CSV
    python run.py --setting 2 --rounds 2        # 覆盖轮数（快速验证用）
    python run.py --setting 3 --dry-run         # 只做配置解析/组件构建检查，不真正执行

--setting 支持 1/2/3/4，分别对应论文四种实验设置：
    1 = Self-Evolve            2 = Homogeneous Federated
    3 = Heterogeneous Backbone 4 = Full Heterogeneity

--execution-mode 默认值按场景推导（不设单一全局默认，避免"配置声明 strict
但实际跑的是 api"这类复现争议）：
    - 真实实验（未加 --mock/--dry-run）：默认 "cli"（论文 strict
      reproduction mode，真实 spawn claude/qwen-code/kimi CLI 二进制）。
    - --mock/--dry-run（开发/结构验证场景）：默认 "api"（LLM API 直连，
      不需要本机装 CLI 二进制，方便测试）。
    - 任何场景都可用 --execution-mode {api,cli} 显式覆盖上面的推导结果。

不重复实现：
  - 跑实验复用已测试的 experiments/runner.py::ExperimentRunner（门面类，
    内部委托 experiments/run_experiment.py::run_experiment()，本文件不
    重新实现任何 Algorithm 1 流程逻辑）；
  - 导出 CSV 复用 evaluation/paper_export.py::export_setting_csvs()
    （Phase14 任务4新增，本文件不重复实现 CSV 解析/写出逻辑）。

输出目录固定为 <repo_root>/results/setting<N>/，与 Phase14 任务4要求的
目录结构一致；若该目录下已有 scaffold_all_settings() 生成的占位 CSV，
真实运行后会被本次实验数据覆盖。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluation.paper_export import export_setting_csvs  # noqa: E402
from experiments.runner import ExperimentRunner  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py", description="按论文 Setting 1-4 运行实验并导出 Figure/Table 所需 CSV",
    )
    parser.add_argument("--setting", type=int, required=True, choices=[1, 2, 3, 4], help="论文 Setting 编号（1-4）")
    parser.add_argument("--rounds", type=int, default=None, help="覆盖配置文件里的轮数（默认使用配置文件自带值）")
    parser.add_argument("--dry-run", action="store_true", default=False, help="只做配置解析/组件构建检查，不真正执行任务、不导出 CSV")
    parser.add_argument("--mock", action="store_true", default=False, help="使用 mock backbone 真实跑完整流程（不需要 API Key，不发出真实请求），用于验证 family 循环结构，不导出 CSV")
    parser.add_argument(
        "--mock-federated", action="store_true", default=False,
        help=(
            "\"FederatedSkill Faithful Mock Validation\" TASK1 新增：比 --mock 更\"忠实\"的 "
            "mock backbone，PatchDistiller/Stage1/Stage2 三个调用方都返回非空、schema 合法的 "
            "payload，用于验证 skill evolution 全链路（技能库/capability_matrix/directive/"
            "transfer 均非空）。与 --mock 一样不需要 API Key、不导出 CSV。"
        ),
    )
    parser.add_argument(
        "--execution-mode", choices=["api", "cli"], default=None,
        help=(
            "api=LLM API 直连+自建 AgentWorkspaceExecutor；"
            "cli=按 worker 的 agent_harness 真实 spawn claude/qwen-code/kimi CLI"
            "（先用 scripts/check_cli_harness.py 确认本机已安装对应 CLI）。"
            "不显式指定时的默认值取决于运行场景（见下）："
            "真实实验（未加 --mock/--dry-run）默认 cli——论文默认即 strict "
            "reproduction mode，不能让'忘记加参数'悄悄退化成 api 而制造复现争议；"
            "--mock/--dry-run（开发/结构验证场景）默认 api，方便测试。"
        ),
    )
    args = parser.parse_args(argv)

    if args.execution_mode is not None:
        execution_mode = args.execution_mode
    elif args.mock or args.mock_federated or args.dry_run:
        # 开发/结构验证场景：mock backbone 只 stub LLMBackbone，不会 stub 真实
        # CLI subprocess，因此 --mock/--dry-run 默认走 api，避免在没装 CLI
        # 二进制的开发机上意外报 CLIBinaryNotFoundError。
        execution_mode = "api"
    else:
        # 真实实验（论文复现）场景：默认必须是 cli（strict reproduction
        # mode），不允许"忘记加 --execution-mode"就悄悄退化成 api 模拟执行。
        execution_mode = "cli"
    print(f"[run.py] execution_mode = {execution_mode}" + ("（未显式指定，按场景推导）" if args.execution_mode is None else "（显式指定）"))

    output_dir = _REPO_ROOT / "results" / f"setting{args.setting}"
    runner = ExperimentRunner.for_setting(
        args.setting, rounds=args.rounds, output_dir=output_dir, execution_mode=execution_mode,
    )

    print(f"[run.py] Setting {args.setting} -> {output_dir}")
    result = runner.run(dry_run=args.dry_run, mock=args.mock, mock_federated=args.mock_federated)

    if args.dry_run:
        print("[run.py] dry-run 完成：未执行真实任务，不导出 CSV。")
        return 0
    if args.mock or args.mock_federated:
        print("[run.py] mock 完成：仅验证 family 循环/round 单调等结构，数据为假数据，不导出 CSV。")
        return 0

    csv_paths = export_setting_csvs(output_dir)
    print("[run.py] 已导出（论文 Figure/Table 所需数据）:")
    for name, path in csv_paths.items():
        print(f"  {name}.csv -> {path}")

    if result is not None:
        print(f"[run.py] 最终 success_rate = {result.final_success_rate:.3f}，共 {len(result.rounds)} 轮")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
