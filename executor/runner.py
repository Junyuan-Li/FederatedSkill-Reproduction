"""
runner.py — CommandRunner：工作区内命令执行封装（Tool Calling 层）

[ENGINEERING] 本模块标签：工程实现细节（subprocess 封装），不是论文给出的算法组件。

对应论文 Agent Harness 架构中的 Tool Calling -> Environment 环节：
每次命令执行都建模为一次工具调用（tool_call），结果记录进
Trajectory.actions，供审计和复现分析使用。

"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """一次命令执行的结果，对应 Trajectory.actions 里的一条 run_command 记录。"""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    runtime_seconds: float
    timed_out: bool = False
    exception: dict | None = None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.exception


class CommandRunner:
    """在指定工作区目录内执行命令的 subprocess 封装，带超时保护。"""

    def __init__(self, default_timeout: int = 30) -> None:
        self._default_timeout = default_timeout

    def run_python_file(
        self, filename: str, cwd: Path, timeout: int | None = None,
    ) -> CommandResult:
        """在 cwd 内执行 `<当前解释器> <filename>`。"""
        return self.run([sys.executable, filename], cwd=cwd, timeout=timeout)

    def run(
        self, command: list[str], cwd: Path, timeout: int | None = None,
    ) -> CommandResult:
        """在 cwd 内执行任意命令，捕获 stdout/stderr/returncode，带超时保护。"""
        t_start = time.monotonic()
        eff_timeout = timeout if timeout is not None else self._default_timeout
        try:
            proc = subprocess.run(
                command, cwd=str(cwd), capture_output=True, text=True, timeout=eff_timeout,
            )
            elapsed = time.monotonic() - t_start
            return CommandResult(
                command=command,
                returncode=proc.returncode,
                stdout=proc.stdout[:2000],
                stderr=proc.stderr[:2000],
                runtime_seconds=elapsed,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t_start
            return CommandResult(
                command=command, returncode=-1, stdout="",
                stderr=f"执行超时（>{eff_timeout}s）",
                runtime_seconds=elapsed, timed_out=True,
            )
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            logger.error("CommandRunner 执行异常: %s", exc)
            return CommandResult(
                command=command, returncode=-1, stdout="", stderr=str(exc),
                runtime_seconds=elapsed,
                exception={"type": type(exc).__name__, "message": str(exc)},
            )
