"""独立验证 Claude/Qwen/Kimi CLI harness，不进入 FederatedSkill 算法链路。

只复用 harness 层的命令、环境变量、prompt 通道和 subprocess 封装；
不导入或调用 server、client、experiments、benchmark、evolution、distillation。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402

from core.datatypes import WorkerProfile  # noqa: E402
from harness.claude_code_harness import ClaudeCodeHarness  # noqa: E402
from harness.cli_utils import CLIRunResult, check_cli_binary, run_cli_subprocess  # noqa: E402
from harness.kimi_cli_harness import KimiCLIHarness  # noqa: E402
from harness.qwen_code_harness import QwenCodeHarness  # noqa: E402

TIMEOUT_SECONDS = 120.0
WORKSPACE_ROOT = _REPO_ROOT / "tmp_cli_validation"
REPORT_PATH = _REPO_ROOT / "results" / "cli_validation_report.json"
EXPECTED_CONTENT = "CLI_AGENT_SUCCESS"
PROMPT = """You are a coding agent.
In the current workspace:

1. Create file hello_agent.txt
2. Write exactly:
CLI_AGENT_SUCCESS

3. Return a final response indicating completion."""

HARNESS_SPECS = {
    "claude-code": (ClaudeCodeHarness, "u1"),
    "qwen-code": (QwenCodeHarness, "u0"),
    "kimi-cli": (KimiCLIHarness, "u2"),
}


def _load_profiles() -> dict[str, WorkerProfile]:
    """只读 Setting4 YAML，复用当前真实 provider/model/key 路由。"""
    config_path = _REPO_ROOT / "experiments" / "configs" / "setting_full_hetero.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    profiles: dict[str, WorkerProfile] = {}
    for worker in config.get("workers", []):
        profile = WorkerProfile(
            client_id=worker["client_id"],
            backbone_model=worker["backbone_model"],
            agent_harness=worker["agent_harness"],
            model_provider=worker["model_provider"],
            api_base=worker["api_base"],
            api_key_env=worker["api_key_env"],
            max_context_tokens=worker.get("max_context_tokens", 131072),
            is_moonshot=worker.get("is_moonshot", False),
        )
        profiles[profile.client_id] = profile
    return profiles


def _tail(text: str, limit: int = 2000) -> str:
    return (text or "")[-limit:]


def _is_expected_content(content: str | None) -> bool:
    """允许普通文本文件末尾包含 CR/LF，其他字符仍严格比较。"""
    return content is not None and content.rstrip("\r\n") == EXPECTED_CONTENT


def _classify_failure(
    result: CLIRunResult,
    harness_error: str | None,
    workspace_write: bool,
) -> tuple[str | None, str | None]:
    """按任务要求的 A/B/C/D 四类返回失败分类与原因。"""
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if result.timed_out:
        return "A", f"CLI启动/运行超时（{TIMEOUT_SECONDS:.0f}s）"
    if result.exception is not None or result.pid is None:
        detail = f"{type(result.exception).__name__}: {result.exception}"
        return "A", f"CLI启动失败: {detail}"
    if result.returncode != 0:
        return "A", f"CLI异常退出: returncode={result.returncode}"
    if harness_error:
        return "D", f"returncode=0 但 harness 判定失败: {harness_error}"
    prompt_missing_signals = (
        "provide the task",
        "provide task instructions",
        "please provide",
        "no prompt",
        "task instructions",
        "what would you like",
    )
    if not workspace_write and any(signal in combined for signal in prompt_missing_signals):
        return "B", "模型输出显示未收到/未识别任务 prompt，且未创建目标文件"
    if not workspace_write:
        return "C", "agent 未在 workspace 创建 hello_agent.txt"
    return None, None


def _validate_one(
    agent_name: str,
    harness_class: type,
    profile: WorkerProfile,
) -> dict[str, Any]:
    workspace = WORKSPACE_ROOT / agent_name
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=False)

    harness = harness_class(router=None)
    version = ""
    result = CLIRunResult(argv=[])
    started_at = datetime.now(timezone.utc)
    harness_error: str | None = None

    try:
        version = check_cli_binary(harness.binary_name, harness.version_args)
        argv = harness.build_argv(profile)
        argv, input_text = harness._build_invocation(profile, argv, PROMPT)
        result = run_cli_subprocess(
            argv,
            cwd=str(workspace),
            input_text=input_text,
            env=harness.build_env(profile),
            timeout=TIMEOUT_SECONDS,
        )
        result.cli_version = version
        try:
            harness._validate_cli_result(result)
        except Exception as exc:  # 诊断 D：进程成功但 adapter 拒绝
            harness_error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        result.exception = exc
        if result.elapsed_seconds == 0.0:
            result.elapsed_seconds = (
                datetime.now(timezone.utc) - started_at
            ).total_seconds()

    output_file = workspace / "hello_agent.txt"
    workspace_write = output_file.is_file()
    actual_content: str | None = None
    if workspace_write:
        try:
            actual_content = output_file.read_text(encoding="utf-8")
        except OSError as exc:
            actual_content = f"<read-error: {exc}>"
    content_valid = _is_expected_content(actual_content)
    prompt_received = workspace_write

    failure_category, failure_reason = _classify_failure(
        result, harness_error, workspace_write
    )
    if failure_category is None and not content_valid:
        failure_category = "C"
        failure_reason = (
            "hello_agent.txt 已创建，但内容不严格等于 CLI_AGENT_SUCCESS: "
            f"{actual_content!r}"
        )

    startup = (
        result.pid is not None
        and result.returncode == 0
        and not result.timed_out
        and result.exception is None
    )
    passed = startup and prompt_received and content_valid and not harness_error

    return {
        "startup": startup,
        "prompt_received": prompt_received,
        "workspace_write": workspace_write,
        "timeout": result.timed_out,
        "returncode": result.returncode,
        "elapsed": round(result.elapsed_seconds, 3),
        "passed": passed,
        "failure_category": failure_category,
        "failure_reason": failure_reason,
        "command": result.command,
        "pid": result.pid,
        "start_time": started_at.isoformat(),
        "cli_version": version,
        "tool_call_count": result.tool_call_count,
        "stream_event_count": result.stream_event_count,
        "file_content_valid": content_valid,
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
    }


def main() -> int:
    load_dotenv(_REPO_ROOT / ".env")
    profiles = _load_profiles()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.unlink(missing_ok=True)

    report: dict[str, dict[str, Any]] = {}
    for agent_name, (harness_class, worker_id) in HARNESS_SPECS.items():
        print(f"\n=== Validating {agent_name} ===", flush=True)
        report[agent_name] = _validate_one(
            agent_name, harness_class, profiles[worker_id]
        )
        status = "PASS" if report[agent_name]["passed"] else "FAIL"
        print(
            f"{agent_name}: {status} returncode={report[agent_name]['returncode']} "
            f"timeout={report[agent_name]['timeout']} "
            f"workspace_write={report[agent_name]['workspace_write']} "
            f"elapsed={report[agent_name]['elapsed']}s",
            flush=True,
        )
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nReport: {REPORT_PATH}")
    return 0 if all(item["passed"] for item in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
