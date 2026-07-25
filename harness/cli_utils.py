"""
cli_utils.py — CLI 二进制检测 + subprocess 封装（Part3 要求）

要求（用户 Part3 原文）：
    检查 claude --version / qwen-code --version / kimi --version；
    如果不存在，明确报错 "Required CLI binary not installed"；
    不要静默 fallback 到 API。

本模块只做两件事：
    1. check_cli_binary()  —— 启动前/每次 initialize() 时检测二进制是否存在
       且可执行，不存在时抛 CLIBinaryNotFoundError（message 里包含用户要求
       的确切短语 "Required CLI binary not installed"）。
     2. run_cli_subprocess() —— 统一的 Popen 调用封装（bytes stdout/stderr、
         进程树超时、异常归一化），供三个具体 Harness 复用。

不属于本模块职责（刻意不做）：
    - 不解析 CLI 输出内容（那是 trajectory_adapter.py 的职责）。
    - 不决定 strict/debug 模式选择（那是 factory.py 的职责）。
"""

from __future__ import annotations

import os
import json
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from core.exceptions import FederatedSkillError

# 与用户 Part3 要求的报错短语完全一致，便于上层/测试用字符串匹配断言。
_NOT_INSTALLED_MSG = "Required CLI binary not installed"


class CLIBinaryNotFoundError(FederatedSkillError):
    """CLI 二进制不存在或不可执行时抛出；调用方不得静默 fallback 到 API 模式。"""


@dataclass
class CLIRunResult:
    """一次 CLI subprocess 调用的结果（capture stdout/stderr + 元信息）。"""

    argv: list[str]
    command: str = ""
    pid: int | None = None
    cli_version: str = ""
    raw_stdout: bytes = b""
    raw_stderr: bytes = b""
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    exception: Exception | None = None
    elapsed_seconds: float = 0.0
    stream_event_count: int = 0
    tool_call_count: int = 0
    last_event_type: str = ""
    timeout_reason: str = ""

    @property
    def success(self) -> bool:
        return self.exception is None and not self.timed_out and self.returncode == 0


def _to_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")


def _terminate_process_tree(proc: subprocess.Popen[bytes], grace_seconds: float) -> None:
    """终止 CLI 的完整进程树；调用本身也受固定时间上限约束。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=grace_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def check_cli_binary(binary: str, version_args: tuple[str, ...] = ("--version",), timeout: float = 10.0) -> str:
    """
    检测 `binary` 是否存在于 PATH 且可正常运行 `binary <version_args>`。

    Args:
        binary:       CLI 可执行文件名/路径，如 "claude"、"qwen-code"、"kimi"
        version_args: 版本检测参数，默认 ("--version",)
        timeout:      子进程超时（秒）

    Returns:
        version 探测命令的 stdout（去除首尾空白），供 experiment_summary.json
        的 version fidelity 记录使用。

    Raises:
        CLIBinaryNotFoundError: PATH 中找不到该二进制，或调用失败/超时。
            消息中包含用户 Part3 要求的确切短语
            "Required CLI binary not installed"，不做任何静默 fallback。
    """
    resolved = shutil.which(binary)
    if resolved is None:
        raise CLIBinaryNotFoundError(
            f"{_NOT_INSTALLED_MSG}: {binary!r}（PATH 中找不到该可执行文件，"
            f"未安装或未加入 PATH，本次运行拒绝静默降级到 API 模式）"
        )
    try:
        proc = subprocess.run(
            [resolved, *version_args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CLIBinaryNotFoundError(
            f"{_NOT_INSTALLED_MSG}: {binary!r}（找到路径 {resolved!r} 但执行"
            f"'{binary} {' '.join(version_args)}' 失败: {type(exc).__name__}: {exc}）"
        ) from exc
    return (proc.stdout or proc.stderr or "").strip()


def run_cli_subprocess(
    argv: list[str],
    *,
    cwd: str | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 600.0,
    kill_grace_seconds: float = 5.0,
) -> CLIRunResult:
    """
    统一的 CLI subprocess 调用封装，供三个具体 Harness 复用。

    保持官方 runner 的 prompt/cwd/timeout 语义，但使用可控 Popen：所有
    stdio 按 bytes 传输并统一 UTF-8 解码；timeout 时终止完整进程树，且
    后续管道 drain 也有固定上限。
    """
    t0 = time.monotonic()
    # Windows 的 npm CLI 通常由 .CMD shim 暴露。CreateProcess 在
    # shell=False 时不会像 PowerShell 那样可靠解析裸命令名，因此这里与
    # check_cli_binary() 一致，先用 shutil.which() 得到真实可执行路径。
    resolved = shutil.which(argv[0]) if argv else None
    effective_argv = [resolved or argv[0], *argv[1:]] if argv else []
    result = CLIRunResult(
        argv=list(effective_argv),
        command=subprocess.list2cmdline(effective_argv),
    )
    heartbeat_stop = threading.Event()
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stream_event_count = 0

    def _report_stream_line(line: bytes) -> None:
        nonlocal stream_event_count
        try:
            event = json.loads(line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        stream_event_count += 1
        event_type = str(event.get("type", "unknown"))
        result.last_event_type = event_type
        subtype = event.get("subtype")
        summary = f"type={event_type}"
        if subtype:
            summary += f" subtype={subtype}"
        message = event.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                tools = [
                    str(item.get("name"))
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "tool_use"
                ]
                if tools:
                    result.tool_call_count += len(tools)
                    summary += f" tools={tools}"
        print(f"[CLI event {stream_event_count}] {summary}", flush=True)

    def _drain_pipe(
        pipe: object,
        target: bytearray,
        report_lines: bool,
    ) -> None:
        pending = bytearray()
        try:
            while True:
                chunk = pipe.read(4096)  # type: ignore[union-attr]
                if not chunk:
                    break
                target.extend(chunk)
                if report_lines:
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        _report_stream_line(bytes(line))
            if report_lines and pending:
                _report_stream_line(bytes(pending))
        except (OSError, ValueError):
            return

    def _heartbeat() -> None:
        while not heartbeat_stop.wait(15.0):
            elapsed = time.monotonic() - t0
            print(
                f"[CLI] pending binary={argv[0] if argv else 'unknown'} "
                f"elapsed={elapsed:.1f}s timeout={timeout:.1f}s "
                f"events={stream_event_count} stdout_bytes={len(stdout_buffer)} "
                f"stderr_bytes={len(stderr_buffer)}",
                flush=True,
            )

    heartbeat = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat.start()
    proc: subprocess.Popen[bytes] | None = None
    try:
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            effective_argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            **popen_kwargs,
        )
        result.pid = proc.pid
        print(
            f"[CLI] start pid={proc.pid} command={result.command!r} "
            f"timeout={timeout:.1f}s",
            flush=True,
        )
        stdout_reader = threading.Thread(
            target=_drain_pipe,
            args=(proc.stdout, stdout_buffer, True),
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=_drain_pipe,
            args=(proc.stderr, stderr_buffer, False),
            daemon=True,
        )
        stdout_reader.start()
        stderr_reader.start()
        try:
            if input_text is not None and proc.stdin is not None:
                proc.stdin.write(input_text.encode("utf-8"))
                proc.stdin.close()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            result.timed_out = True
            result.exception = exc
            result.timeout_reason = f"wall_clock_timeout_{timeout:.1f}s"
            _terminate_process_tree(proc, kill_grace_seconds)
            try:
                proc.wait(timeout=kill_grace_seconds)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(proc, kill_grace_seconds)
        finally:
            stdout_reader.join(timeout=kill_grace_seconds)
            stderr_reader.join(timeout=kill_grace_seconds)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
            stdout_reader.join(timeout=1.0)
            stderr_reader.join(timeout=1.0)
        result.raw_stdout = bytes(stdout_buffer)
        result.raw_stderr = bytes(stderr_buffer)
        result.returncode = proc.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        result.exception = exc
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1.0)
    result.elapsed_seconds = time.monotonic() - t0
    result.stream_event_count = stream_event_count
    result.stdout = result.raw_stdout.decode("utf-8", errors="replace")
    result.stderr = result.raw_stderr.decode("utf-8", errors="replace")
    print(
        f"[CLI] done pid={result.pid} elapsed={result.elapsed_seconds:.1f}s "
        f"returncode={result.returncode} timed_out={result.timed_out} "
        f"events={result.stream_event_count} tools={result.tool_call_count} "
        f"last_event={result.last_event_type or 'none'} "
        f"timeout_reason={result.timeout_reason or 'none'} "
        f"stdout_bytes={len(result.raw_stdout)} stderr_bytes={len(result.raw_stderr)}",
        flush=True,
    )
    return result


def preflight_check_all(binaries: list[str], version_args: tuple[str, ...] = ("--version",)) -> dict[str, str]:
    """
    实验启动前一次性检测多个 CLI 二进制（Part3"启动前检查"），全部通过才返回。

    Returns:
        {binary_name: version_stdout}

    Raises:
        CLIBinaryNotFoundError: 任意一个缺失（汇总所有缺失项到一条错误里，
            而不是只报第一个，方便用户一次性看到需要安装的全部二进制）。
    """
    versions: dict[str, str] = {}
    missing: list[str] = []
    for binary in binaries:
        try:
            versions[binary] = check_cli_binary(binary, version_args=version_args)
        except CLIBinaryNotFoundError as exc:
            missing.append(str(exc))
    if missing:
        raise CLIBinaryNotFoundError(
            f"{_NOT_INSTALLED_MSG}（共 {len(missing)} 项缺失）:\n" + "\n".join(missing)
        )
    return versions
