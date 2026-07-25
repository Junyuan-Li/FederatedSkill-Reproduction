"""
test_llm_connection.py — 测试 API Key 有效性与端点可达性

用法：
    python scripts/test_llm_connection.py               # 测试所有可用 provider
    python scripts/test_llm_connection.py --provider dashscope
    python scripts/test_llm_connection.py --provider moonshot

跳过条件：
    - DASHSCOPE_KEY 未设置 → 跳过 DashScope 测试
    - MOONSHOT_KEY 未设置  → 跳过 Moonshot 测试

结果示例：
    [DashScope] qwen3.6-plus
      端点: https://dashscope.aliyuncs.com/apps/anthropic
      响应: "OK"  ✓ (1.23s)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ─────────────────────────────────────────────
PROBE_PROMPT = "Reply with the word OK and nothing else."
MAX_TOKENS = 16

TESTS = [
    {
        "name": "DashScope / qwen3.6-plus",
        "key_env": "DASHSCOPE_KEY",
        "provider": "dashscope",
        "model": "qwen3.6-plus",
        "temperature": 0.0,
    },
    {
        "name": "DashScope / glm-5 (server)",
        "key_env": "DASHSCOPE_KEY",
        "provider": "dashscope",
        "model": "glm-5",
        "temperature": 0.0,
    },
    {
        "name": "Moonshot / kimi-k2.5",
        "key_env": "MOONSHOT_KEY",
        "provider": "moonshot",
        "model": "kimi-k2.5",
        "temperature": 1.0,           # Moonshot 要求 temperature >= 1.0
    },
]


def run_test(test: dict, verbose: bool = False) -> None:
    """执行单次 API 连通性测试。"""
    name = test["name"]
    key_env = test["key_env"]
    api_key = os.environ.get(key_env, "")

    print(f"\n[{name}]")

    if not api_key:
        print(f"  跳过：{key_env} 未设置")
        return

    # 延迟导入（确保 sys.path 已修改）
    try:
        from llm.providers import PROVIDERS, MODEL_QWEN, MODEL_GLM, MODEL_KIMI  # noqa
        from llm.backbone import LLMBackbone
    except ImportError as e:
        print(f"  [错误] 无法导入项目模块: {e}")
        print(f"  请在项目根目录执行此脚本")
        return

    provider_key = f"{test['provider']}_anthropic"
    if provider_key not in PROVIDERS:
        provider_key = f"{test['provider']}_openai"

    prov = PROVIDERS.get(provider_key)
    if prov is None:
        print(f"  [错误] 未知 provider: {test['provider']}")
        return

    print(f"  端点: {prov.api_base}")
    print(f"  模型: {test['model']}")

    # 构造 LLMBackbone 进行探测
    try:
        backbone = LLMBackbone(
            model=test["model"],
            api_base=prov.api_base,
            api_key=api_key,
            temperature=test["temperature"],
            max_tokens=MAX_TOKENS,
        )
    except Exception as e:
        print(f"  [错误] 构造 LLMBackbone 失败: {e}")
        return

    messages = [{"role": "user", "content": PROBE_PROMPT}]
    t0 = time.perf_counter()
    try:
        result = backbone.call(messages)
        elapsed = time.perf_counter() - t0
        content = (result.content or "").strip()[:80]
        if content:
            print(f"  响应: {repr(content)}  ✓ ({elapsed:.2f}s)")
        else:
            print(f"  响应为空（可能调用成功但返回内容异常）({elapsed:.2f}s)")
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"  [失败] {e} ({elapsed:.2f}s)")
        if verbose:
            import traceback
            traceback.print_exc()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="测试 LLM API 端点连通性"
    )
    parser.add_argument(
        "--provider",
        choices=["dashscope", "moonshot", "all"],
        default="all",
        help="指定要测试的 provider（默认: all）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="打印完整异常栈",
    )
    args = parser.parse_args()

    print("=" * 50)
    print("  FederatedSkill — LLM 连通性测试")
    print("=" * 50)

    tests_to_run = TESTS if args.provider == "all" else [
        t for t in TESTS if t["provider"] == args.provider
    ]

    if not tests_to_run:
        print(f"未找到 provider={args.provider!r} 的测试配置")
        sys.exit(1)

    for test in tests_to_run:
        run_test(test, verbose=args.verbose)

    print("\n" + "─" * 50)
    print("  测试完成。若所有响应均包含 'OK' 则 API 配置正确。")
    print()


if __name__ == "__main__":
    main()
