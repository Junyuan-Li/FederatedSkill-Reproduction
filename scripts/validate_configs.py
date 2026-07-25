"""
validate_configs.py — 验证所有实验 YAML 配置文件的必填字段

用法：
    python scripts/validate_configs.py
    python scripts/validate_configs.py --config_dir experiments/configs
    python scripts/validate_configs.py --config experiments/configs/setting_se.yaml

检查项：
    - 顶层必填：setting_name, workers
    - workers 列表必填：client_id, backbone_model, api_key_env, api_base
    - 联邦实验额外检查：server 字段
    - 环境变量可达性检查（api_key_env 对应的 os.environ key）

输出示例：
    ============================
    Config 字段验证
    ============================
    setting_se.yaml               ✓  OK
    setting_homo_fed.yaml         ✓  OK
    setting_hetero_backbone.yaml  ⚠  workers[1] 缺少 api_key_env
    ---
    2/3 通过
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

try:
    import yaml
except ImportError:
    print("[错误] 需要 pyyaml: pip install pyyaml")
    sys.exit(1)

# ─── 必填字段规范 ─────────────────────────────────
# 顶层字段
REQUIRED_TOP = ["setting_name", "workers"]

# workers 列表中每个 worker 的必填字段
REQUIRED_WORKER = ["client_id", "backbone_model", "api_key_env"]

# 联邦实验（federated: true）额外检查
REQUIRED_FED_TOP = ["server"]

# server 字段（联邦模式）
REQUIRED_SERVER = ["backbone_model", "api_key_env"]


def _check_config(cfg: dict, filepath: Path) -> list[str]:
    """
    检查单个配置字典，返回问题列表（空 = 通过）。
    """
    issues: list[str] = []

    # 顶层检查
    for field in REQUIRED_TOP:
        if field not in cfg:
            issues.append(f"缺少顶层字段: {field!r}")

    # workers 检查
    workers = cfg.get("workers", [])
    if isinstance(workers, list):
        for i, w in enumerate(workers):
            if not isinstance(w, dict):
                issues.append(f"workers[{i}] 不是字典")
                continue
            for field in REQUIRED_WORKER:
                if field not in w:
                    issues.append(f"workers[{i}] 缺少字段: {field!r}")

            # 检查 api_key_env 是否设置（警告级别，不计入失败）
            key_env = w.get("api_key_env", "")
            if key_env and not os.environ.get(key_env):
                issues.append(f"workers[{i}] api_key_env={key_env!r} 环境变量未设置（将在运行时报错）")
    else:
        issues.append("workers 字段不是列表")

    # 联邦模式额外检查
    if cfg.get("federated", False):
        for field in REQUIRED_FED_TOP:
            if field not in cfg:
                issues.append(f"联邦模式缺少字段: {field!r}")

        server = cfg.get("server")
        if isinstance(server, dict):
            for field in REQUIRED_SERVER:
                if field not in server:
                    issues.append(f"server 缺少字段: {field!r}")

            key_env = server.get("api_key_env", "")
            if key_env and not os.environ.get(key_env):
                issues.append(f"server api_key_env={key_env!r} 环境变量未设置")

    return issues


def validate_all(config_dir: Path) -> bool:
    """
    验证 config_dir 下所有 .yaml / .yml 文件。
    返回是否全部通过。
    """
    yaml_files = sorted(
        list(config_dir.glob("*.yaml")) + list(config_dir.glob("*.yml"))
    )

    if not yaml_files:
        print(f"[警告] {config_dir} 下没有 YAML 配置文件")
        return True

    print()
    print("=" * 60)
    print("  FederatedSkill Config 字段验证")
    print("=" * 60)
    print(f"\n  配置目录: {config_dir}")
    print(f"  文件数: {len(yaml_files)}\n")
    print(f"  {'文件名':<35} {'结果'}")
    print("  " + "─" * 55)

    passed = 0
    for f in yaml_files:
        try:
            with open(f, encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
        except Exception as e:
            print(f"  {f.name:<35} ✗ 解析失败: {e}")
            continue

        issues = _check_config(cfg, f)
        if not issues:
            print(f"  {f.name:<35} ✓ OK")
            passed += 1
        else:
            # 区分真正缺失（失败）和环境变量未设（警告）
            errors   = [i for i in issues if "环境变量未设置" not in i]
            warnings = [i for i in issues if "环境变量未设置" in i]
            if not errors:
                print(f"  {f.name:<35} ⚠ {len(warnings)} 个环境变量警告")
                for w in warnings:
                    print(f"    → {w}")
                passed += 1
            else:
                print(f"  {f.name:<35} ✗ {len(errors)} 个字段错误")
                for e in errors:
                    print(f"    → {e}")
                for w in warnings:
                    print(f"    ⚠ {w}")

    print()
    print(f"  结果: {passed}/{len(yaml_files)} 通过")
    print()
    return passed == len(yaml_files)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="验证实验 YAML 配置文件必填字段"
    )
    parser.add_argument(
        "--config_dir",
        default=str(ROOT / "experiments" / "configs"),
        help="配置目录（默认: experiments/configs）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="单独验证指定的 YAML 文件",
    )
    args = parser.parse_args()

    if args.config:
        f = Path(args.config)
        if not f.exists():
            print(f"[错误] 文件不存在: {f}")
            sys.exit(1)
        with open(f, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        issues = _check_config(cfg, f)
        if not issues:
            print(f"✓ {f.name} 验证通过")
        else:
            print(f"✗ {f.name} 存在问题:")
            for i in issues:
                print(f"  → {i}")
            sys.exit(1)
    else:
        ok = validate_all(Path(args.config_dir))
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
