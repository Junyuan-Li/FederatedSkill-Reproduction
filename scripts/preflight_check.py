"""
preflight_check.py — 真实实验前置环境检查

运行：
    python scripts/preflight_check.py

检查项目：
    1. Python 版本（>= 3.10）
    2. pip 核心依赖
    3. 环境变量：DASHSCOPE_KEY / MOONSHOT_KEY
    4. Agent Harness CLI（claude / qwen-code / kimi）

输出示例：
    ============================
    FederatedSkill Preflight Check
    ============================
    Python ✓  3.11.9
    Dependencies ✓  litellm pyyaml pydantic python-dotenv
    DASHSCOPE_KEY ✓  sk-xxx...
    MOONSHOT_KEY ✗  未设置 → export MOONSHOT_KEY="你的key"
    claude-code ✓
    qwen-code ✗  未安装 → npm install -g @qwen-code/qwen-code
    ---
    结论: 可运行 Setting 1-2，Setting 3-4 需要 MOONSHOT_KEY
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 加载 .env（若存在）
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

# ─────────────────────────────────────────────
REQUIRED_PACKAGES = [
    "litellm", "yaml", "pydantic", "dotenv",
    "tqdm", "requests",
]
OPTIONAL_PACKAGES = ["matplotlib", "numpy", "pandas"]

API_KEYS = {
    "DASHSCOPE_KEY": {
        "desc": "DashScope (Qwen/GLM)",
        "hint": 'export DASHSCOPE_KEY="sk-xxx"  # 阿里云百炼平台申请',
        "required_for": "Setting 1-4 全部",
    },
    "MOONSHOT_KEY": {
        "desc": "Moonshot (Kimi)",
        "hint": 'export MOONSHOT_KEY="sk-xxx"  # platform.moonshot.cn 申请',
        "required_for": "Setting 3-4",
    },
}

HARNESS_CMDS = {
    "claude-code": {
        "cmd": "claude",
        "install": "npm install -g @anthropic-ai/claude-code",
        "required_for": "Setting 1-3, Ablation A1/A2/A3",
    },
    "qwen-code": {
        "cmd": "qwen-code",
        "install": "npm install -g @qwen-code/qwen-code",
        "required_for": "Setting 4",
    },
    "kimi-cli": {
        "cmd": "kimi",
        "install": "pip install kimi-cli  # 或参考 Moonshot 官方文档",
        "required_for": "Setting 4",
    },
}

# ANSI 颜色（Windows 10+ 支持，旧版自动降级）
_GREEN = "\033[92m" if sys.stdout.isatty() else ""
_RED   = "\033[91m" if sys.stdout.isatty() else ""
_YELLOW= "\033[93m" if sys.stdout.isatty() else ""
_RESET = "\033[0m"  if sys.stdout.isatty() else ""

def ok(msg: str)  -> str: return f"{_GREEN}✓{_RESET}  {msg}"
def fail(msg: str)-> str: return f"{_RED}✗{_RESET}  {msg}"
def warn(msg: str)-> str: return f"{_YELLOW}?{_RESET}  {msg}"


def check_python() -> bool:
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        print(f"  Python    {ok(version_str)}")
        return True
    else:
        print(f"  Python    {fail(version_str + ' (需要 >= 3.10)')}")
        return False


def check_packages() -> bool:
    missing = []
    present = []
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
            present.append(pkg)
        except ImportError:
            missing.append(pkg)

    opt_missing = []
    for pkg in OPTIONAL_PACKAGES:
        try:
            importlib.import_module(pkg)
        except ImportError:
            opt_missing.append(pkg)

    if not missing:
        print(f"  核心依赖  {ok(', '.join(present[:4]) + ' ...')}")
    else:
        print(f"  核心依赖  {fail('缺少: ' + ', '.join(missing))}")
        print(f"            → pip install -r requirements-real.txt")

    if opt_missing:
        print(f"  可选依赖  {warn('缺少: ' + ', '.join(opt_missing) + ' (图表/表格功能)')}")
        print(f"            → pip install " + " ".join(opt_missing))

    return not missing


def check_api_keys() -> dict[str, bool]:
    results: dict[str, bool] = {}
    for key, info in API_KEYS.items():
        val = os.environ.get(key, "")
        if val:
            masked = val[:6] + "..." + val[-3:] if len(val) > 10 else "***"
            print(f"  {key:<20}{ok(masked + '  (' + info['desc'] + ')')}")
            results[key] = True
        else:
            print(f"  {key:<20}{fail('未设置  (' + info['desc'] + ')')}")
            print(f"            → {info['hint']}")
            print(f"            (需要: {info['required_for']})")
            results[key] = False
    return results


def check_harness() -> dict[str, bool]:
    results: dict[str, bool] = {}
    for name, info in HARNESS_CMDS.items():
        path = shutil.which(info["cmd"])
        if path:
            # 尝试获取版本
            try:
                ver = subprocess.check_output(
                    [info["cmd"], "--version"], stderr=subprocess.STDOUT,
                    timeout=5, text=True,
                ).strip().split("\n")[0][:40]
            except Exception:
                ver = "已安装"
            print(f"  {name:<18}{ok(ver)}")
            results[name] = True
        else:
            print(f"  {name:<18}{fail('未找到')}")
            print(f"            → {info['install']}")
            print(f"            (需要: {info['required_for']})")
            results[name] = False
    return results


def check_project_structure() -> bool:
    root = Path(__file__).resolve().parents[1]
    required = [
        "experiments/configs/setting_se.yaml",
        "experiments/configs/setting_homo_fed.yaml",
        "llm/providers.py",
        "server/evolution.py",
        "client/distiller.py",
    ]
    missing = [p for p in required if not (root / p).exists()]
    if not missing:
        print(f"  项目结构  {ok('核心文件均存在')}")
        return True
    else:
        print(f"  项目结构  {fail('缺少文件: ' + str(missing[:3]))}")
        return False


def main() -> None:
    print()
    print("=" * 50)
    print("  FederatedSkill Preflight Check")
    print("=" * 50)

    print("\n[Python]")
    py_ok = check_python()

    print("\n[依赖包]")
    pkg_ok = check_packages()

    print("\n[API Key]")
    key_results = check_api_keys()

    print("\n[Agent Harness]")
    harness_results = check_harness()

    print("\n[项目结构]")
    struct_ok = check_project_structure()

    # 汇总结论
    dashscope_ok = key_results.get("DASHSCOPE_KEY", False)
    moonshot_ok  = key_results.get("MOONSHOT_KEY", False)
    claude_ok    = harness_results.get("claude-code", False)

    print("\n" + "─" * 50)
    print("  结论：")

    if py_ok and pkg_ok and dashscope_ok and claude_ok and struct_ok:
        print(f"  {ok('可运行 Setting 1-2 (SE + Homo Fed)')}")
    else:
        print(f"  {fail('Setting 1-2 尚未就绪，请修复上述问题')}")

    if dashscope_ok and moonshot_ok and claude_ok:
        print(f"  {ok('可运行 Setting 3-4 (Hetero Backbone + Full Hetero)')}")
    else:
        print(f"  {warn('Setting 3-4 需要 MOONSHOT_KEY + claude-code')}")

    if not pkg_ok:
        print(f"\n  快速修复: pip install -r requirements-real.txt")

    print()


if __name__ == "__main__":
    main()
