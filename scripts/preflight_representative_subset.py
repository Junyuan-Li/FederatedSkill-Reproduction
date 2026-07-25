"""代表性子集实验的最小真实连通性门禁，不回显任何凭据。"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

from core.datatypes import WorkerProfile  # noqa: E402
from harness.claude_code_harness import ClaudeCodeHarness  # noqa: E402
from harness.cli_utils import check_cli_binary, run_cli_subprocess  # noqa: E402
from llm.backbone import LLMBackbone  # noqa: E402
from llm.retry import RetryConfig  # noqa: E402
from scripts.validate_subset_protocol import validate  # noqa: E402

OUTPUTS = (
    REPO_ROOT / "results" / "subset_setting1_self_evolution",
    REPO_ROOT / "results" / "subset_setting2_homogeneous_federation",
)
REPORT_PATH = REPO_ROOT / "subset_preflight_report.json"


def _profile() -> WorkerProfile:
    return WorkerProfile(
        client_id="_subset_probe",
        backbone_model="qwen3.6-plus",
        agent_harness="claude-code",
        model_provider="dashscope",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="QWEN_DASHSCOPE_API_KEY",
        max_context_tokens=131072,
    )


def _probe_backbone(profile: WorkerProfile) -> dict[str, object]:
    started = time.monotonic()
    try:
        backbone = LLMBackbone.from_worker_profile(
            profile,
            max_tokens=16,
            retry_config=RetryConfig.fast_fail(),
            request_timeout_seconds=60.0,
        )
        result = backbone.call("Reply with exactly: SUBSET_OK")
        success = bool(result.text.strip())
        error = None if success else "empty response"
    except Exception as exc:  # noqa: BLE001 - preflight 需将任意失败写入报告
        success = False
        error = f"{type(exc).__name__}: {exc}"
    return {
        "component": "qwen_backbone",
        "success": success,
        "latency_seconds": round(time.monotonic() - started, 3),
        "error": error,
    }


def _probe_cli(profile: WorkerProfile) -> dict[str, object]:
    started = time.monotonic()
    try:
        version = check_cli_binary("claude")
        harness = ClaudeCodeHarness(router=None)
        result = run_cli_subprocess(
            harness.build_argv(profile),
            input_text="Reply with exactly: SUBSET_CLI_OK",
            env=harness.build_env(profile),
            timeout=120.0,
        )
        success = result.success and harness.success_marker() in result.stdout
        error = None if success else (
            f"returncode={result.returncode}; timed_out={result.timed_out}; "
            f"stderr_tail={(result.stderr or '')[-300:]!r}"
        )
    except Exception as exc:  # noqa: BLE001 - preflight 需将任意失败写入报告
        version = None
        success = False
        error = f"{type(exc).__name__}: {exc}"
    return {
        "component": "qwen_via_claude_code",
        "success": success,
        "cli_version": version,
        "latency_seconds": round(time.monotonic() - started, 3),
        "error": error,
    }


def main() -> int:
    protocol = validate()
    if not os.environ.get("QWEN_DASHSCOPE_API_KEY"):
        raise RuntimeError("QWEN_DASHSCOPE_API_KEY 未配置")
    nonempty = [str(path) for path in OUTPUTS if path.exists() and any(path.iterdir())]
    if nonempty:
        raise RuntimeError(f"拒绝覆盖非空 subset 结果目录: {nonempty}")

    profile = _profile()
    checks = [_probe_backbone(profile), _probe_cli(profile)]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol": protocol,
        "checks": checks,
        "passed": all(bool(item["success"]) for item in checks),
    }
    REPORT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for item in checks:
        print(
            f"{item['component']}: "
            f"{'OK' if item['success'] else 'FAIL'} "
            f"({item['latency_seconds']}s)"
        )
        if item["error"]:
            print(f"  {item['error']}")
    print(f"report: {REPORT_PATH}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())