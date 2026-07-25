"""
run_experiment.py — 统一实验入口（Algorithm 1）

按论文 Section 4 / Section 5 的协议，从 YAML 配置文件构建并运行实验。

支持 Setting 1-4 + 所有消融：
  python experiments/run_experiment.py --config experiments/configs/setting_se.yaml
  python experiments/run_experiment.py --config experiments/configs/setting_homo_fed.yaml --rounds 8
  python experiments/run_experiment.py --config experiments/configs/setting_hetero_backbone.yaml --dry-run
  python experiments/run_experiment.py --config experiments/configs/ablation_a1_no_capability_matrix.yaml

输出结构：
  <output_dir>/
    round_<N>_summary.json   每轮指标快照
    experiment_summary.json  全局汇总（Table 1 行）
    figures/                 (--plot 时) Figure 2/3/4
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# 确保项目根目录在 sys.path（从任意位置运行时）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_project_dotenv(env_path: Path | None = None) -> bool:
    """
    在实验入口加载仓库根目录 `.env`，让配置里的 api_key_env 能从文件落到
    `os.environ`。

    只在 run_experiment/run_family_experiment/main 这些入口调用，不在
    `_build_backbone()` 或 `llm.backbone` 导入时做全局副作用，避免破坏单元
    测试中通过 `patch.dict(os.environ, ...)` 精确控制环境变量的能力。
    `override=False`：若用户已经在终端显式设置了环境变量，终端值优先。
    """
    env_path = env_path or (_REPO_ROOT / ".env")
    if not env_path.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("发现 .env 但 python-dotenv 未安装，无法自动加载: %s", env_path)
        return False
    return bool(load_dotenv(env_path, override=False))

from benchmark.curriculum import FamilyCurriculumSampler
from benchmark.family import load_all_families
from benchmark.sampler import HeterogeneousSampler, RandomSampler
from benchmark.skillflow_benchmark import FamilyTaskSampler
from client.executor import TaskExecutor
from executor.harness_executor import HarnessAwareExecutor
from executor.router_executor import VerificationAwareExecutor
from client.federated_client import FederatedClient
from core.constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, MOONSHOT_TEMPERATURE
from core.datatypes import WorkerProfile
from evaluation.evaluator import ExperimentEvaluator, ExperimentResult
from evaluation.paper_export import LEGACY_ENGINEERING_FAMILY_IDS
from experiments.baseline import SelfEvolutionRunner
from experiments.federated import FederatedRunner
from experiments.task_checkpoint import collect_task_checkpoint_stats
from llm.backbone import BackboneCallResult, LLMBackbone
from llm.router import BackboneRouter
from server.evolution import FederatedServer

logger = logging.getLogger(__name__)


# ===========================================================================
# 配置解析
# ===========================================================================

def _load_yaml(config_path: Path) -> dict[str, Any]:
    """安全加载 YAML 实验配置文件。"""
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误，期望映射类型：{config_path}")
    return data


# Experiment Integrity Hardening TASK3：每个 worker 必须显式声明的字段。
# 不包含 api_base/max_context_tokens/is_moonshot——这三个有合理默认值，
# 不在本次加固范围内（不过度工程化）。
_REQUIRED_WORKER_FIELDS = ("client_id", "backbone_model", "agent_harness", "model_provider", "api_key_env")


def _validate_experiment_config(cfg: dict[str, Any]) -> None:
    """
    实验配置 Schema 校验（Experiment Integrity Hardening TASK3）。

    背景：在真实 API 实验开始前，消除可能导致论文结果被静默污染的
    隐藏 fallback：此前 `mode = cfg.get("sampler", "random")` / `seed = cfg.get("seed", 42)`
    / `worker_cfg.get("agent_harness", "claude-code")` 等调用，在配置文件本身
    缺失这些字段时会静默使用默认值，而不是报错——实验者可能以为自己
    配置了某个 agent_harness/model_provider，实际却静默跑了完全不同的默认值，
    且事后无法发现。

    本函数只做只读的存在性校验，不修改 cfg，不影响任何 client/server/
    evolution 算法逻辑。在 `run_experiment()` 中在构建 sampler/worker/backbone
    之前调用，不合法时直接 raise ValueError 终止（包括 --dry-run 也会
    触发，便于实验者在真实 API 调用之前就发现配置缺陷）。

    Args:
        cfg: `_load_yaml()` 加载出的原始配置 dict。

    Raises:
        ValueError: 缺少 sampler/seed/workers 顶层字段，或任何 worker
            缺少 client_id/backbone_model/agent_harness/model_provider/
            api_key_env 中的任一项。
    """
    missing_top = [k for k in ("sampler", "seed", "workers") if k not in cfg]
    if missing_top:
        raise ValueError(
            f"实验配置缺失必需顶层字段: {missing_top}。为防止静默默认值污染论文结果，"
            f"sampler/seed/workers 必须在配置文件中显式声明，不允许依赖代码里的默认值。"
        )

    workers = cfg.get("workers") or []
    if not isinstance(workers, list) or not workers:
        raise ValueError("实验配置的 workers 必须是非空列表。")

    for idx, wc in enumerate(workers):
        if not isinstance(wc, dict):
            raise ValueError(f"workers[{idx}] 必须是映射类型，实际: {type(wc).__name__}")
        missing = [f for f in _REQUIRED_WORKER_FIELDS if f not in wc]
        if missing:
            raise ValueError(
                f"workers[{idx}]（client_id={wc.get('client_id', '<unknown>')!r}）"
                f"缺失必需字段: {missing}。每个 worker 必须显式声明 "
                f"client_id/backbone_model/agent_harness/model_provider/api_key_env，"
                f"不允许依赖代码里的默认值。"
            )

    if cfg.get("federated") and not cfg.get("server"):
        raise ValueError("federated=true 时必须在配置中提供 server 节点。")


def _apply_paper_benchmark_scope(
    cfg: dict[str, Any], families: dict[str, Any]
) -> dict[str, Any]:
    """
    Paper Benchmark Scope 过滤（Experimental Protocol Fix — TASK1，P0）。

    背景：`benchmark/families/` 目录下有 25 个 family JSON，其中 20 个是
    论文使用的真实 SkillFlow benchmark family，另外 5 个
    （data_cleaning/data_transformation/document_processing/
    financial_analysis/report_generation）是本项目自建的 legacy/engineering
    family，仅用于离线单测，论文 Table1/Figure2/Figure3 从未涉及它们。

    此前的问题：`load_all_families()` 不做任何过滤，25 个 family 全部进入
    `_run_family_loop()` 的执行与聚合（`mean_success_rate` 等），
    `evaluation/paper_export.py` 只在**导出 CSV 之后**打 `is_paper_family`
    标签，并不能改变已经执行/已经聚合的数据——过滤发生得太晚。

    修复：不删除任何 legacy family（仍保留在磁盘上供工程测试使用），而是在
    "任务执行 / family 循环 / 指标聚合" 之前，按配置文件里的
    `paper_benchmark_only: true/false` 显式过滤 `families` 字典本身。
    排除列表复用 `evaluation.paper_export.LEGACY_ENGINEERING_FAMILY_IDS`，
    不在本文件重复定义，避免两处 schema 漂移。

    `paper_benchmark_only` 未设置或为 false 时保持旧行为（加载全部 25 个
    family），但会打印一条明确的 WARNING，提示当前结果不可与论文对比——
    不静默、不猜测。
    """
    paper_benchmark_only = bool(cfg.get("paper_benchmark_only", False))

    if not paper_benchmark_only:
        print("Paper benchmark mode:")
        print("  disabled")
        print("Loaded families:")
        print(f"  {len(families)} (含 legacy family，与论文 Table1/Figure2/Figure3 不可比)")
        logger.warning(
            "paper_benchmark_only=false：本次运行加载全部 %d 个 family"
            "（含本项目自建的 %d 个 legacy family: %s）。这些结果不能作为论文"
            "复现证据，仅用于工程验证。如需复现论文结果，请在配置文件中设置"
            " paper_benchmark_only: true。",
            len(families), len(LEGACY_ENGINEERING_FAMILY_IDS),
            sorted(LEGACY_ENGINEERING_FAMILY_IDS),
        )
        return families

    excluded = {fid: f for fid, f in families.items() if fid in LEGACY_ENGINEERING_FAMILY_IDS}
    filtered = {fid: f for fid, f in families.items() if fid not in LEGACY_ENGINEERING_FAMILY_IDS}

    if len(filtered) != 20:
        raise ValueError(
            f"paper_benchmark_only=true 要求过滤后恰好剩余论文的 20 个官方 "
            f"family，但实际剩余 {len(filtered)} 个。请检查 benchmark/families/ "
            f"目录内容（当前共 {len(families)} 个文件）是否与 "
            f"evaluation.paper_export.LEGACY_ENGINEERING_FAMILY_IDS "
            f"（{len(LEGACY_ENGINEERING_FAMILY_IDS)} 个）保持同步，不允许静默 "
            f"跑一个数量不确定的 family 集合。"
        )

    print("Paper benchmark mode:")
    print("  enabled")
    print("Loaded families:")
    print(f"  {len(filtered)}")
    print("Excluded:")
    print(f"  {len(excluded)} legacy families")
    logger.info(
        "paper_benchmark_only=true：加载论文官方 %d 个 family，排除 %d 个 "
        "legacy family: %s",
        len(filtered), len(excluded), sorted(excluded),
    )
    return filtered


def _apply_family_subset(
    cfg: dict[str, Any], families: dict[str, Any]
) -> dict[str, Any]:
    """按配置显式选择 family 子集；未知 family 直接失败。"""
    requested = cfg.get("family_subset")
    if requested is None:
        return families
    if not isinstance(requested, list) or not requested or not all(
        isinstance(family_id, str) for family_id in requested
    ):
        raise ValueError("family_subset 必须是非空字符串数组")
    missing = [family_id for family_id in requested if family_id not in families]
    if missing:
        raise ValueError(f"family_subset 包含未知 family: {missing}")
    selected = {family_id: families[family_id] for family_id in requested}
    logger.info("family_subset 启用: %s", requested)
    return selected


def _build_worker_profile(worker_cfg: dict[str, Any]) -> WorkerProfile:
    """从 YAML worker 节点构造 WorkerProfile。"""
    return WorkerProfile(
        client_id=worker_cfg["client_id"],
        backbone_model=worker_cfg["backbone_model"],
        agent_harness=worker_cfg.get("agent_harness", "claude-code"),
        model_provider=worker_cfg.get("model_provider", "dashscope"),
        api_base=worker_cfg.get("api_base", ""),
        api_key_env=worker_cfg.get("api_key_env", "DASHSCOPE_KEY"),
        max_context_tokens=worker_cfg.get("max_context_tokens", 131072),
        is_moonshot=worker_cfg.get("is_moonshot", False),
    )


def _build_mock_backbone() -> LLMBackbone:
    """
    构造一个不发出任何真实网络请求的 mock backbone，用于 `--mock` 结构验证
    （校验 family 循环顺序 / round 单调 / family 不混淆），不消耗任何 API
    费用、不要求任何环境变量。复用 `_test_e2e.py` 里已验证过的
    `unittest.mock.MagicMock` 打法，只是抽成可在真实实验入口复用的函数——
    不新增 sampler/evaluator/tracker，只是 LLMBackbone 的测试替身。

    Algorithm Fidelity Fix — Multi-Directive Execution（TASK6 mock 验证）：
    此前 `call_json` 对 Stage1/Stage2/Distiller 三种调用方一律返回同一份
    固定 JSON（`{"upsert_files": {}, "delete_paths": {}, "summary": ...}`），
    其中没有 "directives" 字段，`EvolutionPlanner._parse_plan()` 用
    `raw.get("directives", [])` 兜底为空列表——导致 `--mock` 模式下永远不
    会产生任何 directive，无法用于验证 `server/evolution.py` 恢复的"该
    worker 本轮全部 directives 都要被真实执行"这层 cardinality 修复。
    这里按 system_prompt 是否包含 Stage1 独有的 "capability_matrix" schema
    关键字（见 `server/prompt_builder.py::Stage1PromptBuilder._STAGE1_SCHEMA`，
    Stage2/Distiller 的 schema 都不含这个词）区分 Stage1 与其它调用，仅对
    Stage1 额外返回 2 条 target_worker_id="u0" 的 directive（不同 priority/
    workflow_name/action），其余调用（Stage2 合并、client 蒸馏）继续走原来
    的固定 JSON，行为不变。只改变 mock 测试替身的返回内容，不改变任何真实
    Stage1/Stage2 决策代码。
    """
    from unittest.mock import MagicMock

    backbone = MagicMock()
    backbone.call.return_value = BackboneCallResult(
        text="```python\ndef solve(*args, **kwargs):\n    return None\n```",
        prompt_tokens=10, completion_tokens=5, cost_usd=0.0,
    )

    _generic_json = {"upsert_files": {}, "delete_paths": [], "summary": "[--mock] 未真实调用 LLM"}
    _stage1_json_with_directives = {
        "capability_matrix": {},
        "high_level_memory": "[--mock] 未真实调用 LLM",
        "low_level_memories": {},
        "directives": [
            {
                "target_worker_id": "u0",
                "workflow_name": "mock-workflow-a",
                "action": "repair",
                "priority": 4,
                "reason": "[--mock] 演示 directive 0（不代表真实决策）",
                "source_worker_id": None,
                "source_reward": None,
            },
            {
                "target_worker_id": "u0",
                "workflow_name": "mock-workflow-b",
                "action": "absorb",
                "priority": 2,
                "reason": "[--mock] 演示 directive 1（不代表真实决策）",
                "source_worker_id": "u1",
                "source_reward": 0.8,
            },
        ],
    }

    def _mock_call_json(user_prompt: str, system_prompt: str | None = None):
        # 注：`"capability_matrix"` 这个 Stage1 专属 schema 关键字实际拼装在
        # `Stage1PromptBuilder.build()` 返回的 user_prompt 里（见
        # `server/prompt_builder.py::_section_schema()`），不在 system_prompt
        # 里——因此这里要检查 user_prompt，而不是 system_prompt。
        payload = (
            _stage1_json_with_directives
            if "capability_matrix" in user_prompt
            else _generic_json
        )
        return (
            dict(payload),
            BackboneCallResult(text="{}", prompt_tokens=10, completion_tokens=5, cost_usd=0.0),
        )

    backbone.call_json.side_effect = _mock_call_json
    return backbone


def _build_faithful_mock_backbone() -> LLMBackbone:
    """
    "FederatedSkill Faithful Mock Validation" TASK1 新增：一个比
    `_build_mock_backbone()` 更"忠实"的 mock backbone，专用于 `mock_federated`
    模式（`--mock-federated`），修复原 mock 固定返回空 `upsert_files`/空
    `capability_matrix` 导致 library/capability_matrix.jsonl 永远为空、
    mock 无法真正验证 skill evolution 链路的结构性缺陷（Artifact Fidelity
    Hardening 阶段 mock 验证已发现此问题）。

    与 `_build_mock_backbone()` 完全独立、互不影响：
      - `--mock`（原有）：语义仍是"结构验证"（family 循环顺序/round 单调/
        family 不混淆），不要求 upsert_files/capability_matrix 非空。
      - `--mock-federated`（本函数）：语义是"faithful federated mock"，
        三个调用方都返回 schema 合法、内容非空的 payload，让
        PatchDistiller → Stage1 EvolutionPlanner → Stage2 EvolutionExecutor
        全链路都能真实产出至少一个技能/一条 ABSORB 或 REPAIR directive/
        一个有效 merged patch。

    禁止修改真实 LLM 调用路径（TASK1 硬性约束）：本函数与
    `_build_mock_backbone()` 一样只在 `_build_backbone(mock=True/
    mock_federated=True)` 分支下被调用，真实 backbone 构造代码
    （`LLMBackbone.from_worker_profile()`）分支完全未改动。

    三个调用方按 user_prompt 中的 schema 专属关键字区分（三者互不重叠，
    见对应 PromptBuilder 的 `_section_schema()`/`_STAGE*_SCHEMA`/
    `_OUTPUT_SCHEMA`）：
      - Stage1 planner（`server/prompt_builder.py::Stage1PromptBuilder`）
        专属关键字 "capability_matrix"。
      - Stage2 merge（`server/prompt_builder.py::MergePromptBuilder`）
        专属关键字 "decision_log"（Stage1/Distiller 的 schema 都不含这个词）。
      - PatchDistiller（`llm/prompt_builder.py::DistillerPromptBuilder`）
        没有专属关键字可判定为"其余情况"（唯一关键字 "rationale" 用排除法
        已经足够，不需要额外检查）。
    """
    from unittest.mock import MagicMock

    backbone = MagicMock()
    backbone.call.return_value = BackboneCallResult(
        text="```python\ndef solve(*args, **kwargs):\n    return None\n```",
        prompt_tokens=10, completion_tokens=5, cost_usd=0.0,
    )

    # 三个调用方共用同一份演示 SKILL.md 内容——满足
    # client/library.py::SkillLibrary 对技能目录的最低要求（必须有 SKILL.md
    # 且含合法 YAML 前置元数据 name/description）。
    _mock_skill_md = (
        "---\n"
        "name: mock-workflow-a\n"
        "description: \"[mock_federated] faithful mock 演示技能，用于验证 "
        "PatchDistiller/Stage1/Stage2 全链路，不代表真实决策\"\n"
        "---\n\n"
        "# Workflow\n"
        "1. [mock_federated] 演示步骤一。\n"
        "2. [mock_federated] 演示步骤二。\n"
    )

    # PatchDistiller 调用（client/distiller.py::_step5_call_llm）：upsert_files
    # 必须非空，否则技能库/capability_matrix 永远长不出真实内容（TASK1 要
    # 修复的根本问题）。
    _distiller_json = {
        "upsert_files": {"mock-workflow-a/SKILL.md": _mock_skill_md},
        "delete_paths": [],
        "summary": "[mock_federated] 蒸馏出一个演示 SKILL.md（不代表真实决策）",
        "rationale": "[mock_federated] 用于验证 faithful mock 全链路，不代表真实决策",
    }

    # Stage1 planner 调用（server/planner.py::plan）：capability_matrix 必须
    # 是非空的 workflow × worker 映射（而不是 {}），并附带至少一条
    # ABSORB/REPAIR directive（TASK1 要求）。worker id 沿用各 setting2-4
    # 配置文件里的真实 client_id（u0/u1/u2）。
    _stage1_json = {
        "capability_matrix": {
            "mock-workflow-a": {
                "u0": "absorbing",
                "u1": "covered",
                "u2": "gap",
            },
        },
        "high_level_memory": "[mock_federated] 演示 high-level memory（不代表真实决策）",
        "low_level_memories": {},
        "directives": [
            {
                "target_worker_id": "u0",
                "workflow_name": "mock-workflow-a",
                "action": "repair",
                "priority": 4,
                "reason": "[mock_federated] 演示 REPAIR directive（不代表真实决策）",
                "source_worker_id": None,
                "source_reward": None,
            },
            {
                "target_worker_id": "u2",
                "workflow_name": "mock-workflow-a",
                "action": "absorb",
                "priority": 2,
                "reason": "[mock_federated] 演示 ABSORB directive（不代表真实决策）",
                "source_worker_id": "u1",
                "source_reward": 0.8,
            },
        ],
    }

    # Stage2 EvolutionExecutor 调用（server/merge.py::execute_for_worker）：
    # 必须产出有效的 merged patch（同样要求 upsert_files 非空）。
    # decision_log 故意不带 "action" 字段——`_parse_output()` 里"Stage2 不得
    # 改写 Stage1 已决定的 action"一致性检查只在该字段非空且与
    # directive.action 不一致时才记审计 warning；缺省该字段时直接采用
    # directive.action，行为上等价但不会刷无意义的 warning 日志。
    _stage2_json = {
        "upsert_files": {"mock-workflow-a/SKILL.md": _mock_skill_md},
        "delete_paths": [],
        "summary": "[mock_federated] Stage2 生成的演示 merged patch（不代表真实决策）",
        "decision_log": {
            "affected_files": ["mock-workflow-a/SKILL.md"],
            "reason": "[mock_federated] 演示 merge 决策（不代表真实决策）",
        },
        "updated_low_level_memory": "[mock_federated] 演示 low-level memory 更新（不代表真实决策）",
    }

    def _faithful_mock_call_json(user_prompt: str, system_prompt: str | None = None):
        if "capability_matrix" in user_prompt:
            payload = _stage1_json
        elif "decision_log" in user_prompt:
            payload = _stage2_json
        else:
            payload = _distiller_json
        # 用 deepcopy（而非 dict()浅拷贝）：payload 里含嵌套 dict/list
        # （capability_matrix/directives/upsert_files），浅拷贝只复制顶层
        # key，调用方（如 _parse_plan/_step6_validate）若就地修改嵌套结构，
        # 会污染下一次调用（甚至跨 worker/跨 round）返回的"新" payload，
        # 8 轮联邦实验里这会导致 capability_matrix 状态在多轮之间意外累积
        # 错乱。deepcopy 保证每次调用互相独立。
        return (
            copy.deepcopy(payload),
            BackboneCallResult(text="{}", prompt_tokens=10, completion_tokens=5, cost_usd=0.0),
        )

    backbone.call_json.side_effect = _faithful_mock_call_json
    return backbone


def _build_backbone(
    cfg: dict[str, Any], role: str = "worker", mock: bool = False, mock_federated: bool = False,
) -> LLMBackbone:
    """
    从配置节点构造 LLMBackbone。

    若对应 API key 未设置，抛出 EnvironmentError（早于实验开始时发现）。
    mock=True 时跳过真实 backbone 构造和 API key 校验，直接返回
    `_build_mock_backbone()`——用于 `--mock` 结构验证。
    mock_federated=True（"FederatedSkill Faithful Mock Validation" TASK1
    新增）时返回 `_build_faithful_mock_backbone()`——用于 `--mock-federated`
    faithful federated mock 验证，优先级高于 mock（两者同时为 True 时走
    faithful 分支）。真实 backbone 构造分支（下方 mock/mock_federated 均为
    False 时的代码）未被本次改动触碰。
    """
    if mock_federated:
        return _build_faithful_mock_backbone()
    if mock:
        return _build_mock_backbone()

    api_key_env = cfg.get("api_key_env", "DASHSCOPE_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise EnvironmentError(
            f"[{role}] 环境变量 {api_key_env} 未设置。"
            f" 请先执行: set {api_key_env}=<your-key>"
        )

    model = cfg["backbone_model"]
    api_base = cfg.get("api_base", "")

    # Moonshot 要求 temperature ≥ 1.0；由 LLMBackbone.from_worker_profile 处理，
    # 此处直接使用 from_worker_profile 以统一逻辑。
    profile_dict = {
        "client_id": cfg.get("client_id", role),
        "backbone_model": model,
        "agent_harness": cfg.get("agent_harness", "claude-code"),
        "model_provider": cfg.get("model_provider", "dashscope"),
        "api_base": api_base,
        "api_key_env": api_key_env,
        "max_context_tokens": cfg.get("max_context_tokens", 131072),
        "is_moonshot": cfg.get("is_moonshot", False),
    }
    profile = WorkerProfile(**profile_dict)

    # merger_max_tokens（server 端 evolution agent 专用，官方 configs 2/3/4 中
    # merger_llm.max_tokens: 16384）此前只写在 yaml 里，从未被读取——
    # LLMBackbone.from_worker_profile() 不接收 max_tokens 就静默回退到
    # DEFAULT_MAX_TOKENS=8192。这里在 role=="server" 时显式读取并传入。
    if role == "server" and "merger_max_tokens" in cfg:
        return LLMBackbone.from_worker_profile(
            profile, max_tokens=int(cfg["merger_max_tokens"])
        )
    return LLMBackbone.from_worker_profile(profile)


# ===========================================================================
# Reproducibility Metadata（Result Reproduction Readiness Audit TASK1/TASK6）
# ===========================================================================
#
# 下面三个 helper 只做只读的“运行时状态 -> JSON 元数据”整理，不参与/不影响
# 任何 client 执行/patch 生成/evolution agent/能力矩阵/memory/merge 逻辑：
#   - _worker_runtime_metadata() / _server_runtime_metadata()：从已经构造好
#     的 WorkerProfile + LLMBackbone 实例读出 backbone_model/agent_harness/
#     provider/temperature/max_tokens（真实生效值，见 llm/backbone.py 新增
#     的 temperature/max_tokens 只读属性），而不是重新猜测/硬编码。
#   - _config_file_hash()：配置文件字节的 sha256，用于事后核对“这份结果是不是
#     用这份配置跑出来的”。
#   - _build_reproducibility_metadata()：把 seed/时间戳/配置路径与哈希/主模型
#     信息打包成一个 dict，供 _save_family_loop_summary()/_save_results() 写入
#     experiment_summary.json 的 "reproducibility" 键。

def _runtime_mode_label(mock: bool, mock_federated: bool) -> str:
    if mock_federated:
        return "mock_federated"
    if mock:
        return "mock"
    return "api"


def _worker_runtime_metadata(
    profile: WorkerProfile, backbone: Any, mock: bool, runtime_mode: str | None = None,
) -> dict[str, Any]:
    """
    单个 worker 的运行时元数据（写入 experiment_summary.json 的 "workers" 块）。

    非 mock：直接读 LLMBackbone 实例的 temperature/max_tokens 只读属性——
    这两个值是 LLMBackbone.from_worker_profile() 构造时算好并保存在实例上的，
    本函数只是读出来，不重新计算，因此天然反映“运行时真实生效值”（哪怕配置
    文件里某个字段实际从未被任何代码读取，这里读到的也是 backbone 真正在用
    的数值，不会被配置文件字面值误导）。

    mock：_build_mock_backbone() 返回的是 unittest.mock.MagicMock，没有真实
    temperature/max_tokens 可读（访问会得到另一个 MagicMock，无法 JSON 序列
    化）。这种情况下改为复用 LLMBackbone.from_worker_profile() 内部同一条
    Moonshot 温度下限规则的两个输入常量（core.constants），而不是新写一个
    硬编码字面量。
    """
    if mock:
        temperature = MOONSHOT_TEMPERATURE if profile.is_moonshot else DEFAULT_TEMPERATURE
        max_tokens = DEFAULT_MAX_TOKENS
    else:
        temperature = backbone.temperature
        max_tokens = backbone.max_tokens
    return {
        "backbone_model": profile.backbone_model,
        "agent_harness": profile.agent_harness,
        "provider": profile.model_provider,
        "execution_mode": runtime_mode or ("mock" if mock else "api"),
        "mock": bool(mock),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def _server_runtime_metadata(
    server_cfg: dict[str, Any], backbone: Any, mock: bool, runtime_mode: str | None = None,
) -> dict[str, Any]:
    """服务器 backbone 的运行时元数据，规则与 _worker_runtime_metadata() 一致
    （服务器没有 WorkerProfile，直接从 server 配置节点读 backbone_model/
    model_provider/is_moonshot）。"""
    is_moonshot = bool(server_cfg.get("is_moonshot", False))
    if mock:
        temperature = MOONSHOT_TEMPERATURE if is_moonshot else DEFAULT_TEMPERATURE
        max_tokens = DEFAULT_MAX_TOKENS
    else:
        temperature = backbone.temperature
        max_tokens = backbone.max_tokens
    return {
        "backbone_model": server_cfg.get("backbone_model"),
        "provider": server_cfg.get("model_provider", "dashscope"),
        "execution_mode": runtime_mode or ("mock" if mock else "api"),
        "mock": bool(mock),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def _config_file_hash(config_path: Path) -> str:
    """配置文件内容的 sha256 十六进制摘要（Reproducibility Metadata TASK6）。"""
    return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()


def _build_reproducibility_metadata(
    config_path: Path,
    seed: int,
    worker_metadata: dict[str, dict[str, Any]],
    server_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    组装写入 experiment_summary.json 顶层 "reproducibility" 键的字典：
    seed / 时间戳 / 配置文件路径与哈希 / 主模型名 / API provider
    （Result Reproduction Readiness Audit TASK6）。

    “主模型”取服务器 backbone（联邦设置的协调点）；非联邦设置没有服务器，
    退化为取（唯一的）worker backbone——两种情况都是该 setting 语义上
    最具代表性的单一模型，而不是任意选择。完整的逐 worker 明细已经在同一份
    JSON 的 "workers" 键里，这里的 model_name/api_provider 只是一个便于
    快速核对的摘要，不是唯一数据来源。
    """
    primary = server_metadata or next(iter(worker_metadata.values()), {})
    return {
        "seed": seed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config_path": str(config_path),
        "config_hash": _config_file_hash(config_path),
        "model_name": primary.get("backbone_model"),
        "api_provider": primary.get("provider"),
    }


def _build_sampler(cfg: dict[str, Any], families: dict):
    """
    按配置 sampler 字段构造采样器。

    支持：
      - "random"        → RandomSampler（从所有任务随机抽取）
      - "curriculum"     → FamilyTaskSampler(mode="curriculum")
      - "replicate"      → FamilyTaskSampler(mode="replicate")（联邦默认）
      - "heterogeneous"  → HeterogeneousSampler（按 worker 绑定的 task_categories
                           分配不同任务子集，用于 Setting3/4，验证 D_i ≠ D_j）

    Paper Fidelity Audit（Phase15 P0-1）：
    原实现遇到未知 sampler 值（包括 "heterogeneous"）时只打 warning 并静默回退
    到 random，导致 Setting3/4 的 D1=D2=D3，无法验证论文的 cross-client skill
    transfer 假设。现改为对未知值 fail-loud（raise ValueError），并补上真正的
    "heterogeneous" 分支；HeterogeneousSampler 缺少 task_categories 时同样
    fail-loud，而不是静默退化。
    """
    mode = cfg.get("sampler", "random")
    seed = cfg.get("seed", 42)
    all_tasks = [t for fam in families.values() for t in fam.tasks]

    if mode == "random":
        return RandomSampler(tasks=all_tasks, seed=seed)
    elif mode in ("curriculum", "replicate"):
        return FamilyTaskSampler(families=families, mode=mode, seed=seed)
    elif mode == "heterogeneous":
        workers = cfg.get("workers") or []
        if not workers:
            raise ValueError(
                "sampler='heterogeneous' 要求配置文件中存在 workers 列表"
            )
        missing = [
            w.get("client_id", "<unknown>")
            for w in workers
            if not w.get("task_categories")
        ]
        if missing:
            raise ValueError(
                f"sampler='heterogeneous' 要求每个 worker 配置 task_categories "
                f"字段（非空列表），缺失的 worker: {missing}。这是为了验证论文 "
                f"D_i ≠ D_j（不同 client 分配不同任务分布），不允许静默回退到 "
                f"random，否则会破坏 Setting3/Setting4 实验的有效性。"
            )
        worker_categories = {
            w["client_id"]: list(w["task_categories"]) for w in workers
        }
        # 真实 SkillFlow family（benchmark/families/*.json 里的 20 个官方 family）
        # 任务本身没有填 category 字段（只有 family_id），HeterogeneousSampler
        # 是按 Task.category 分组的。这里用 family_id 回填空 category，使
        # task_categories 里配置的 family 名称（如 "financial_analysis"）能真正
        # 命中官方 family 数据，而不是全部落入同一个 "" 分组。
        for t in all_tasks:
            if not t.category:
                t.category = t.family_id
        return HeterogeneousSampler(
            tasks=all_tasks, worker_categories=worker_categories, seed=seed
        )
    elif mode == "family_curriculum":
        # 每个 worker 固定绑定一个 family，按 round_idx 递增采样难度递增的
        # 任务（benchmark/curriculum.py::FamilyCurriculumSampler）。
        # 若配置文件里每个 worker 显式声明了 family_id 字段（如
        # setting_se_family.yaml 的"Setting 1b"用法），据此构造固定映射；
        # 否则留空，由调用方（如 _run_family_loop 的按 family 循环驱动）
        # 通过 assign_family() 显式绑定，不做隐式 round-robin 猜测。
        workers = cfg.get("workers") or []
        worker_family_map = {
            w["client_id"]: w["family_id"] for w in workers if w.get("family_id")
        }
        return FamilyCurriculumSampler(
            families, worker_family_map=worker_family_map or None, seed=seed
        )
    else:
        raise ValueError(
            f"未知 sampler={mode!r}。合法值: random / curriculum / replicate / "
            f"heterogeneous / family_curriculum。论文复现实验不允许静默回退，"
            f"请显式修正配置文件。"
        )


# ===========================================================================
# 干跑（dry-run）
# ===========================================================================

def _dry_run(cfg: dict[str, Any]) -> None:
    """
    打印实验配置摘要，不执行任何 LLM 调用。
    用于在真正运行前验证配置合法性。
    """
    setting_name = cfg.get("setting_name", "unknown")
    federated = cfg.get("federated", False)
    rounds = cfg.get("rounds", 8)
    workers = cfg.get("workers", [])
    server_cfg = cfg.get("server", {})

    print("=" * 60)
    print(f"[DRY-RUN] 实验配置摘要")
    print(f"  Setting:   {setting_name}")
    print(f"  Federated: {federated}")
    print(f"  Rounds:    {rounds}")
    print(f"  Workers:   {len(workers)}")
    for wc in workers:
        api_key_env = wc.get("api_key_env", "DASHSCOPE_KEY")
        key_present = "OK" if os.environ.get(api_key_env) else "未设置"
        print(f"    - {wc['client_id']}: {wc['backbone_model']} / "
              f"{wc.get('agent_harness')} [{api_key_env}: {key_present}]")
    if federated and server_cfg:
        env = server_cfg.get("api_key_env", "DASHSCOPE_KEY")
        key_ok = "OK" if os.environ.get(env) else "未设置"
        print(f"  Server:    {server_cfg.get('backbone_model')} [{env}: {key_ok}]")
    print(f"  Sampler:   {cfg.get('sampler', 'random')}")
    print(f"  Loop over families: {cfg.get('loop_over_families', False)}")
    print(f"  Output:    {cfg.get('output_dir', 'results/<setting_name>')}")
    print("=" * 60)


# ===========================================================================
# 按 family 循环（Phase1：恢复论文原始实验单位）
# ===========================================================================

def _build_executor(router: BackboneRouter, execution_mode: str):
    """按 execution_mode 构造 executor（Real CLI Harness Fidelity Fix 新增）。

    "api"（默认）: VerificationAwareExecutor —— 与本函数新增之前完全一致的
        既有行为，不做任何改变。
    "cli": HarnessAwareExecutor(mode="strict") —— 按每个 worker 的
        agent_harness 真实 spawn CLI 二进制（见 harness/ 包），CLI 未安装时
        会在真正执行任务时抛出 harness.cli_utils.CLIBinaryNotFoundError，
        不会静默回退到 API。
    """
    if execution_mode == "cli":
        return HarnessAwareExecutor(router=router, mode="strict", top_k_skills=3)
    if execution_mode != "api":
        raise ValueError(f"未知 execution_mode={execution_mode!r}，仅支持 'api'/'cli'")
    return VerificationAwareExecutor(router=router, top_k_skills=3)


def family_failure_cleanup(
    *,
    family_id: str,
    family_output_dir: Path,
    worker_ids: list[str],
    shared_library: bool,
    exc: Exception,
    elapsed_seconds: float,
    runner: Any | None = None,
) -> dict[str, Any]:
    """[Experiment Isolation Fix] family 执行失败后的清理与失败审计记录。

    真实 bug（非假设）：`_run_family_loop()` 里 `runner.run()` 抛异常时
    （某个 task 耗尽 `max_retry` 仍失败，异常向上传播），该 family 里此前
    已经成功执行的若干 round 早就通过
    `client/library.py::SkillLibrary.apply_patch()` 把 skill 文件写到了
    磁盘（`family_output_dir/libraries/<worker>/`）——这些文件在此前从未
    被清理。若同一个 family_id 在后续某次实验（同一个 output_dir）里被
    重新跑一次，`_run_family_loop()` 开头的 state-leak guard 断言
    （"family 的 library_root 初始化前必须为空"）就会因为这些残留文件而
    失败。

    正确修复不是弱化那个断言（会真正引入论文禁止的"task1产生skill +
    task2从头重跑"式隐藏污染：旧 skill 残留 + 新一轮从空库假设开始，二者
    状态不一致），而是：family 失败时立即物理删除该 family 的 skill
    library 目录，让下一次重跑该 family 时 library_root 确实是空的，
    断言天然通过、不需要被放宽。

    不影响、不清理：
      - `family_output_dir/workers/<worker>/tasks/round_*_*/` 下的逐 task
        checkpoint（trajectory.json/reward.json/failure.json/
        task_status.json）——这是 `TaskCheckpointStore` 已经在做的
        "failure log"/trajectory 落盘，本函数不删除，
        `collect_task_checkpoint_stats()` 仍能读到，满足"保留失败日志/
        超时原因"的要求。
      - 其他 family 的任何目录（本函数只接收当前失败 family 自己的
        `family_output_dir`/`worker_ids`，路径天然隔离）。
      - 算法/skill evolution/aggregation/benchmark 逻辑本身：本函数只做
        磁盘清理 + 审计记录，不调用 `SkillLibrary.apply_patch()`/
        `server/planner.py`/`server/merge.py` 等任何决策代码，也不修改
        `_run_family_loop()` 里任何 `assert` 语句。

    Args:
        family_id: 失败的 family id。
        family_output_dir: 该 family 的输出目录
            （`output_dir/families/<family_id>`）。
        worker_ids: 该 family 涉及的所有 worker/client_id。
        shared_library: 是否使用共享技能库（对应
            `family_output_dir/libraries/shared`），否则按
            `family_output_dir/libraries/<worker_id>` 逐个清理。
        exc: `runner.run()` 抛出的原始异常，用于记录失败原因/尝试解析
            超时原因。
        elapsed_seconds: 该 family 从开始到失败经过的墙钟时间。
        runner: 若调用方仍持有已失败的 runner 实例（`_run_family_loop()`
            的 except 块里 `runner` 变量仍在作用域内），会尽力
            （best-effort，单独 try/except 包裹，绝不让清理阶段的次生
            异常掩盖/替换掉原始失败原因）读取其内部的
            `_cost_accountant`/`_execution_trace`/
            `_distillation_failure_recorder` 等 recorder 并调用
            `.flush(family_output_dir)`——修复"family 提前异常退出时，
            `run()` 结尾那批 flush 调用永远不会执行到，cost/audit 日志被
            完全丢弃"的问题，满足用户"Preserve: cost log"的要求。
            `runner=None` 时跳过这一步，不报错。

    Returns:
        写入 `family_output_dir/family_failure.json` 的完整 payload，
        同时返回给调用方（便于测试断言/后续汇总复用）。
    """
    library_dirs: list[Path] = []
    if shared_library:
        library_dirs.append(family_output_dir / "libraries" / "shared")
    else:
        library_dirs.extend(
            family_output_dir / "libraries" / worker_id for worker_id in worker_ids
        )

    cleaned_dirs: list[str] = []
    for lib_dir in library_dirs:
        if not lib_dir.exists():
            continue
        try:
            shutil.rmtree(lib_dir)
            cleaned_dirs.append(str(lib_dir))
        except OSError as cleanup_exc:  # noqa: BLE001 - 清理失败不应中断失败记录
            logger.error(
                "[family_failure_cleanup] family=%s 清理 %s 失败: %s",
                family_id, lib_dir, cleanup_exc,
            )

    # best-effort：尽量把 runner 已经累积的部分 cost/audit 记录 flush 下来，
    # 不让失败前的部分成本数据被完全丢弃。任何单个 recorder flush 失败都
    # 不应掩盖/替换掉原始 family 失败原因，因此逐个包裹独立 try/except。
    flushed_logs: list[str] = []
    if runner is not None:
        for attr_name in (
            "_cost_accountant",
            "_execution_trace",
            "_distillation_failure_recorder",
        ):
            recorder = getattr(runner, attr_name, None)
            if recorder is None:
                continue
            try:
                recorder.flush(family_output_dir)
                flushed_logs.append(attr_name.lstrip("_"))
            except Exception as flush_exc:  # noqa: BLE001 - best-effort，不掩盖原始异常
                logger.warning(
                    "[family_failure_cleanup] family=%s 清理阶段 flush %s 失败"
                    "（不影响已记录的原始失败原因）: %s",
                    family_id, attr_name, flush_exc,
                )

    timeout_match = re.search(r"timeout_reason=(\S+)", str(exc))
    payload = {
        "family_id": family_id,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "timeout_reason": timeout_match.group(1) if timeout_match else None,
        "elapsed_seconds": elapsed_seconds,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cleaned_skill_library_dirs": cleaned_dirs,
        "partial_logs_flushed": flushed_logs,
    }
    family_output_dir.mkdir(parents=True, exist_ok=True)
    (family_output_dir / "family_failure.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info(
        "[family_failure_cleanup] family=%s 已清理 %d 个 skill library 目录，"
        "尝试 flush 了 %d 个部分日志，失败详情已写入 family_failure.json",
        family_id, len(cleaned_dirs), len(flushed_logs),
    )
    return payload


def _run_family_loop(
    cfg: dict[str, Any],
    families: dict[str, Any],
    worker_cfgs: list[dict[str, Any]],
    output_dir: Path,
    rounds_cap: int,
    seed: int,
    federated: bool,
    setting_name: str,
    mock: bool,
    disable_capability_matrix: bool,
    shared_library: bool,
    disable_patch_distillation: bool,
    config_path: Path | None = None,
    distillation_failure_mode: str = "strict",
    execution_mode: str = "api",
    mock_federated: bool = False,
    max_retry: int = 0,
) -> ExperimentResult:
    """
    按 family 循环跑实验——恢复论文 Section 5.1 的原始实验单位，而不是把
    20 个 family 的任务打平成一个池子随机/按类别抽取。

    官方证据（与论文一致性审计，非本函数发明）：
      1. 官方 `skillfl/skillflow_adapter/partitioning.py::TaskPartitioner`
         是 (单个 family 的任务列表, n_workers) -> shards 的静态划分函数，
         从未跨 family 打平任务池。
      2. 论文 Table 1 "Heterogeneous Backbone" / "Full Hetero" 两节，对
         每一个 family 行都同时列出 3 个 backbone 的 SE/FedSkill 成功率，
         说明这三个异构 client 在该 setting 下跑的是**同一批 family**
         （仅 backbone/harness 不同），而不是像本仓库原先
         `HeterogeneousSampler` + `task_categories` 那样把 20 个 family
         拆成互不相交的子集分给不同 worker（那套机制人为制造了论文数据
         结构不支持的 D_i != D_j）。

    因此本函数对每一个 family：让**所有** worker 都绑定到同一个
    family_id（复用已有 `benchmark.curriculum.FamilyCurriculumSampler`，
    不新增 sampler），从空技能库开始，再进入下一个 family——
    20 个 family 各自独立、互不污染技能库。

    Round 数处理（Experimental Protocol Fix — TASK3）：
    论文按 family 自身的任务序列长度决定 round 数（Table 6 显示不同 family
    为 8 或 9 轮）。原实现用全局 `rounds_cap`（配置文件 `rounds` 字段）
    对所有 family 取 `min(rounds_cap, len(family.tasks))`，会静默把
    9 个任务的 family（DMAIC-Quality-Analysis/Financial-Statement-Rolling/
    Healthcare-Cost-Benefit-Analysis/Medical-Data-Standardization/
    Production-Capacity-Planning/Supply-Chain-Replenishment 共 6 个，已用
    脚本核实）截断到 8 轮，丢弃最后 1 个（通常也是最难的）任务。
    现按 `cfg["rounds_per_family_mode"]` 选择：
      - "family_length"（默认）：family_rounds = len(family.tasks)，
        与论文协议一致，`rounds_cap` 被忽略。
      - "fixed_cap"：显式 opt-in 的工程覆盖，family_rounds =
        min(rounds_cap, len(family.tasks))，发生截断时打印 WARNING
        （不是静默截断），仅用于快速验证，不代表论文协议。

    Returns:
        跨所有 family 汇总的 ExperimentResult（rounds 字段留空，
        per-family 明细见 <output_dir>/families/<family_id>/ 和顶层
        experiment_summary.json；Phase2 会在 evaluation/paper_export.py
        里把这些明细整理成 Table1/Figure2/Figure3 需要的格式）。
    """
    worker_ids = [wc["client_id"] for wc in worker_cfgs]
    family_ids = sorted(families.keys())
    n_families = len(family_ids)
    if n_families == 0:
        raise ValueError("families 为空，无法按 family 循环")

    rounds_mode = str(cfg.get("rounds_per_family_mode", "family_length"))
    if rounds_mode not in ("family_length", "fixed_cap"):
        raise ValueError(
            f"未知 rounds_per_family_mode={rounds_mode!r}。合法值: "
            f"'family_length'（默认，round 数 = 该 family 的任务数，与论文一致）/ "
            f"'fixed_cap'（显式覆盖，round 数 = min(rounds, len(family.tasks))，"
            f"会有意截断任务，仅用于工程验证）。"
        )

    shared_lib_root_name = "libraries"
    family_results: dict[str, ExperimentResult] = {}
    # Result Reconstruction Audit（Table 1）新增：记录执行失败的 family 及
    # 失败原因，纯审计/健壮性用途，不影响任何 family 内部的演化逻辑。
    failed_families: dict[str, str] = {}
    family_task_stats: dict[str, dict[str, int | float]] = {}
    # Result Reproduction Readiness Audit TASK1/TASK6 新增：worker/server
    # 运行时元数据，只在第一个 family（idx==1）构造 profile/backbone 时
    # 采样一次——所有 family 复用同一份 worker_cfgs/server_cfg，构造出的
    # WorkerProfile/LLMBackbone 在各 family 间是等价的，没必要每个 family
    # 都重复记录。
    worker_metadata: dict[str, dict[str, Any]] = {}
    server_metadata: dict[str, Any] = {}
    t_loop_start = time.monotonic()
    runtime_mode = _runtime_mode_label(mock, mock_federated)

    for idx, family_id in enumerate(family_ids, start=1):
        family = families[family_id]
        n_tasks = len(family.tasks)
        if rounds_mode == "family_length":
            family_rounds = n_tasks
        else:  # "fixed_cap"：显式配置覆盖，非论文默认协议
            family_rounds = min(rounds_cap, n_tasks)
            if family_rounds < n_tasks:
                logger.warning(
                    "[rounds_per_family_mode=fixed_cap] family=%s 共 %d 个任务，被 "
                    "显式 cap 到 %d 轮，最后 %d 个任务不会被执行——这是配置文件主动 "
                    "覆盖论文协议（论文按 family 任务数决定 round 数），不是静默截断。",
                    family_id, n_tasks, family_rounds, n_tasks - family_rounds,
                )
        cap_note = "" if family_rounds == n_tasks else f" (capped from {n_tasks} tasks)"
        print(f"Running family {idx}/{n_families} ({family_id}) — {family_rounds} rounds{cap_note}")

        # 每个 family 独立一个 sampler 实例，作用域内只含这一个 family，
        # 从物理上排除"跨 family 混淆"的可能性（不是靠约定，是靠数据结构）。
        sampler = FamilyCurriculumSampler({family_id: family}, seed=seed)
        for wid in worker_ids:
            sampler.assign_family(wid, family_id)
        # [state-leak guard / TASK2] sampler 的任务池必须只含当前 family，
        # 用真实数据结构断言，而不是仅凭代码约定信任"不会串"。
        assert set(sampler._families.keys()) == {family_id}, (  # noqa: SLF001（内部一致性断言）
            f"[state-leak guard] family={family_id} 迭代中 sampler 意外持有其他 "
            f"family 的任务池: {sorted(sampler._families.keys())}"
        )

        family_output_dir = output_dir / "families" / family_id
        family_output_dir.mkdir(parents=True, exist_ok=True)

        router = BackboneRouter()
        clients: list[FederatedClient] = []
        profiles: dict[str, WorkerProfile] = {}
        shared_lib_root = (
            family_output_dir / shared_lib_root_name / "shared" if shared_library else None
        )

        for wc in worker_cfgs:
            profile = _build_worker_profile(wc)
            backbone = _build_backbone(wc, role=profile.client_id, mock=mock, mock_federated=mock_federated)
            router.register(profile.client_id, backbone)
            profiles[profile.client_id] = profile
            if idx == 1:
                worker_metadata[profile.client_id] = _worker_runtime_metadata(
                    profile, backbone, mock or mock_federated, runtime_mode=runtime_mode
                )

            # 技能库按 family 隔离（每个 family 从空库开始），对应论文
            # "agents are initialized with an empty skill library" 的
            # per-family 语义，而不是让技能在 20 个不相关 family 间累积。
            library_root = shared_lib_root or (
                family_output_dir / shared_lib_root_name / profile.client_id
            )
            # [state-leak guard / TASK2] 空技能库初始化断言：新 family 的
            # library_root 不应已经含有任何文件（否则说明目录被跨 family
            # 复用，技能会在不相关 family 间累积，违反论文假设）。
            if library_root.exists():
                leftover = list(library_root.rglob("*"))
                assert not leftover, (
                    f"[state-leak guard] family={family_id} worker={profile.client_id} 的 "
                    f"library_root={library_root} 初始化前已有 {len(leftover)} 个文件，"
                    f"违反'每个 family 从空技能库开始'的论文假设。"
                )
            clients.append(FederatedClient(profile=profile, library_root=library_root, router=router))

        executor = _build_executor(router, execution_mode)

        if not federated:
            runner = SelfEvolutionRunner(
                clients=clients,
                executor=executor,
                sampler=sampler,
                rounds=family_rounds,
                setting_name=f"{setting_name}[{family_id}]",
                disable_patch_distillation=disable_patch_distillation,
                distillation_failure_mode=distillation_failure_mode,
                output_dir=family_output_dir,
                max_retry=max_retry,
            )
        else:
            server_cfg = cfg.get("server")
            if not server_cfg:
                raise ValueError("federated=true 时必须在配置中提供 server 节点")
            server_backbone = _build_backbone(server_cfg, role="server", mock=mock, mock_federated=mock_federated)
            if idx == 1:
                server_metadata.update(
                    _server_runtime_metadata(
                        server_cfg, server_backbone, mock or mock_federated,
                        runtime_mode=runtime_mode,
                    )
                )
            server = FederatedServer.create(
                server_backbone=server_backbone,
                family_name=family_id,
                worker_profiles=profiles,
            )
            # [state-leak guard / TASK2] FederatedServer.create() 每次都新建
            # CapabilityTracker/EvolutionMemoryStore 实例（见 server/evolution.py），
            # 这里用真实数据断言验证，而不是只信任"应该是新实例"的代码约定。
            assert server.current_capability.to_dict() == {}, (
                f"[state-leak guard] family={family_id} 的 capability matrix 初始化后"
                f"非空，违反'每个 family 独立能力矩阵'假设。"
            )
            _mem_snapshot = server.memory_store.to_dict()
            assert _mem_snapshot["high_level"]["last_updated_round"] == -1, (
                f"[state-leak guard] family={family_id} 的 high-level memory 初始化后"
                f"已被更新过（last_updated_round="
                f"{_mem_snapshot['high_level']['last_updated_round']}），违反"
                f"'每个 family 独立记忆'假设。"
            )
            assert all(
                v["last_updated_round"] == -1 for v in _mem_snapshot["low_level"].values()
            ), f"[state-leak guard] family={family_id} 的 low-level memory 初始化后已被更新过。"
            runner = FederatedRunner(
                clients=clients,
                server=server,
                executor=executor,
                sampler=sampler,
                rounds=family_rounds,
                setting_name=f"{setting_name}[{family_id}]",
                disable_capability_matrix=disable_capability_matrix,
                disable_patch_distillation=disable_patch_distillation,
                output_dir=family_output_dir,
                distillation_failure_mode=distillation_failure_mode,
                max_retry=max_retry,
            )

        t_family_start = time.monotonic()
        # Result Reconstruction Audit（Table 1 审计新增）：此前这里没有任何
        # try/except——单个 family 的 runner.run() 抛异常（如真实 API 调用
        # 失败）会让整个多 family 循环直接崩溃退出整个进程，已经跑完的
        # family 的 round_*_summary.json 虽然还在磁盘上，但
        # _save_family_loop_summary() 只在 for 循环整体跑完后才被调用，
        # 因此 experiment_summary.json（table1.csv 的数据来源）永远不会
        # 写出，Table 1 "是否正确统计失败的 family" 这一项因此不成立。
        # 这里改为捕获异常、记录失败原因后继续下一个 family，不改变
        # Client/Server 内部任何演化/合并逻辑，只让循环本身更健壮、可审计。
        try:
            family_result = runner.run()
        except Exception as exc:  # noqa: BLE001 - 需要捕获任意第三方/LLM异常以保护循环
            logger.error("family=%s 执行失败，记为失败并继续下一个 family: %s", family_id, exc)
            failed_families[family_id] = f"{type(exc).__name__}: {exc}"
            # [Experiment Isolation Fix] family 失败时立即清理该 family 的
            # skill library（否则残留文件会在下次重跑同一 family 时触发
            # 上面的 state-leak guard 断言），并尽力保留失败日志/超时原因/
            # 已累积的部分 cost 日志——只在异常处理路径调用，不影响成功
            # 路径的任何行为。
            family_failure_cleanup(
                family_id=family_id,
                family_output_dir=family_output_dir,
                worker_ids=worker_ids,
                shared_library=shared_library,
                exc=exc,
                elapsed_seconds=time.monotonic() - t_family_start,
                runner=runner,
            )
            family_task_stats[family_id] = collect_task_checkpoint_stats(
                family_output_dir, family_rounds * len(worker_ids)
            )
            continue
        family_elapsed = time.monotonic() - t_family_start
        _save_results(
            family_result, family_output_dir, family_elapsed,
            capability_history=getattr(runner, "capability_history", None),
        )
        family_results[family_id] = family_result
        family_task_stats[family_id] = collect_task_checkpoint_stats(
            family_output_dir, family_rounds * len(worker_ids)
        )

    loop_elapsed = time.monotonic() - t_loop_start
    reproducibility = (
        _build_reproducibility_metadata(config_path, seed, worker_metadata, server_metadata or None)
        if config_path is not None else None
    )
    _save_family_loop_summary(
        family_results, output_dir, loop_elapsed, failed_families=failed_families,
        workers=worker_metadata, server=(server_metadata or None), reproducibility=reproducibility,
        family_task_stats=family_task_stats,
        max_retry=max_retry,
        family_ids=family_ids,
    )

    completed_tasks = sum(int(s["completed_tasks"]) for s in family_task_stats.values())
    total_tasks = sum(int(s["total_tasks"]) for s in family_task_stats.values())
    task_success_rate = completed_tasks / total_tasks if total_tasks else 0.0
    return ExperimentResult(
        setting_name=setting_name,
        rounds=[],
        final_metrics={
            "success_rate": task_success_rate,
            "completed_tasks": completed_tasks,
            "total_tasks": total_tasks,
            "n_families": n_families,
            "n_failed_families": len(failed_families),
        },
        metadata={
            "mode": "family_loop",
            "family_ids": family_ids,
            "elapsed_seconds": loop_elapsed,
            "failed_families": failed_families,
        },
    )


def _save_family_loop_summary(
    family_results: dict[str, ExperimentResult],
    output_dir: Path,
    elapsed: float,
    failed_families: dict[str, str] | None = None,
    workers: dict[str, dict[str, Any]] | None = None,
    server: dict[str, Any] | None = None,
    reproducibility: dict[str, Any] | None = None,
    family_task_stats: dict[str, dict[str, int | float]] | None = None,
    max_retry: int = 0,
    family_ids: list[str] | None = None,
) -> None:
    """把按 family 循环的汇总写到 <output_dir>/experiment_summary.json。

    per-family 明细（每个 family 完整的 round_*_summary.json）已经在
    <output_dir>/families/<family_id>/ 下由 _save_results() 写出；这里只
    额外写一份跨 family 的汇总索引，供 Phase2 的 CSV 导出直接读取，不重复
    存储 per-round 明细。

    Args:
        failed_families: Result Reconstruction Audit（Table 1）新增，
            {family_id: 失败原因字符串}，来自 _run_family_loop() 里对
            runner.run() 的 try/except；默认 None 等价于空字典（保持向后
            兼容，旧调用方/已有测试不受影响）。写入 summary 的
            "failed_families" 键 + "n_failed_families" 计数，使 Table 1 的
            "失败 family 是否被正确统计"这一检查项有据可查——不影响
            "families"/"n_families"/"mean_success_rate" 这些既有字段的
            计算口径（仍只统计成功完成的 family）。
        workers: Result Reproduction Readiness Audit TASK1 新增，
            {worker_id: {backbone_model, agent_harness, provider,
            temperature, max_tokens}}，由 _worker_runtime_metadata() 从
            真实构造好的 WorkerProfile/LLMBackbone 读出。默认 None 等价于
            空字典，纯新增键，不影响任何既有字段。
        server: TASK1 新增，联邦设置下服务器 backbone 的同类元数据；
            非联邦设置（Setting1 SE）为 None，写入 summary 时省略该键。
        reproducibility: TASK6 新增，{seed, timestamp, config_path,
            config_hash, model_name, api_provider}，由
            _build_reproducibility_metadata() 组装。默认 None 等价于
            空字典。
    """
    failed_families = failed_families or {}
    family_task_stats = family_task_stats or {}
    families_summary = {
        fid: {
            "final_success_rate": r.final_success_rate,
            "rounds": len(r.rounds),
            "success_rates": r.success_rates,
            "library_sizes": r.library_sizes,
        }
        for fid, r in family_results.items()
    }
    completed_tasks = sum(int(s["completed_tasks"]) for s in family_task_stats.values())
    total_tasks = sum(int(s["total_tasks"]) for s in family_task_stats.values())
    task_success_rate = completed_tasks / total_tasks if total_tasks else 0.0
    summary = {
        "mode": "family_loop",
        "n_families": len(families_summary),
        "elapsed_seconds": elapsed,
        "mean_success_rate": task_success_rate,
        "success_rate": task_success_rate,
        "completed_tasks": completed_tasks,
        "total_tasks": total_tasks,
        "task_metrics_by_family": family_task_stats,
        "retry_policy": {"max_retry": max_retry, "max_attempts": max_retry + 1},
        # [Runtime Protocol Alignment Issue6] 论文 Table/Appendix 的官方 family
        # 顺序在本环境不可获取（arXiv 抓取失败、无本地 PDF、官方仓库未见排序
        # 列表），采用代码自身确定性的 sorted(family_names) 作为非随机替代，
        # 如实记录依据，供审计追溯。
        "family_order": "sorted_by_name",
        "family_order_reason": "official_order_unavailable",
        "family_ids_in_order": family_ids or sorted(families_summary.keys()),
        "families": families_summary,
        "failed_families": failed_families,
        "n_failed_families": len(failed_families),
        "workers": workers or {},
    }
    if server is not None:
        summary["server"] = server
    if reproducibility is not None:
        summary["reproducibility"] = reproducibility
    path = output_dir / "experiment_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[family_loop] 已写入跨 family 汇总: {path}")
    if failed_families:
        print(f"[family_loop] 有 {len(failed_families)} 个 family 执行失败: {list(failed_families.keys())}")


# ===========================================================================
# 单 family 独立实验入口（--family）
#
# 论文 Section 5 的实验单位是"单个 family 独立运行"：每次运行只跑一个
# family，从空技能库开始，family 内任务保持官方顺序、不裁剪，且每次运行
# 都必须是全新的 experiment_id、不能复用旧结果。本节复用已经过充分审计/
# 测试的 `_run_family_loop()`（不改动其内部任何 state-leak guard/清理/
# checkpoint 逻辑），只是把 families 字典收窄到一个 family、并把输出目录
# 换成按 experiment_id 隔离的全新目录，然后在其基础上"物化"出用户要求的
# trajectories/patches/libraries/metrics 四个语义化子目录（复制而非移动，
# 不删除 `_run_family_loop()` 原有的 families/<family_id>/ 明细，避免破坏
# 其既有断言/清理路径）。
# ===========================================================================

def _generate_experiment_id(family_id: str) -> str:
    """生成本次运行专属的 experiment_id，保证跨运行不重复。"""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{_safe_family_component(family_id)}_{uuid.uuid4().hex[:8]}"


def _safe_family_component(family_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", family_id).strip("._") or "family"


def _assert_fresh_experiment_dir(run_root: Path) -> None:
    """[不允许复用旧结果] experiment_id 对应目录必须是全新的，不允许已存在。"""
    if run_root.exists():
        raise RuntimeError(
            f"[不允许复用旧结果] experiment_id 目录已存在: {run_root}。"
            f"每次运行必须生成全新的 experiment_id，不允许复用/覆盖旧结果目录。"
        )


def _preflight_family_run_checks(run_root: Path, family_id: str) -> None:
    """
    启动前检查（用户要求）：library / trajectory / metrics 必须为空。

    `run_root` 由 `_assert_fresh_experiment_dir()` 保证是全新目录，因此这里
    的检查在正常路径下必然通过；仍显式执行并 fail-loud，用于覆盖
    "experiment_id 生成后、真正开始跑 family 之前"这一时间窗口内任何
    意外的目录残留（例如外部脚本手工复用了 run_root），不依赖"应该是空的"
    这种口头约定。
    """
    family_root = run_root / "families" / family_id
    materialized_root = run_root / family_id
    check_dirs = {
        "library": family_root / "libraries",
        "trajectory": materialized_root / "trajectories",
        "metrics": materialized_root / "metrics",
    }
    for label, check_dir in check_dirs.items():
        if check_dir.exists() and any(check_dir.rglob("*")):
            raise RuntimeError(
                f"[state-leak guard] 启动前检查失败: {label} 目录 {check_dir} 非空，"
                f"检测到旧结果残留，禁止复用。"
            )
    print("Pre-flight checks:")
    print("  library: empty")
    print("  trajectory: empty")
    print("  metrics: empty")


def _materialize_family_result_layout(run_root: Path, family_id: str) -> Path:
    """
    把 `_run_family_loop()` 写出的 `run_root/families/<family_id>/` 原始明细，
    复制整理成用户要求的目标布局：

      run_root/<family_id>/
        trajectories/<worker_id>/round_XXX_<task>.json
        patches/<worker_id>/round_XXX_<task>.json
        libraries/<worker_id>/...          (family 结束时的技能库快照)
        metrics/round_XX_summary.json ...  + experiment_summary.json

    只做复制（`shutil.copy2`/`copytree`），不删除/不移动原始
    `families/<family_id>/` 目录——避免影响 `_run_family_loop()` 内部已有的
    state-leak guard（下次同一个 family 重跑会用全新 experiment_id、全新
    run_root，不会与本次目录冲突）。

    Returns:
        物化后的目标目录 `run_root/<family_id>`。
    """
    source_root = run_root / "families" / family_id
    dest_root = run_root / family_id
    trajectories_dir = dest_root / "trajectories"
    patches_dir = dest_root / "patches"
    libraries_dir = dest_root / "libraries"
    metrics_dir = dest_root / "metrics"
    for d in (trajectories_dir, patches_dir, libraries_dir, metrics_dir):
        d.mkdir(parents=True, exist_ok=True)

    # libraries：family 结束时的技能库快照（若 family 执行失败，
    # family_failure_cleanup() 已把该目录物理删除，这里保持空目录即可）。
    source_libraries = source_root / "libraries"
    if source_libraries.exists():
        shutil.copytree(source_libraries, libraries_dir, dirs_exist_ok=True)

    # trajectories / patches：从 workers/<worker>/tasks/round_*_*/ 里按文件名拆分
    source_workers = source_root / "workers"
    if source_workers.exists():
        for worker_dir in sorted(p for p in source_workers.iterdir() if p.is_dir()):
            tasks_dir = worker_dir / "tasks"
            if not tasks_dir.exists():
                continue
            for task_dir in sorted(p for p in tasks_dir.iterdir() if p.is_dir()):
                traj_src = task_dir / "trajectory.json"
                if traj_src.exists():
                    dest = trajectories_dir / worker_dir.name / f"{task_dir.name}.json"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(traj_src, dest)
                patch_src = task_dir / "patch.json"
                if patch_src.exists():
                    dest = patches_dir / worker_dir.name / f"{task_dir.name}.json"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(patch_src, dest)

    # metrics：per-round summary + 跨 family（此处只含 1 个 family）的汇总
    for round_summary in sorted(source_root.glob("round_*_summary.json")):
        shutil.copy2(round_summary, metrics_dir / round_summary.name)
    top_summary = run_root / "experiment_summary.json"
    if top_summary.exists():
        shutil.copy2(top_summary, metrics_dir / "experiment_summary.json")
    failure_json = source_root / "family_failure.json"
    if failure_json.exists():
        shutil.copy2(failure_json, metrics_dir / "family_failure.json")

    return dest_root


def run_family_experiment(
    config_path: Path,
    family_id: str,
    rounds_override: int | None = None,
    results_root_override: Path | None = None,
    dry_run: bool = False,
    mock: bool = False,
    mock_federated: bool = False,
    distillation_failure_mode: str = "strict",
    execution_mode: str = "api",
) -> ExperimentResult | None:
    """
    单 family 独立实验入口（对应 `python run_experiment.py --family xxx`）。

    严格遵守论文 Section 5 的 family-level 独立执行协议：
      - 只运行 `family_id` 指定的这一个 family（不触碰其它 family）。
      - 该 family 从空技能库开始（复用 `_run_family_loop()` 里已有的
        state-leak guard，未新增/未放宽任何断言）。
      - family 内任务顺序保持官方顺序、不裁剪（复用
        `rounds_per_family_mode="family_length"` 默认协议）。
      - 每次运行生成全新的 `experiment_id`，不允许复用旧结果目录。

    Args:
        config_path: YAML 实验配置（必须 `sampler: family_curriculum`，
            与 `run_experiment()` 里 `loop_over_families=true` 分支要求一致）。
        family_id: 要运行的单个 family（如 "Cross-Format-Data-Reconciliation"）。
        rounds_override: 覆盖 `rounds_per_family_mode=fixed_cap` 时的 cap
            轮数；默认协议（"family_length"）下会被忽略。
        results_root_override: 覆盖默认的 `results/` 根目录（用于测试）。
        dry_run: 仅打印将要运行的配置摘要，不发起任何 LLM 调用。

    Returns:
        `_run_family_loop()` 返回的跨（此处仅 1 个）family 汇总
        `ExperimentResult`；`dry_run=True` 时返回 None。
    """
    _load_project_dotenv()
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")

    cfg = _load_yaml(config_path)
    _validate_experiment_config(cfg)

    if cfg.get("sampler") != "family_curriculum":
        raise ValueError(
            f"--family 要求配置文件 sampler='family_curriculum'（与 "
            f"loop_over_families 主实验协议一致），但 {config_path} 里 "
            f"sampler={cfg.get('sampler')!r}。请使用 setting_se.yaml 等 "
            f"family-loop 配置文件。"
        )

    setting_name: str = cfg.get("setting_name", "unknown_setting")
    federated: bool = bool(cfg.get("federated", False))
    rounds: int = rounds_override if rounds_override is not None else int(cfg.get("rounds", 8))
    seed: int = int(cfg.get("seed", 42))

    families_dir = Path(cfg.get("families_dir", str(_REPO_ROOT / "benchmark" / "families")))
    if not families_dir.is_absolute():
        families_dir = _REPO_ROOT / families_dir
    families = load_all_families(directory=families_dir)
    families = _apply_paper_benchmark_scope(cfg, families)
    families = _apply_family_subset(cfg, families)

    if family_id not in families:
        raise ValueError(
            f"--family={family_id!r} 不在当前配置可用的 family 列表中。"
            f"可用 family（共 {len(families)} 个）: {sorted(families.keys())}"
        )
    selected_families = {family_id: families[family_id]}

    worker_cfgs: list[dict] = cfg.get("workers", [])
    if not worker_cfgs:
        raise ValueError("配置文件中 workers 列表为空")

    abl = cfg.get("ablation", {}) or {}
    disable_capability_matrix: bool = bool(
        cfg.get("disable_capability_matrix", abl.get("disable_capability_matrix", False))
    )
    shared_library: bool = bool(
        cfg.get("shared_library", False)
        or abl.get("disable_personalization", False)
        or (cfg.get("isolated_worker_skills") is False)
    )
    disable_patch_distillation: bool = bool(
        cfg.get("skip_patch_distillation", abl.get("skip_patch_distillation", False))
        or cfg.get("disable_patch_distillation", abl.get("disable_patch_distillation", False))
    )

    experiment_id = _generate_experiment_id(family_id)
    results_root = Path(results_root_override) if results_root_override is not None else (_REPO_ROOT / "results")
    run_root = results_root / experiment_id
    runtime_mode = _runtime_mode_label(mock, mock_federated)

    print("=" * 60)
    print("[--family] 单 family 独立实验")
    print(f"  Family:        {family_id}")
    print(f"  Experiment ID: {experiment_id}")
    print(f"  Tasks:         {len(selected_families[family_id].tasks)} (完整，不裁剪)")
    print(f"  Setting:       {setting_name}")
    print(f"  Federated:     {federated}")
    print(f"  Runtime mode:  {runtime_mode}")
    print(f"  Output root:   {run_root}")
    print("=" * 60)
    if runtime_mode != "api":
        print(f"[注意] 当前是 {runtime_mode}，不会调用真实 LLM，结果不能作为真实论文复现指标。")

    if dry_run:
        print("[DRY-RUN] 不执行 LLM 调用，仅打印以上摘要。")
        return None

    # [不允许复用旧结果] experiment_id 对应目录必须是全新目录。
    _assert_fresh_experiment_dir(run_root)
    run_root.mkdir(parents=True)
    _preflight_family_run_checks(run_root, family_id)

    result = _run_family_loop(
        cfg=cfg,
        families=selected_families,
        worker_cfgs=worker_cfgs,
        output_dir=run_root,
        rounds_cap=rounds,
        seed=seed,
        federated=federated,
        setting_name=setting_name,
        mock=mock,
        disable_capability_matrix=disable_capability_matrix,
        shared_library=shared_library,
        disable_patch_distillation=disable_patch_distillation,
        config_path=config_path,
        distillation_failure_mode=distillation_failure_mode,
        execution_mode=execution_mode,
        mock_federated=mock_federated,
        max_retry=int(cfg.get("max_retry", 0)),
    )

    dest_root = _materialize_family_result_layout(run_root, family_id)
    print(f"\n[--family] family={family_id} 结果已物化到: {dest_root}")
    print(f"  trajectories/  patches/  libraries/  metrics/")

    return result


def run_family_batch_experiments(
    config_path: Path,
    family_ids: list[str],
    rounds_override: int | None = None,
    results_root_override: Path | None = None,
    dry_run: bool = False,
    mock: bool = False,
    mock_federated: bool = False,
    distillation_failure_mode: str = "strict",
    execution_mode: str = "api",
    continue_on_error: bool = True,
) -> dict[str, Any]:
    """
    顺序运行多个 family，每个 family 仍走 `run_family_experiment()` 的独立
    experiment_id / 空库 preflight / 结果物化协议。

    这是面向"逐个 family 一个一个跑"的薄封装入口：只负责把 family 名称
    列表排成队列并记录 batch manifest，不复制、不修改 `_run_family_loop()`
    或任何 client/server/evolution 算法逻辑。

    Args:
        config_path: YAML 实验配置（必须 `sampler: family_curriculum`）。
        family_ids: 要依次运行的 family id 列表，顺序即执行顺序。
        continue_on_error: True（默认）时单个 family 失败后继续跑后续 family，
            并在 manifest 里记录失败；False 时遇到第一个失败直接向上抛出。

    Returns:
        batch manifest 字典；非 dry-run 时同时写入
        `<results_root>/family_batch_manifest.json`。
    """
    _load_project_dotenv()
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")

    cfg = _load_yaml(config_path)
    _validate_experiment_config(cfg)
    if cfg.get("sampler") != "family_curriculum":
        raise ValueError(
            f"批量 family 运行要求配置文件 sampler='family_curriculum'，但 "
            f"{config_path} 里 sampler={cfg.get('sampler')!r}。"
        )

    families_dir = Path(cfg.get("families_dir", str(_REPO_ROOT / "benchmark" / "families")))
    if not families_dir.is_absolute():
        families_dir = _REPO_ROOT / families_dir
    families = load_all_families(directory=families_dir)
    families = _apply_paper_benchmark_scope(cfg, families)
    families = _apply_family_subset(cfg, families)

    if not family_ids:
        raise ValueError("family_ids 不能为空；命令行请使用 --families A,B 或 --all-families。")
    unknown = [fid for fid in family_ids if fid not in families]
    if unknown:
        raise ValueError(
            f"以下 family 不在当前配置可用列表中: {unknown}。"
            f"可用 family（共 {len(families)} 个）: {sorted(families.keys())}"
        )

    results_root = Path(results_root_override) if results_root_override is not None else (_REPO_ROOT / "results")
    manifest: dict[str, Any] = {
        "mode": "family_batch",
        "config_path": str(config_path),
        "results_root": str(results_root),
        "dry_run": dry_run,
        "runtime_mode": _runtime_mode_label(mock, mock_federated),
        "family_ids_in_order": family_ids,
        "n_families": len(family_ids),
        "families": {},
    }

    print("=" * 60)
    print("[family-batch] 逐 family 独立运行队列")
    print(f"  Config:       {config_path}")
    print(f"  Families:     {len(family_ids)}")
    print(f"  Runtime mode: {manifest['runtime_mode']}")
    print(f"  Results root: {results_root}")
    print("=" * 60)
    if manifest["runtime_mode"] != "api":
        print(f"[注意] 当前是 {manifest['runtime_mode']}，不会调用真实 LLM，结果不能作为真实论文复现指标。")

    if not dry_run:
        results_root.mkdir(parents=True, exist_ok=True)

    for idx, family_id in enumerate(family_ids, start=1):
        print(f"\n[family-batch] ({idx}/{len(family_ids)}) family={family_id}")
        before_dirs = {p for p in results_root.iterdir() if p.is_dir()} if results_root.exists() else set()
        try:
            run_family_experiment(
                config_path=config_path,
                family_id=family_id,
                rounds_override=rounds_override,
                results_root_override=results_root,
                dry_run=dry_run,
                mock=mock,
                mock_federated=mock_federated,
                distillation_failure_mode=distillation_failure_mode,
                execution_mode=execution_mode,
            )
            after_dirs = {p for p in results_root.iterdir() if p.is_dir()} if results_root.exists() else set()
            new_dirs = sorted(after_dirs - before_dirs)
            manifest["families"][family_id] = {
                "status": "DRY_RUN" if dry_run else "OK",
                "experiment_dir": str(new_dirs[0]) if len(new_dirs) == 1 else None,
            }
        except Exception as exc:  # noqa: BLE001 — batch 模式需要记录单 family 失败并继续
            after_dirs = {p for p in results_root.iterdir() if p.is_dir()} if results_root.exists() else set()
            new_dirs = sorted(after_dirs - before_dirs)
            manifest["families"][family_id] = {
                "status": "FAILED",
                "experiment_dir": str(new_dirs[0]) if len(new_dirs) == 1 else None,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
            logger.exception("[family-batch] family=%s 运行失败", family_id)
            if not continue_on_error:
                raise

    statuses = [info["status"] for info in manifest["families"].values()]
    manifest["n_succeeded"] = sum(1 for s in statuses if s == "OK")
    manifest["n_failed"] = sum(1 for s in statuses if s == "FAILED")
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()

    if not dry_run:
        manifest_path = results_root / "family_batch_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[family-batch] manifest 已写入: {manifest_path}")
        print(f"  OK={manifest['n_succeeded']}  FAILED={manifest['n_failed']}")
    else:
        print("\n[family-batch] DRY-RUN 完成：未创建结果目录/manifest。")

    return manifest


# ===========================================================================
# 主运行逻辑
# ===========================================================================

def run_experiment(
    config_path: Path,
    rounds_override: int | None = None,
    output_dir_override: Path | None = None,
    dry_run: bool = False,
    plot: bool = False,
    mock: bool = False,
    distillation_failure_mode: str = "strict",
    execution_mode: str = "api",
    mock_federated: bool = False,
) -> ExperimentResult | None:
    """
    从 YAML 配置文件加载并运行完整实验（Algorithm 1）。

    Args:
        config_path:         配置文件路径（绝对或相对路径）
        rounds_override:     覆盖配置中的轮数
        output_dir_override: 覆盖配置中的输出目录
        dry_run:              仅打印配置，不执行
        plot:                 实验结束后生成 Figure 2/3/4
        mock:                 使用 mock backbone（不发出任何真实 API 请求，
            不需要 API Key）实际跑完整流程，用于验证 family 循环/round
            单调/不混淆等结构正确性（与 --dry-run 不同：--dry-run 只打印
            配置摘要就返回，--mock 会真实跑完整个循环）。
        mock_federated: "FederatedSkill Faithful Mock Validation" TASK1
            新增。True 时改用 `_build_faithful_mock_backbone()`（PatchDistiller/
            Stage1/Stage2 三个调用方都返回 schema 合法且内容非空的 payload），
            用于验证 skill evolution 全链路（技能库/capability_matrix/
            directive/transfer 均非空），而不仅仅是 `--mock` 原本验证的
            family 循环结构。优先级高于 mock（同时为 True 时走 faithful
            分支）；不要求同时传入 mock=True。
        distillation_failure_mode: "strict"（默认）或 "audit"（Experiment
            Integrity Hardening TASK1）。strict 模式下 patch 蒸馏 LLM 调用
            失败会直接终止实验（防止静默污染论文结果）；audit 模式仅用于
            调试/对比，不得用于正式论文结果。
        execution_mode: "api"（默认，向后兼容——与本参数新增之前完全一致的
            行为，构造 executor.router_executor.VerificationAwareExecutor）
            或 "cli"（Real CLI Harness Fidelity Fix 新增，构造
            executor.harness_executor.HarnessAwareExecutor(mode="strict")，
            按每个 worker 的 agent_harness 真实 spawn claude/qwen-code/kimi
            CLI 二进制，见 harness/ 包）。默认值刻意保持 "api" 而不是论文
            语义上的"更贴近论文"的 "cli"，只是为了不改变本参数新增之前
            所有既有调用方/测试的行为——要切换到真实 CLI，必须显式传入
            execution_mode="cli"（或 run.py 的 --execution-mode cli）。

    Returns:
        ExperimentResult（干跑时返回 None）
    """
    _load_project_dotenv()
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")

    cfg = _load_yaml(config_path)

    # Experiment Integrity Hardening TASK3：在任何构建 sampler/worker/backbone
    # 之前早期校验，不合法时 fail-loud 直接终止（--dry-run 也会触发）。
    _validate_experiment_config(cfg)

    if "tasks_path" in cfg:
        logger.warning(
            "配置文件 %s 中的 'tasks_path' 字段已废弃，本入口从不读取它——"
            "任务始终从 'families_dir'（默认 benchmark/families/）加载。"
            "请从配置文件中移除 tasks_path，避免误导。",
            config_path,
        )

    if dry_run:
        _dry_run(cfg)
        return None

    # ── 参数覆盖 ─────────────────────────────────────────────────────────────
    setting_name: str = cfg.get("setting_name", "unknown_setting")
    federated: bool = bool(cfg.get("federated", False))
    rounds: int = rounds_override if rounds_override is not None else int(cfg.get("rounds", 8))
    seed: int = int(cfg.get("seed", 42))

    output_dir = Path(
        output_dir_override or cfg.get("output_dir", f"results/{setting_name}")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "实验启动: setting=%s federated=%s rounds=%d seed=%d output=%s",
        setting_name, federated, rounds, seed, output_dir,
    )

    # ── 任务 family 加载 ──────────────────────────────────────────────────────
    # families_dir 优先读取 config 中的 families_dir 字段，否则用默认目录
    families_dir = Path(cfg.get("families_dir", str(_REPO_ROOT / "benchmark" / "families")))
    if not families_dir.is_absolute():
        families_dir = _REPO_ROOT / families_dir
    families = load_all_families(directory=families_dir)
    families = _apply_paper_benchmark_scope(cfg, families)
    families = _apply_family_subset(cfg, families)

    sampler = _build_sampler(cfg, families)

    # ── Worker 构建 ───────────────────────────────────────────────────────────
    worker_cfgs: list[dict] = cfg.get("workers", [])
    if not worker_cfgs:
        raise ValueError("配置文件中 workers 列表为空")

    # ── Ablation flags ──────────────────────────────────────────────────────
    # 同时支持顶层字段和 ablation: 子键（向后兼容两种写法）
    abl = cfg.get("ablation", {}) or {}
    disable_capability_matrix: bool = bool(
        cfg.get("disable_capability_matrix", abl.get("disable_capability_matrix", False))
    )
    shared_library: bool = bool(
        cfg.get("shared_library", False)
        or abl.get("disable_personalization", False)
        or (cfg.get("isolated_worker_skills") is False)
    )
    disable_patch_distillation: bool = bool(
        cfg.get("skip_patch_distillation", abl.get("skip_patch_distillation", False))
        or cfg.get("disable_patch_distillation", abl.get("disable_patch_distillation", False))
    )

    if any([disable_capability_matrix, shared_library, disable_patch_distillation]):
        logger.info(
            "Ablation flags: A1(no_cap_matrix)=%s A2(shared_lib)=%s A3(no_distill)=%s",
            disable_capability_matrix, shared_library, disable_patch_distillation,
        )

    # ── Phase1: 按 family 循环（恢复论文原始实验单位） ──────────────────────
    # loop_over_families=true 时，忽略上面已构建的 flat sampler（仍会被构造，
    # 但不使用），走 _run_family_loop() 的独立路径：20 个 family 各自独立
    # 跑一遍，而不是把任务打平/按 category 分组。这是 Setting1-4 主实验应
    # 走的路径；random/heterogeneous 等 flat sampler 仍保留给消融实验用。
    if bool(cfg.get("loop_over_families", False)):
        if cfg.get("sampler") != "family_curriculum":
            raise ValueError(
                "loop_over_families=true 要求 sampler='family_curriculum'，"
                f"但配置文件里 sampler={cfg.get('sampler')!r}。请显式修正配置。"
            )
        result = _run_family_loop(
            cfg=cfg,
            families=families,
            worker_cfgs=worker_cfgs,
            output_dir=output_dir,
            rounds_cap=rounds,
            seed=seed,
            federated=federated,
            setting_name=setting_name,
            mock=mock,
            disable_capability_matrix=disable_capability_matrix,
            shared_library=shared_library,
            disable_patch_distillation=disable_patch_distillation,
            config_path=config_path,
            distillation_failure_mode=distillation_failure_mode,
            execution_mode=execution_mode,
            mock_federated=mock_federated,
            max_retry=int(cfg.get("max_retry", 0)),
        )
        print(f"\n实验完成: {result.setting_name}")
        print(f"  跨 family 平均成功率: {result.final_success_rate:.3f}")
        print(f"  总耗时:     {result.metadata.get('elapsed_seconds', 0):.1f}s")
        print(f"  结果目录:   {output_dir}")
        if plot:
            logger.warning("--plot 尚不支持 loop_over_families 模式（Phase2 待办），已跳过绘图。")
        return result

    router = BackboneRouter()
    clients: list[FederatedClient] = []
    profiles: dict[str, WorkerProfile] = {}
    # Result Reproduction Readiness Audit TASK1/TASK6：扫平模式下的 worker/server
    # 运行时元数据，用途与 _run_family_loop() 里完全一致。
    worker_metadata: dict[str, dict[str, Any]] = {}
    server_metadata: dict[str, Any] = {}
    runtime_mode = _runtime_mode_label(mock, mock_federated)

    # A2 ablation: 所有 worker 共享同一个 library root（模拟 global shared library）
    shared_lib_root = output_dir / "libraries" / "shared" if shared_library else None

    for wc in worker_cfgs:
        profile = _build_worker_profile(wc)
        backbone = _build_backbone(wc, role=profile.client_id, mock=mock, mock_federated=mock_federated)

        router.register(profile.client_id, backbone)
        profiles[profile.client_id] = profile
        worker_metadata[profile.client_id] = _worker_runtime_metadata(
            profile, backbone, mock or mock_federated, runtime_mode=runtime_mode,
        )

        library_root = shared_lib_root or (output_dir / "libraries" / profile.client_id)
        client = FederatedClient(
            profile=profile,
            library_root=library_root,
            router=router,
        )
        clients.append(client)

    # 最终论文一致性收口 Priority 1：按 task.verification.type 在 TaskExecutor /
    # SkillFlowTaskExecutor 之间分派（原来固定用 TaskExecutor，真实 skillflow_script/
    # docker 任务会因验证器接口不匹配而被静默记为 reward=0.0，见 router_executor.py docstring）
    executor = _build_executor(router, execution_mode)

    # ── 运行：SE 基线 vs. 联邦 ────────────────────────────────────────────────
    t_start = time.monotonic()
    if not federated:
        runner = SelfEvolutionRunner(
            clients=clients,
            executor=executor,
            sampler=sampler,
            rounds=rounds,
            setting_name=setting_name,
            disable_patch_distillation=disable_patch_distillation,
            distillation_failure_mode=distillation_failure_mode,
            output_dir=output_dir,
        )
        result = runner.run()
    else:
        server_cfg = cfg.get("server")
        if not server_cfg:
            raise ValueError("federated=true 时必须在配置中提供 server 节点")

        server_backbone = _build_backbone(server_cfg, role="server", mock=mock, mock_federated=mock_federated)
        family_name = server_cfg.get("family_name", "default_family")
        server_metadata.update(
            _server_runtime_metadata(
                server_cfg, server_backbone, mock or mock_federated, runtime_mode=runtime_mode,
            )
        )

        server = FederatedServer.create(
            server_backbone=server_backbone,
            family_name=family_name,
            worker_profiles=profiles,
        )
        runner = FederatedRunner(
            clients=clients,
            server=server,
            executor=executor,
            sampler=sampler,
            rounds=rounds,
            setting_name=setting_name,
            disable_capability_matrix=disable_capability_matrix,
            disable_patch_distillation=disable_patch_distillation,
            output_dir=output_dir,
            distillation_failure_mode=distillation_failure_mode,
        )
        result = runner.run()

    elapsed = time.monotonic() - t_start

    # ── 保存结果 ──────────────────────────────────────────────────────────────
    # Phase14 新增：若 runner 是 FederatedRunner，则附带其 capability_history
    # （跨轮次真实 covered/absorbing/broken/gap 四态记录），用 getattr 容错——
    # SelfEvolutionRunner 没有该属性，返回 None，不影响 Setting1 的既有行为。
    _save_results(
        result, output_dir, elapsed, capability_history=getattr(runner, "capability_history", None),
        workers=worker_metadata,
        server=(server_metadata or None),
        reproducibility=_build_reproducibility_metadata(config_path, seed, worker_metadata, server_metadata or None),
    )

    # ── 可选绘图 ──────────────────────────────────────────────────────────────
    if plot:
        try:
            from evaluation.results_exporter import ResultsExporter
            exporter = ResultsExporter(results_dir=output_dir, output_dir=output_dir / "tables")
            summary = exporter.export_all()
            logger.info(
                "论文结果已导出: %d CSVs, %d figures",
                len(summary.csv_files), len(summary.figure_files),
            )
        except Exception as exc:
            logger.warning("结果导出失败（非致命）: %s", exc)

    return result


# ===========================================================================
# 结果持久化
# ===========================================================================

def _save_results(
    result: ExperimentResult,
    output_dir: Path,
    elapsed: float,
    capability_history: Any | None = None,
    workers: dict[str, dict[str, Any]] | None = None,
    server: dict[str, Any] | None = None,
    reproducibility: dict[str, Any] | None = None,
) -> None:
    """
    将实验结果保存为 JSON 文件。

    输出：
      <output_dir>/round_<N>_summary.json   — 每轮指标（Table 1 行）
      <output_dir>/experiment_summary.json  — 全局汇总

    Args:
        capability_history: Phase14 新增可选参数——
            evaluation.capability_tracker.CapabilityEvolutionTracker 实例。
            提供时，每轮 JSON 会额外带上 "capability_summary" 键（真实
            covered/absorbing/broken/gap 四态计数，来自
            server.capability.CapabilityTracker.to_capability_matrix() 的
            真实快照）；不提供（如 Setting1 自进化，没有 server/能力矩阵
            概念）或找不到对应轮次时，该键省略——旧版 round JSON 读取方
            （如 evaluation/results_exporter.py）不受影响。
    """
    capability_by_round: dict[int, dict] = {}
    if capability_history is not None:
        for cap_summary in capability_history.history:
            capability_by_round[cap_summary.round_idx] = cap_summary.to_dict()

    # ── 每轮摘要 ──────────────────────────────────────────────────────────────
    for i, round_result in enumerate(result.rounds):
        per_round_path = output_dir / f"round_{i:02d}_summary.json"
        with open(per_round_path, "w", encoding="utf-8") as f:
            # 将 TrialSnapshot 序列化（供 ResultsExporter 构建 CSV）
            snap_list = [
                {
                    "round_idx": s.round_idx,
                    "worker_id": s.worker_id,
                    "task_id": s.task_id,
                    "reward": s.reward,
                    "soft_reward": s.soft_reward,
                    "trajectory_tokens": s.trajectory_tokens,
                    "patch_tokens": s.patch_tokens,
                    "library_size_before": s.library_size_before,
                    "library_size_after": s.library_size_after,
                    "cost_usd": s.cost_usd,
                    # Phase14 新增：基于真实轨迹/patch 文本计算的 SELR（Appendix E Eq.5）
                    "selr": s.selr,
                    "n_sensitive_entities": s.n_sensitive_entities,
                    "n_leaked_entities": s.n_leaked_entities,
                }
                for s in round_result.snapshots
            ]
            round_dict = {
                "round_idx": round_result.round_idx,
                "setting_name": round_result.setting_name,
                "metrics": round_result.metrics,
                "per_worker": round_result.per_worker,
                "snapshots": snap_list,
            }
            cap_summary_dict = capability_by_round.get(round_result.round_idx)
            if cap_summary_dict is not None:
                round_dict["capability_summary"] = cap_summary_dict
            json.dump(round_dict, f, ensure_ascii=False, indent=2)

    # ── 全局汇总 ──────────────────────────────────────────────────────────────
    summary_path = output_dir / "experiment_summary.json"
    summary = result.to_dict()
    summary["elapsed_seconds"] = elapsed
    # Result Reproduction Readiness Audit TASK1/TASK6：与 family_loop 模式的
    # _save_family_loop_summary() 保持同样的 "workers"/"server"/
    # "reproducibility" 键。默认均为 None（如 _run_family_loop() 内部每个
    # family 写自己 output_dir 时的那些调用）时不添加这三个键，不影响旧格式
    # 读取方。
    if workers is not None:
        summary["workers"] = workers
    if server is not None:
        summary["server"] = server
    if reproducibility is not None:
        summary["reproducibility"] = reproducibility
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    logger.info(
        "实验结果已保存: summary=%s (rounds=%d, SR=%.3f, elapsed=%.1fs)",
        summary_path, len(result.rounds), result.final_success_rate, elapsed,
    )
    print(f"\n实验完成: {result.setting_name}")
    print(f"  最终成功率: {result.final_success_rate:.3f}")
    print(f"  总耗时:     {elapsed:.1f}s")
    print(f"  结果目录:   {output_dir}")


# ===========================================================================
# CLI 入口
# ===========================================================================

def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_experiment",
        description="FederatedSkill 统一实验入口（Algorithm 1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # Setting 1: SE baseline（干跑验证配置）
  python experiments/run_experiment.py --config experiments/configs/setting_se.yaml --dry-run

  # Setting 2: Homo Fed（8 轮）
  python experiments/run_experiment.py --config experiments/configs/setting_homo_fed.yaml --rounds 8

  # Setting 4: Full Hetero（覆盖输出目录）
  python experiments/run_experiment.py \\
      --config experiments/configs/setting_full_hetero.yaml \\
      --output results/hetero_run1 --plot

  # Ablation A1
  python experiments/run_experiment.py --config experiments/configs/ablation_a1_no_capability_matrix.yaml

  # 单 family 独立实验（论文 Section 5 family-level 独立执行协议）
  python experiments/run_experiment.py --config experiments/configs/setting_se.yaml \\
      --family Cross-Format-Data-Reconciliation

  # 多个 family 逐个独立运行（每个 family 一个全新的 experiment_id）
  python experiments/run_experiment.py --config experiments/configs/setting_se.yaml \
      --families Cross-Format-Data-Reconciliation,OCR-Data-Extraction

  # 当前配置可用的全部 family 逐个独立运行
  python experiments/run_experiment.py --config experiments/configs/setting_se.yaml --all-families
""",
    )
    parser.add_argument(
        "--config", required=True, type=Path,
        help="YAML 实验配置文件路径（experiments/configs/...）",
    )
    parser.add_argument(
        "--family", default=None,
        help="只运行指定的单个 family（论文 Section 5 family-level 独立执行）。"
             "每次运行生成全新 experiment_id，输出到 results/<experiment_id>/<family>/"
             "{trajectories,patches,libraries,metrics}/，不复用旧结果。",
    )
    parser.add_argument(
        "--families", default=None,
        help="逗号分隔的 family 列表，按给定顺序逐个独立运行；每个 family "
             "仍生成自己的全新 experiment_id。不能与 --family/--all-families 同用。",
    )
    parser.add_argument(
        "--all-families", action="store_true", default=False,
        help="运行当前配置可用的全部 family（先应用 paper_benchmark_only/family_subset 过滤），"
             "每个 family 一个独立 experiment_id。不能与 --family/--families 同用。",
    )
    parser.add_argument(
        "--stop-on-family-error", action="store_true", default=False,
        help="批量 family 模式下遇到第一个失败立即停止；默认记录失败并继续后续 family。",
    )
    parser.add_argument(
        "--rounds", type=int, default=None,
        help="覆盖配置中的 rounds 数量",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="覆盖配置中的输出目录 output_dir",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="仅打印配置摘要，不执行 LLM 调用",
    )
    parser.add_argument(
        "--mock", action="store_true", default=False,
        help="使用 mock backbone 真实跑完整流程（不发出任何真实 API 请求，"
             "不需要 API Key），用于验证 family 循环/round 单调/不混淆等结构",
    )
    parser.add_argument(
        "--mock-federated", action="store_true", default=False,
        help="\"FederatedSkill Faithful Mock Validation\" TASK1 新增：比 --mock "
             "更\"忠实\"的 mock，PatchDistiller/Stage1/Stage2 三个调用方都返回 "
             "schema 合法且内容非空的 payload（至少一个 SKILL.md / 非空 "
             "capability_matrix / 至少一条 ABSORB 或 REPAIR directive / 一个有效 "
             "merged patch），用于验证 skill evolution 全链路而不仅是 family 循环 "
             "结构。与 --mock 一样不发出任何真实 API 请求、不需要 API Key。",
    )
    parser.add_argument(
        "--plot", action="store_true", default=False,
        help="实验结束后自动生成 Figure 2/3/4（需要 matplotlib）",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    parser.add_argument(
        "--distillation-mode", default="strict",
        choices=["strict", "audit"],
        help="Patch 蒸馏 LLM 调用失败时的处理模式（Experiment Integrity "
             "Hardening TASK1）。strict（默认）：直接终止实验，防止静默污染"
             "论文结果。audit：仅用于调试/对比，记录失败到 "
             "distillation_failed.csv 并以本轮无 patch 的方式继续，不得用于"
             "正式论文结果。",
    )
    parser.add_argument(
        "--execution-mode", default="api",
        choices=["api", "cli"],
        help="任务执行模式：api=API executor；cli=严格调用配置声明的真实 CLI harness。",
    )
    return parser


def main() -> None:
    """CLI 主入口。"""
    parser = _build_cli()
    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        family_mode_count = sum(bool(x) for x in (args.family, args.families, args.all_families))
        if family_mode_count > 1:
            parser.error("--family / --families / --all-families 只能选择其中一种。")

        if args.all_families:
            cfg = _load_yaml(Path(args.config).resolve())
            _validate_experiment_config(cfg)
            families_dir = Path(cfg.get("families_dir", str(_REPO_ROOT / "benchmark" / "families")))
            if not families_dir.is_absolute():
                families_dir = _REPO_ROOT / families_dir
            families = load_all_families(directory=families_dir)
            families = _apply_paper_benchmark_scope(cfg, families)
            families = _apply_family_subset(cfg, families)
            run_family_batch_experiments(
                config_path=args.config,
                family_ids=sorted(families.keys()),
                rounds_override=args.rounds,
                results_root_override=args.output,
                dry_run=args.dry_run,
                mock=args.mock,
                mock_federated=args.mock_federated,
                distillation_failure_mode=args.distillation_mode,
                execution_mode=args.execution_mode,
                continue_on_error=not args.stop_on_family_error,
            )
        elif args.families:
            family_ids = [fid.strip() for fid in args.families.split(",") if fid.strip()]
            run_family_batch_experiments(
                config_path=args.config,
                family_ids=family_ids,
                rounds_override=args.rounds,
                results_root_override=args.output,
                dry_run=args.dry_run,
                mock=args.mock,
                mock_federated=args.mock_federated,
                distillation_failure_mode=args.distillation_mode,
                execution_mode=args.execution_mode,
                continue_on_error=not args.stop_on_family_error,
            )
        elif args.family:
            run_family_experiment(
                config_path=args.config,
                family_id=args.family,
                rounds_override=args.rounds,
                results_root_override=args.output,
                dry_run=args.dry_run,
                mock=args.mock,
                mock_federated=args.mock_federated,
                distillation_failure_mode=args.distillation_mode,
                execution_mode=args.execution_mode,
            )
        else:
            run_experiment(
                config_path=args.config,
                rounds_override=args.rounds,
                output_dir_override=args.output,
                dry_run=args.dry_run,
                plot=args.plot,
                mock=args.mock,
                distillation_failure_mode=args.distillation_mode,
                mock_federated=args.mock_federated,
                execution_mode=args.execution_mode,
            )
    except EnvironmentError as exc:
        print(f"\n[错误] 环境变量缺失: {exc}", file=sys.stderr)
        print("提示: 请复制 .env.example 到 .env 并填入 API Key。", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"\n[错误] 文件未找到: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"\n[错误] 参数/配置无效: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
