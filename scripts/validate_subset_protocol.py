"""验证 representative subset 的完整 family 与 Setting1/2 控制变量。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "experiments" / "configs"
SELECTION_PATH = CONFIG_DIR / "representative_subset.yaml"
SETTING1_PATH = CONFIG_DIR / "subset_setting1_self_evolution.yaml"
SETTING2_PATH = CONFIG_DIR / "subset_setting2_homogeneous_federation.yaml"
FAMILIES_DIR = REPO_ROOT / "benchmark" / "families"

ALLOWED_TREATMENT_KEYS = {
    "setting_name", "description", "output_dir", "federated", "server",
    "merger_mode", "isolated_worker_skills", "sync_schedule",
}


def _yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise RuntimeError(f"配置顶层不是 mapping: {path}")
    return value


def _normalized(cfg: dict) -> dict:
    return {key: value for key, value in cfg.items() if key not in ALLOWED_TREATMENT_KEYS}


def validate() -> dict:
    selection = _yaml(SELECTION_PATH)
    setting1 = _yaml(SETTING1_PATH)
    setting2 = _yaml(SETTING2_PATH)
    family_subset = selection.get("family_subset")
    if not isinstance(family_subset, list) or len(family_subset) != 3:
        raise RuntimeError("representative subset 必须恰好包含 Easy/Medium/Hard 三个 family")
    if len(set(family_subset)) != 3:
        raise RuntimeError("representative subset family 不得重复")

    for cfg_name, cfg in (("Setting1", setting1), ("Setting2", setting2)):
        if cfg.get("family_subset") != family_subset:
            raise RuntimeError(f"{cfg_name} family_subset 与选择报告不一致")
        if cfg.get("sampler") != "family_curriculum":
            raise RuntimeError(f"{cfg_name} 禁止 task sampling")
        if cfg.get("rounds_per_family_mode") != "family_length":
            raise RuntimeError(f"{cfg_name} 必须完整运行 family_length")
        if cfg.get("max_retry") != 0:
            raise RuntimeError(f"{cfg_name} max_retry 必须为 0")
        if len(cfg.get("workers") or []) != 3:
            raise RuntimeError(f"{cfg_name} 必须使用三个同质 worker 进行受控比较")

    if setting1.get("federated") is not False or "server" in setting1:
        raise RuntimeError("Setting1 必须是无 server 的独立 Self Evolution")
    if setting2.get("federated") is not True or not isinstance(setting2.get("server"), dict):
        raise RuntimeError("Setting2 必须启用 Homogeneous Federation")
    if _normalized(setting1) != _normalized(setting2):
        raise RuntimeError("Setting1/Setting2 除 federation 机制字段外存在配置漂移")

    worker_models = {
        (worker["backbone_model"], worker["agent_harness"])
        for worker in setting2["workers"]
    }
    server = setting2["server"]
    if worker_models != {("qwen3.6-plus", "claude-code")}:
        raise RuntimeError(f"worker 不同质或模型/harness 漂移: {worker_models}")
    if server.get("backbone_model") != "qwen3.6-plus":
        raise RuntimeError("Setting2 server 必须与 homogeneous workers 使用同一 backbone")

    selected_task_count = 0
    for tier_name, tier in selection["tiers"].items():
        family_id = tier["family_id"]
        family_data = json.loads(
            (FAMILIES_DIR / f"{family_id}.json").read_text(encoding="utf-8")
        )
        actual_ids = [task["task_id"] for task in family_data["tasks"]]
        expected_ids = tier["task_ids_in_official_order"]
        if actual_ids != expected_ids:
            raise RuntimeError(f"{tier_name}/{family_id} task 顺序或完整性漂移")
        selected_task_count += len(actual_ids)

    return {
        "family_count": len(family_subset),
        "task_count": selected_task_count,
        "family_subset": family_subset,
        "controlled_worker_profile": "3 x qwen3.6-plus / claude-code",
        "only_treatment": "FederatedSkill server collaboration enabled",
    }


def main() -> int:
    print(json.dumps(validate(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())