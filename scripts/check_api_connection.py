"""
scripts/check_api_connection.py — Provider Connectivity Dry-Run（Provider Key
Isolation Fix Part6）

用途：
    在启动真实付费实验（Setting 1-4）之前，用极小的一次性调用逐个验证
    Qwen3.6-Plus / GLM-5 / Kimi-K2.5 / Claude Code CLI 四条鉴权路径是否
    可达、密钥是否有效——按论文 model → provider → 环境变量映射逐一测试：

        Qwen3.6-Plus  -> DashScope (compatible)      -> QWEN_DASHSCOPE_API_KEY
        GLM-5         -> DashScope (compatible)      -> GLM_DASHSCOPE_API_KEY
        Kimi-K2.5     -> DashScope (compatible)      -> KIMI_DASHSCOPE_API_KEY
        Claude Code   -> 真实 claude CLI 二进制        -> ANTHROPIC_BASE_URL /
                                                          ANTHROPIC_AUTH_TOKEN

设计约束：
    - Qwen / GLM / Kimi 三项复用现成的
      `llm.backbone.LLMBackbone.from_worker_profile()` +
      `core.datatypes.WorkerProfile`，走与真实实验完全相同的调用路径
      （litellm + 现有重试逻辑），不新写一套 HTTP 客户端。每项只发一条
      极短 prompt（max_tokens=16, fast_fail 重试预算），是真实但成本
      极小的一次 API round-trip。
        - Claude Code **不**走 Python API/litellm（与 harness/claude_code_harness.py
            的设计一致——Claude Code 由真实 `claude` CLI 二进制驱动，不经过任何
            Python LLM 客户端）。因此这一项通过真实 `claude --print` 最小调用验证
            gateway endpoint，而不是只检查环境变量是否存在。
    - 输出只包含 model / provider / latency / result，绝不打印任何
      API Key 或 Token 明文（哪怕是部分掩码也不打印）。

用法：
    python scripts/check_api_connection.py
"""

from __future__ import annotations

import sys
import time
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from core.datatypes import WorkerProfile
from harness.cli_utils import (
    CLIBinaryNotFoundError,
    check_cli_binary,
    run_cli_subprocess,
)
from llm.backbone import LLMBackbone
from llm.providers import (
    DASHSCOPE_OPENAI_BASE,
    GLM_DASHSCOPE_API_KEY_ENV,
    QWEN_DASHSCOPE_API_KEY_ENV,
)
from llm.retry import RetryConfig

# 连通性探测用最小重试预算（RetryConfig.fast_fail()）：真正的实验调用需要
# backbone.py 默认的"限频无界重试"策略保证不轻易放弃，但纯连通性探测只想
# 快速看到 成功/失败，不应该在真实故障时卡住几分钟到几十分钟。
_PROBE_RETRY = RetryConfig.fast_fail()

# 使用固定文本便于人工识别探针结果。LLMBackbone 只将空字符串或纯空白判定为
# 空响应，合法短文本（如 "OK"）不会再被误判并触发重复付费调用。
_PROBE_PROMPT = "Reply with exactly this text and nothing else: CONNECTION_OK"


@dataclass
class ConnectivityResult:
    model: str
    provider: str
    endpoint: str
    latency_ms: float | None
    success: bool
    error: str | None = None


def _probe(model: str, provider: str, profile: WorkerProfile) -> ConnectivityResult:
    """通过 litellm 真实发起一次极小的 round-trip 调用（Qwen/GLM/Kimi 专用）。"""
    key_present = bool(os.environ.get(profile.api_key_env))
    if not key_present:
        return ConnectivityResult(
            model, provider, profile.api_base, None, False,
            error=f"{profile.api_key_env} 未配置",
        )

    t0 = time.monotonic()
    try:
        backbone = LLMBackbone.from_worker_profile(profile, max_tokens=16, retry_config=_PROBE_RETRY)
        result = backbone.call(_PROBE_PROMPT)
        latency_ms = (time.monotonic() - t0) * 1000
        ok = bool(result.text.strip())
        return ConnectivityResult(
            model, provider, profile.api_base, latency_ms, ok,
            error=None if ok else "空响应",
        )
    except Exception as exc:  # noqa: BLE001 — 连通性探测，任何异常都算失败
        latency_ms = (time.monotonic() - t0) * 1000
        return ConnectivityResult(
            model, provider, profile.api_base, latency_ms, False,
            error=f"{type(exc).__name__}: {exc}",
        )


def _probe_claude_code_cli() -> ConnectivityResult:
    """通过真实 Claude CLI 最小调用验证 gateway endpoint。"""
    base_url_present = bool(os.environ.get("ANTHROPIC_BASE_URL"))
    token_present = bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if not base_url_present or not token_present:
        missing = [
            name for name, present in (
                ("ANTHROPIC_BASE_URL", base_url_present),
                ("ANTHROPIC_AUTH_TOKEN", token_present),
            ) if not present
        ]
        return ConnectivityResult(
            "claude-code (CLI)", "anthropic_cli",
            os.environ.get("ANTHROPIC_BASE_URL", ""), None, False,
            error=f"{', '.join(missing)} 未配置",
        )

    t0 = time.monotonic()
    try:
        check_cli_binary("claude", version_args=("--version",))
        result = run_cli_subprocess(
            [
                "claude", "--print", "--verbose",
                "--output-format", "stream-json",
            ],
            input_text=_PROBE_PROMPT,
            env=os.environ.copy(),
            timeout=120.0,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        ok = result.success and '"type":"result"' in result.stdout
        return ConnectivityResult(
            "claude-code (CLI)", "anthropic_cli",
            os.environ.get("ANTHROPIC_BASE_URL", ""), latency_ms, ok,
            error=None if ok else (
                f"returncode={result.returncode}; "
                f"stderr={result.stderr[-300:]!r}"
            ),
        )
    except CLIBinaryNotFoundError as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        return ConnectivityResult(
            "claude-code (CLI)", "anthropic_cli",
            os.environ.get("ANTHROPIC_BASE_URL", ""), latency_ms, False,
            error=str(exc),
        )


def main() -> int:
    checks: list[ConnectivityResult] = []

    checks.append(_probe(
        "qwen3.6-plus", "dashscope_qwen_openai",
        WorkerProfile(
            client_id="_probe_qwen", backbone_model="qwen3.6-plus",
            agent_harness="claude-code", model_provider="dashscope",
            api_base=DASHSCOPE_OPENAI_BASE, api_key_env=QWEN_DASHSCOPE_API_KEY_ENV,
        ),
    ))

    checks.append(_probe(
        "glm-5", "dashscope_glm_openai",
        WorkerProfile(
            client_id="_probe_glm", backbone_model="glm-5",
            agent_harness="claude-code", model_provider="dashscope",
            api_base=DASHSCOPE_OPENAI_BASE, api_key_env=GLM_DASHSCOPE_API_KEY_ENV,
        ),
    ))

    checks.append(_probe(
        "kimi-k2.5", "dashscope_kimi_openai",
        WorkerProfile(
            client_id="_probe_kimi", backbone_model="kimi-k2.5",
            agent_harness="kimi-cli", model_provider="dashscope",
            api_base=DASHSCOPE_OPENAI_BASE, api_key_env="KIMI_DASHSCOPE_API_KEY",
            is_moonshot=True,
        ),
    ))

    checks.append(_probe_claude_code_cli())

    print(f"{'model':<32} {'provider':<24} {'latency_ms':<12} {'result'}")
    print("-" * 90)
    all_ok = True
    for c in checks:
        latency = f"{c.latency_ms:.0f}" if c.latency_ms is not None else "-"
        status = "OK" if c.success else "FAIL"
        line = f"{c.model:<32} {c.provider:<24} {latency:<12} {status}"
        if not c.success:
            all_ok = False
            line += f"  ({c.error})"
        print(line)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "all_required_models_passed": all_ok,
        "checks": [
            {
                "provider": c.provider,
                "endpoint": c.endpoint,
                "model_name": c.model,
                "success": c.success,
                "latency_ms": c.latency_ms,
                "error": c.error,
            }
            for c in checks
        ],
    }
    report_path = _REPO_ROOT / "api_preflight_report.json"
    temp_path = report_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, report_path)
    print(f"report: {report_path}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
