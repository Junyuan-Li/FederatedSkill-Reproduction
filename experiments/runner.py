"""
runner.py — ExperimentRunner：统一实验运行器门面（Phase13 任务3）

不重复实现：本类不复制 experiments/run_experiment.py 里任何 Algorithm 1
流程逻辑（配置解析 / worker 构建 / SelfEvolutionRunner vs FederatedRunner
分流 / 结果落盘），只是把已经跑通、已测试的 run_experiment() 函数包装成
一个具名为 ExperimentRunner 的类，满足：

    runner = ExperimentRunner.for_setting(1)   # Setting1: Self-Evolve
    result = runner.run(dry_run=True)

对应四种实验设置（复用 experiments/configs/ 下已有 4 个 setting 文件，
不新建重复的 setting 配置）：
    Setting 1 — Self-Evolve              → setting_se.yaml
    Setting 2 — Homogeneous Federated    → setting_homo_fed.yaml
    Setting 3 — Heterogeneous Backbone   → setting_hetero_backbone.yaml
    Setting 4 — Full Heterogeneity       → setting_full_hetero.yaml
"""

from __future__ import annotations

from pathlib import Path

from evaluation.evaluator import ExperimentResult
from experiments.run_experiment import (
    run_experiment,
    run_family_batch_experiments,
    run_family_experiment,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIGS_DIR = _REPO_ROOT / "experiments" / "configs"

#: Setting 编号 -> 配置文件名（均为已有文件，不新建）
SETTING_CONFIG_MAP: dict[int, str] = {
    1: "setting_se.yaml",
    2: "setting_homo_fed.yaml",
    3: "setting_hetero_backbone.yaml",
    4: "setting_full_hetero.yaml",
}


class ExperimentRunner:
    """
    统一实验运行器门面。

    职责仅有两项：
      1. 从 experiments/configs/ 按 setting 编号或路径解析出配置文件；
      2. 委托给 experiments.run_experiment.run_experiment() 执行/干跑。
    不承担配置解析、worker 构建、Runner 分流等职责——这些已在
    run_experiment() 中实现并被 _test_imports.py / pytest 覆盖。
    """

    def __init__(
        self,
        config_path: str | Path,
        rounds: int | None = None,
        output_dir: str | Path | None = None,
        execution_mode: str = "api",
    ) -> None:
        self.config_path = Path(config_path)
        if not self.config_path.is_absolute():
            self.config_path = (_REPO_ROOT / self.config_path).resolve()
        self.rounds = rounds
        self.output_dir = Path(output_dir) if output_dir else None
        # Real CLI Harness Fidelity Fix 新增："api"（默认，与新增之前行为一致）
        # 或 "cli"（真实 CLI subprocess，见 harness/ 包）。
        self.execution_mode = execution_mode

    @classmethod
    def for_setting(
        cls,
        setting_num: int,
        rounds: int | None = None,
        output_dir: str | Path | None = None,
        execution_mode: str = "api",
    ) -> "ExperimentRunner":
        """
        按论文 Setting 1-4 编号快速构造。

        Args:
            setting_num: 1=Self-Evolve, 2=Homogeneous Fed,
                         3=Heterogeneous Backbone, 4=Full Heterogeneity
            execution_mode: "api"（默认）或 "cli"（真实 Agent CLI Harness）。
        """
        if setting_num not in SETTING_CONFIG_MAP:
            raise ValueError(
                f"未知 setting_num={setting_num}，仅支持 {sorted(SETTING_CONFIG_MAP)}"
            )
        config_path = _CONFIGS_DIR / SETTING_CONFIG_MAP[setting_num]
        return cls(config_path=config_path, rounds=rounds, output_dir=output_dir, execution_mode=execution_mode)

    def run(
        self, dry_run: bool = False, plot: bool = False, mock: bool = False,
        mock_federated: bool = False,
    ) -> ExperimentResult | None:
        """
        执行（或干跑）本实验。

        Args:
            mock: 使用 mock backbone 真实跑完整流程（不需要 API Key，
                不发出任何真实 API 请求），用于验证 family 循环等结构。
            mock_federated: "FederatedSkill Faithful Mock Validation" TASK1
                新增。使用更"忠实"的 mock backbone（PatchDistiller/Stage1/
                Stage2 三个调用方都返回非空、schema 合法的 payload），
                用于验证 skill evolution 全链路。同样不发出任何真实 API
                请求、不需要 API Key。

        Returns:
            ExperimentResult；dry_run=True 时返回 None（与 run_experiment() 语义一致）。
        """
        return run_experiment(
            config_path=self.config_path,
            rounds_override=self.rounds,
            output_dir_override=self.output_dir,
            dry_run=dry_run,
            plot=plot,
            mock=mock,
            execution_mode=self.execution_mode,
            mock_federated=mock_federated,
        )

    def run_family(
        self,
        family_id: str,
        dry_run: bool = False,
        mock: bool = False,
        mock_federated: bool = False,
    ) -> ExperimentResult | None:
        """运行单个 family，复用 run_experiment.py 的独立 experiment_id 协议。"""
        return run_family_experiment(
            config_path=self.config_path,
            family_id=family_id,
            rounds_override=self.rounds,
            results_root_override=self.output_dir,
            dry_run=dry_run,
            mock=mock,
            execution_mode=self.execution_mode,
            mock_federated=mock_federated,
        )

    def run_families(
        self,
        family_ids: list[str],
        dry_run: bool = False,
        mock: bool = False,
        mock_federated: bool = False,
        continue_on_error: bool = True,
    ) -> dict[str, object]:
        """按列表顺序逐个独立运行 family，并返回 batch manifest。"""
        return run_family_batch_experiments(
            config_path=self.config_path,
            family_ids=family_ids,
            rounds_override=self.rounds,
            results_root_override=self.output_dir,
            dry_run=dry_run,
            mock=mock,
            execution_mode=self.execution_mode,
            mock_federated=mock_federated,
            continue_on_error=continue_on_error,
        )

    def __repr__(self) -> str:
        return f"ExperimentRunner(config={self.config_path.name}, rounds={self.rounds})"
