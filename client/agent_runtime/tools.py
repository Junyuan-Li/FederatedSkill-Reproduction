"""
tools.py — 内置工具注册表

对应论文 Section 4.1.1:
    'issues tool calls to interact with the task environment'

工具集设计原则：
  - 每个工具对应一类环境交互（代码执行 / 技能检索 / 文件读写）
  - 工具调用生成 TrajectoryStep（role='tool'）
  - 工具结果截断至 K_obs 字符（TrajectoryCompressor 阶段处理）

论文对应：
  Trajectory 中的 tool_calls / tool_results 字段由这里填充。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 单次工具调用超时（秒）
_TOOL_EXEC_TIMEOUT = 15


# ---------------------------------------------------------------------------
# 工具结果
# ---------------------------------------------------------------------------


class ToolResult:
    """
    单次工具调用的结果。

    stdout:        标准输出内容
    stderr:        标准错误内容
    exit_code:     进程退出码（非代码工具为 0）
    error:         工具调用本身的异常（与代码错误区分）
    elapsed_sec:   执行时长
    """

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        error: str = "",
        elapsed_sec: float = 0.0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.error = error
        self.elapsed_sec = elapsed_sec

    @property
    def success(self) -> bool:
        """True iff exit_code == 0 and no tool-level error."""
        return self.exit_code == 0 and not self.error

    def combined_output(self) -> str:
        """合并 stdout + stderr 用于 observation 字段。"""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[STDERR]\n{self.stderr}")
        if self.error:
            parts.append(f"[TOOL ERROR]\n{self.error}")
        return "\n".join(parts) if parts else "(no output)"


# ---------------------------------------------------------------------------
# 内置工具实现
# ---------------------------------------------------------------------------


class BuiltinTools:
    """
    论文 §4.1.1 所述 agentic harness 中的内置工具集。

    提供三类基础工具：
      python_execute  — 在沙箱子进程中运行 Python 代码片段
      skill_search    — 在技能库中检索相关技能（关键词匹配）
      file_write      — 向技能库写入文件（受路径安全限制）
    """

    @staticmethod
    def python_execute(code: str, timeout: float = _TOOL_EXEC_TIMEOUT) -> ToolResult:
        """
        在隔离子进程中执行 Python 代码片段。

        对应论文：agent 通过 'issues tool calls' 与环境交互。
        安全措施：独立子进程 + timeout，不共享解释器状态。

        Args:
            code:    Python 源代码字符串
            timeout: 超时秒数

        Returns:
            ToolResult（stdout/stderr/exit_code）
        """
        t0 = time.monotonic()
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as f:
                f.write(code)
                tmp_path = f.name

            result = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ToolResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                elapsed_sec=time.monotonic() - t0,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                error=f"执行超时（>{timeout}s）",
                exit_code=124,
                elapsed_sec=time.monotonic() - t0,
            )
        except Exception as exc:
            return ToolResult(
                error=f"工具调用异常: {type(exc).__name__}: {exc}",
                exit_code=1,
                elapsed_sec=time.monotonic() - t0,
            )
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def skill_search(
        query: str,
        library_root: Path,
        top_k: int = 3,
    ) -> ToolResult:
        """
        在技能库中搜索与 query 相关的技能（关键词匹配）。

        对应论文 §4.1.1：
            'agent autonomously searches the library for relevant skills'

        Args:
            query:        搜索关键词（空格分割）
            library_root: 技能库根目录
            top_k:        最多返回几个技能

        Returns:
            ToolResult（stdout 包含匹配技能的 SKILL.md 内容）
        """
        try:
            keywords = [k.lower() for k in query.split() if k]
            results: list[tuple[int, Path]] = []  # (分数, SKILL.md 路径)

            for skill_md in library_root.rglob("SKILL.md"):
                content = skill_md.read_text(encoding="utf-8", errors="ignore")
                score = sum(1 for kw in keywords if kw in content.lower())
                if score > 0:
                    results.append((score, skill_md))

            results.sort(key=lambda x: -x[0])
            top_results = results[:top_k]

            if not top_results:
                return ToolResult(stdout="（技能库中未找到相关技能）")

            lines = [f"技能库检索结果 (top {len(top_results)})："]
            for score, skill_path in top_results:
                lines.append(f"\n=== {skill_path.parent.name} (匹配度={score}) ===")
                lines.append(skill_path.read_text(encoding="utf-8", errors="ignore")[:800])

            return ToolResult(stdout="\n".join(lines))

        except Exception as exc:
            return ToolResult(
                error=f"skill_search 异常: {type(exc).__name__}: {exc}",
                exit_code=1,
            )

    @staticmethod
    def file_write(
        rel_path: str,
        content: str,
        library_root: Path,
    ) -> ToolResult:
        """
        向技能库写入文件（受路径安全校验）。

        安全：禁止绝对路径 / 目录穿越（validate_safe_rel_path）。

        Args:
            rel_path:     技能库内的相对路径
            content:      文件内容
            library_root: 技能库根目录

        Returns:
            ToolResult
        """
        from core.datatypes import validate_safe_rel_path

        safe = validate_safe_rel_path(rel_path)
        if safe is None:
            return ToolResult(
                error=f"不安全的路径已拒绝: {rel_path!r}",
                exit_code=1,
            )
        try:
            target = library_root / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return ToolResult(stdout=f"已写入: {safe}")
        except Exception as exc:
            return ToolResult(
                error=f"file_write 异常: {type(exc).__name__}: {exc}",
                exit_code=1,
            )


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------


# 工具函数签名类型（接受 kwargs，返回 ToolResult）
ToolFn = Callable[..., ToolResult]


class ToolRegistry:
    """
    工具注册表 — 按名称索引可调用工具。

    论文 §4.1.1：agent 通过 tool call 与环境交互，
    注册表负责路由 LLM 生成的 function_call → 实际执行。

    内置工具在 register_builtins() 后可用：
      - python_execute
      - skill_search
      - file_write
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}

    def register(self, name: str, fn: ToolFn) -> None:
        """注册一个工具函数。"""
        self._tools[name] = fn

    def register_builtins(self, library_root: Path) -> "ToolRegistry":
        """注册三个内置工具，绑定 library_root。"""
        self.register("python_execute", BuiltinTools.python_execute)
        self.register(
            "skill_search",
            lambda query, top_k=3: BuiltinTools.skill_search(query, library_root, top_k),
        )
        self.register(
            "file_write",
            lambda rel_path, content: BuiltinTools.file_write(rel_path, content, library_root),
        )
        return self

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        """
        按名称调用工具。

        未知工具返回错误 ToolResult，不抛出。
        """
        fn = self._tools.get(name)
        if fn is None:
            return ToolResult(
                error=f"未注册的工具: {name!r}，可用: {list(self._tools.keys())}",
                exit_code=1,
            )
        try:
            return fn(**kwargs)
        except TypeError as exc:
            return ToolResult(
                error=f"工具参数错误 ({name}): {exc}",
                exit_code=1,
            )

    def tool_descriptions(self) -> str:
        """生成工具描述字符串（用于 system prompt）。"""
        return "\n".join(
            f"- {name}({', '.join(fn.__code__.co_varnames[:fn.__code__.co_argcount])})"
            if hasattr(fn, "__code__") else f"- {name}(...)"
            for name, fn in self._tools.items()
        )

    def __len__(self) -> int:
        return len(self._tools)
