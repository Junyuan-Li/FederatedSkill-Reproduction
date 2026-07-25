"""
main_trainer.py — FederatedSkill 复现实验主入口

⚠️ LEGACY ENTRY POINT（Phase15 复现保真审计，Phase5）
    本文件是早期开发阶段的入口，其内部 `_build_sampler()` 是与
    `experiments/run_experiment.py::_build_sampler()` 平行的另一套实现
    （签名不同、异构兜底逻辑也不同），不是论文复现实验的官方入口。

    论文四个 Setting（1/2/3/4）的复现实验请统一使用：

        python run.py --setting 1|2|3|4 [--rounds N] [--dry-run]

    该入口会调用 `experiments/runner.py::ExperimentRunner`，最终落到
    `experiments/run_experiment.py::run_experiment()`（已修复 P0/P1，
    详见 docs/audit_report_v1.md、docs/reproduction_changes.md）。

    保留本文件不删除，仅作历史/调试用途，请勿在正式复现流程中依赖它。

用法::

    # Setting 4 全异构联邦（论文核心实验）
    python main_trainer.py --config experiments/configs/setting_full_hetero.yaml

    # Setting 1 SE 基线
    python main_trainer.py --config experiments/configs/setting_se.yaml

    # 同时运行 SE + 全异构，打印 Table 1 对比
    python main_trainer.py --compare \\
        --se-config experiments/configs/setting_se.yaml \\
        --fed-config experiments/configs/setting_full_hetero.yaml

    # Dry-run（验证配置，不调 LLM API）
    python main_trainer.py --config experiments/configs/setting_full_hetero.yaml --dry-run

参数说明::

    --config        单实验配置文件路径（.yaml）
    --rounds        覆盖配置文件中的 rounds
    --output-dir    覆盖配置文件中的 output_dir
    --dry-run       仅打印配置并退出，不执行任何 LLM 调用
    --compare       同时运行 SE + Fed 并打印对比表
    --se-config     compare 模式下 SE 配置路径
    --fed-config    compare 模式下 Federated 配置路径
    --log-level     日志级别（DEBUG/INFO/WARNING），默认 INFO
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from benchmark.loader import TaskLoader
from benchmark.family import load_all_families
from benchmark.curriculum import FamilyCurriculumSampler, SkillFlowFamilySampler
from benchmark.sampler import (
    DifficultyAwareSampler,
    HeterogeneousSampler,
    RandomSampler,
    TaskSampler,
)
from client.executor import TaskExecutor
from executor.router_executor import VerificationAwareExecutor
from client.federated_client import FederatedClient
from core.datatypes import WorkerProfile
from evaluation.evaluator import ExperimentEvaluator, ExperimentResult
from evaluation.reporter import ResultReporter
from experiments.baseline import SelfEvolutionRunner
from experiments.federated import FederatedRunner
from llm.backbone import LLMBackbone
from llm.router import BackboneRouter
from server.evolution import FederatedServer


_REPO_ROOT = Path(__file__).resolve().parent


def _load_project_dotenv(env_path: Path | None = None) -> bool:
    """Legacy 入口也加载仓库根目录 `.env`，与正式 run_experiment.py 保持一致。"""
    env_path = env_path or (_REPO_ROOT / ".env")
    if not env_path.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        logging.getLogger(__name__).warning(
            "发现 .env 但 python-dotenv 未安装，无法自动加载: %s", env_path,
        )
        return False
    return bool(load_dotenv(env_path, override=False))


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_yaml(path: str | Path) -> dict[str, Any]:
    """加载 YAML 配置文件，返回 dict。"""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_worker_profiles(
    worker_cfgs: list[dict[str, Any]],
) -> list[WorkerProfile]:
    """从 YAML workers 字段构建 WorkerProfile 列表。"""
    profiles = []
    for w in worker_cfgs:
        # 过滤掉非 WorkerProfile 字段（如 task_categories / family_id）
        profile_fields = {
            k: v for k, v in w.items()
            if k not in {"task_categories", "family_id"}
        }
        profiles.append(WorkerProfile(**profile_fields))
    return profiles


def _build_clients(
    worker_cfgs: list[dict[str, Any]],
    storage_root: Path,
) -> list[FederatedClient]:
    """为每个 worker 构建 FederatedClient，library 存储在 storage_root/{client_id}。"""
    clients = []
    for w in worker_cfgs:
        profile_fields = {k: v for k, v in w.items() if k not in {"task_categories", "family_id"}}
        profile = WorkerProfile(**profile_fields)
        library_root = storage_root / profile.client_id
        library_root.mkdir(parents=True, exist_ok=True)
        clients.append(FederatedClient(profile=profile, library_root=library_root))
    return clients


def _build_executor(profiles: list[WorkerProfile]) -> TaskExecutor:
    """
    根据 WorkerProfile 列表构建共用执行器。

    最终论文一致性收口 Priority 1：返回 VerificationAwareExecutor（对外接口与
    TaskExecutor 完全一致，duck typing 兼容），按 task.verification.type 分派到
    TaskExecutor 或 SkillFlowTaskExecutor，见 executor/router_executor.py docstring。
    """
    router = BackboneRouter.from_profiles(profiles)
    return VerificationAwareExecutor(router=router)


def _build_server(
    server_cfg: dict[str, Any],
    profiles: list[WorkerProfile],
) -> FederatedServer:
    """根据 server config 和 worker profiles 构建 FederatedServer。"""
    backbone = LLMBackbone(
        litellm_model=server_cfg["backbone_model"],
        api_key=os.environ.get(server_cfg["api_key_env"], ""),
        api_base=server_cfg["api_base"],
    )
    return FederatedServer.create(
        server_backbone=backbone,
        family_name=server_cfg.get("family_name", "default_family"),
        worker_profiles=profiles,
    )


def _build_sampler(
    config: dict[str, Any],
    tasks: list,
    worker_cfgs: list[dict],
) -> TaskSampler:
    """根据配置选择 Sampler 类型。"""
    sampler_type = config.get("sampler", "random").lower()
    seed = config.get("seed", 42)

    if sampler_type == "family_curriculum":
        # SkillFlow 风格：每个 worker 绑定一个 task family，按 round_idx
        # 递增难度采样同一技能的任务序列（见 benchmark/family.py, curriculum.py）
        families_dir = config.get("families_dir")
        families = load_all_families(families_dir) if families_dir else load_all_families()
        worker_family_map: dict[str, str] = {}
        for w in worker_cfgs:
            if "family_id" in w:
                worker_family_map[w["client_id"]] = w["family_id"]
        return FamilyCurriculumSampler(
            families=families, worker_family_map=worker_family_map, seed=seed
        )
    if sampler_type == "skillflow_family":
        # Official Implementation Alignment Audit 新增：与 "family_curriculum"
        # 完全等价（SkillFlowFamilySampler 是 FamilyCurriculumSampler 的别名），
        # 作为推荐用于主实验的命名，与官方 SkillFlow benchmark 术语对齐。
        families_dir = config.get("families_dir")
        families = load_all_families(families_dir) if families_dir else load_all_families()
        worker_family_map = {}
        for w in worker_cfgs:
            if "family_id" in w:
                worker_family_map[w["client_id"]] = w["family_id"]
        return SkillFlowFamilySampler(
            families=families, worker_family_map=worker_family_map, seed=seed
        )
    if sampler_type == "heterogeneous":
        # 从 worker 配置提取 category 映射
        category_map: dict[str, list[str]] = {}
        for w in worker_cfgs:
            if "task_categories" in w:
                category_map[w["client_id"]] = w["task_categories"]
        if category_map:
            return HeterogeneousSampler(
                tasks=tasks, worker_category_map=category_map, seed=seed
            )
        # 没有 category 映射时退化为 random
        logging.getLogger(__name__).warning(
            "sampler=heterogeneous 但 workers 未配置 task_categories，退化为 random"
        )
    elif sampler_type == "difficulty_aware":
        # Official Implementation Alignment Audit 新增守卫：DifficultyAwareSampler
        # 仅供 ablation 研究使用（见 DifficultyAwareSampler.ABLATION_ONLY 及
        # docs/SIMPLIFICATIONS.md §2.3），主实验配置必须显式声明
        # `ablation: true` 才允许使用，否则降级为推荐的
        # SkillFlowFamilySampler（需要 families 数据）或 random。
        if not config.get("ablation", False):
            logging.getLogger(__name__).warning(
                "sampler=difficulty_aware 但 未声明 ablation: true——"
                "DifficultyAwareSampler 仅供 ablation 研究使用，不能用于主实验，"
                "已降级为 random。如需使用请在配置里显式加 ablation: true。"
            )
            return RandomSampler(tasks=tasks, seed=seed)
        return DifficultyAwareSampler(tasks=tasks, seed=seed)

    return RandomSampler(tasks=tasks, seed=seed)


# ---------------------------------------------------------------------------
# 单实验运行
# ---------------------------------------------------------------------------


def run_single(
    config_path: str,
    rounds_override: int | None = None,
    output_dir_override: str | None = None,
    dry_run: bool = False,
) -> ExperimentResult:
    """
    运行单个实验配置文件，返回 ExperimentResult。

    Args:
        config_path:        YAML 配置路径
        rounds_override:    覆盖配置中的 rounds
        output_dir_override: 覆盖配置中的 output_dir
        dry_run:            若 True，仅打印配置后退出
    """
    logger = logging.getLogger(__name__)
    config = _load_yaml(config_path)
    logger.info("加载配置: %s  setting=%s", config_path, config.get("setting_name"))

    # 覆盖参数
    if rounds_override is not None:
        config["rounds"] = rounds_override
    if output_dir_override is not None:
        config["output_dir"] = output_dir_override

    # Dry-run：打印配置并退出
    if dry_run:
        print("\n─── Dry-Run: 配置概览 ───────────────────────────────────")
        print(f"  setting_name: {config.get('setting_name')}")
        print(f"  federated:    {config.get('federated', False)}")
        print(f"  rounds:       {config.get('rounds')}")
        print(f"  sampler:      {config.get('sampler', 'random')}")
        print(f"  workers:      {len(config.get('workers', []))}")
        for w in config.get("workers", []):
            print(f"    - {w.get('client_id')}: {w.get('backbone_model')} + {w.get('agent_harness')}")
        if config.get("federated"):
            srv = config.get("server", {})
            print(f"  server:       {srv.get('backbone_model')} | {srv.get('family_name')}")
        print("─" * 55)
        sys.exit(0)

    # 构建组件
    output_dir = Path(config.get("output_dir", "results"))
    output_dir.mkdir(parents=True, exist_ok=True)
    storage_root = output_dir / "libraries"
    worker_cfgs = config.get("workers", [])
    profiles = _build_worker_profiles(worker_cfgs)
    clients = _build_clients(worker_cfgs, storage_root)
    executor = _build_executor(profiles)

    sampler_type = config.get("sampler", "random").lower()
    # family_curriculum 模式不依赖旧版 default_tasks.json 扁平任务列表
    tasks = [] if sampler_type == "family_curriculum" else TaskLoader.load_default()
    sampler = _build_sampler(config, tasks, worker_cfgs)
    reporter = ResultReporter(verbose=True)

    federated = config.get("federated", False)
    rounds = config.get("rounds", 8)
    setting_name = config.get("setting_name", Path(config_path).stem)

    if federated:
        server_cfg = config.get("server", {})
        if not server_cfg:
            raise ValueError("federated=true 但配置文件中缺少 server 字段")
        server = _build_server(server_cfg, profiles)
        runner = FederatedRunner(
            clients=clients,
            server=server,
            executor=executor,
            sampler=sampler,
            rounds=rounds,
            setting_name=setting_name,
            reporter=reporter,
            output_dir=output_dir,
        )
    else:
        runner = SelfEvolutionRunner(
            clients=clients,
            executor=executor,
            sampler=sampler,
            rounds=rounds,
            setting_name=setting_name,
            reporter=reporter,
        )

    result = runner.run()

    # 持久化 CSV
    csv_path = output_dir / f"{setting_name}_metrics.csv"
    reporter.to_csv(result, csv_path)
    logger.info("指标 CSV 已保存: %s", csv_path)

    return result


# ---------------------------------------------------------------------------
# Compare 模式（SE vs FederatedSkill，对应 Table 1）
# ---------------------------------------------------------------------------


def run_compare(
    se_config_path: str,
    fed_config_path: str,
    rounds_override: int | None = None,
    dry_run: bool = False,
) -> None:
    """同时运行 SE + Federated，打印 Table 1 格式对比。"""
    logger = logging.getLogger(__name__)
    logger.info("运行 Compare 模式: SE vs Federated")

    se_result = run_single(se_config_path, rounds_override, dry_run=dry_run)
    fed_result = run_single(fed_config_path, rounds_override, dry_run=dry_run)

    reporter = ResultReporter()
    reporter.print_comparison(
        results={se_result.setting_name: se_result, fed_result.setting_name: fed_result},
        baseline_key=se_result.setting_name,
    )

    # 导出对比 CSV
    output_dir = Path("results/comparison")
    output_dir.mkdir(parents=True, exist_ok=True)
    reporter.comparison_to_csv(
        results={se_result.setting_name: se_result, fed_result.setting_name: fed_result},
        path=output_dir / "table1_comparison.csv",
        baseline_key=se_result.setting_name,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FederatedSkill 复现实验主入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--config", type=str, help="单实验 YAML 配置文件路径")
    mode.add_argument(
        "--compare",
        action="store_true",
        help="同时运行 SE + Federated 并打印对比（需要 --se-config 和 --fed-config）",
    )

    parser.add_argument("--se-config", type=str, default=None, help="compare 模式下 SE 配置路径")
    parser.add_argument("--fed-config", type=str, default=None, help="compare 模式下 Fed 配置路径")
    parser.add_argument("--rounds", type=int, default=None, help="覆盖配置文件中的 rounds")
    parser.add_argument("--output-dir", type=str, default=None, help="覆盖配置文件中的 output_dir")
    parser.add_argument("--dry-run", action="store_true", help="仅打印配置并退出，不调 LLM API")
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    return parser.parse_args()


def main() -> None:
    _load_project_dotenv()
    args = _parse_args()
    _setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    if args.compare:
        if not args.se_config or not args.fed_config:
            logger.error("--compare 模式需要同时提供 --se-config 和 --fed-config")
            sys.exit(1)
        run_compare(
            se_config_path=args.se_config,
            fed_config_path=args.fed_config,
            rounds_override=args.rounds,
            dry_run=args.dry_run,
        )
    elif args.config:
        run_single(
            config_path=args.config,
            rounds_override=args.rounds,
            output_dir_override=args.output_dir,
            dry_run=args.dry_run,
        )
    else:
        # 默认运行 Full-Hetero（论文核心实验）
        default_cfg = "experiments/configs/setting_full_hetero.yaml"
        logger.info("未指定 --config，使用默认配置: %s", default_cfg)
        if not Path(default_cfg).exists():
            logger.error("默认配置不存在: %s", default_cfg)
            sys.exit(1)
        run_single(config_path=default_cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
