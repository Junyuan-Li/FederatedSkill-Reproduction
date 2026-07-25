"""
verifier.py — 任务验证器

对应论文：R_{i,x}(τ) — 执行奖励计算。

验证器接收生成的代码和任务规格，在沙箱子进程中执行验证逻辑，
返回 VerificationResult（reward ∈ [0, 1]）。

⚠ 安全说明：
  本验证器通过 subprocess 在独立进程中执行 LLM 生成的代码。
  建议在实际部署时配合容器沙箱（如 Docker）使用。
  学术复现环境下，仅在受控任务集上运行，风险可控。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from benchmark.task import Task, VerificationSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 验证结果
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """
    单次任务验证结果。

    reward ∈ {0.0, 1.0}（论文使用二值 reward；部分测试集支持软得分）。
    """

    reward: float                          # 0.0 = 失败，1.0 = 成功
    success: bool                          # True iff reward >= 1.0
    stdout: str = ""
    stderr: str = ""
    subtest_results: dict[str, bool] = field(default_factory=dict)  # {描述: pass/fail}
    subtest_failures: list[str] = field(default_factory=list)        # 失败用例描述
    runtime_seconds: float = 0.0
    exception_info: dict | None = None
    generated_files: list[str] = field(
        default_factory=list,
        # Phase14 任务1新增字段，默认空列表，向后兼容旧调用方（位置/关键字参数均不受影响）：
        # SkillFlowScriptVerifier.run_full_verification() 会填充此字段，记录 agent
        # 生成解答执行后，工作区内新增/修改的文件相对路径（"generated files detection"）。
    )

    @property
    def verifier_score(self) -> float:
        """软得分：通过用例数 / 总用例数；无用例时等于 reward。"""
        if not self.subtest_results:
            return self.reward
        passed = sum(1 for v in self.subtest_results.values() if v)
        return passed / len(self.subtest_results)

    def __str__(self) -> str:
        parts = [f"reward={self.reward:.1f}"]
        if self.subtest_results:
            parts.append(f"subtests={sum(v for v in self.subtest_results.values())}/{len(self.subtest_results)}")
        return f"VerificationResult({', '.join(parts)})"


# ---------------------------------------------------------------------------
# 验证器基类
# ---------------------------------------------------------------------------


class BaseVerifier:
    """验证器基类。"""

    def verify(self, task: "Task", generated_code: str) -> VerificationResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Python 沙箱验证器
# ---------------------------------------------------------------------------


class PythonSandboxVerifier(BaseVerifier):
    """
    在子进程中执行生成代码 + 测试代码，根据退出码判断成功/失败。

    对应 python_test 验证模式：
      生成代码 + test_code 拼接成完整脚本，exit(0)=成功，其余=失败。
    """

    def verify(self, task: "Task", generated_code: str) -> VerificationResult:
        spec = task.verification
        test_code = spec.test_code or ""
        full_script = generated_code.strip() + "\n\n# ---- 验证测试 ----\n" + test_code
        return self._run_script(full_script, spec.timeout_seconds)

    def _run_script(self, script: str, timeout: int) -> VerificationResult:
        t_start = time.monotonic()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(script)
            tmp_path = f.name

        try:
            proc = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = time.monotonic() - t_start
            success = proc.returncode == 0
            return VerificationResult(
                reward=1.0 if success else 0.0,
                success=success,
                stdout=proc.stdout[:2000],
                stderr=proc.stderr[:2000],
                runtime_seconds=elapsed,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t_start
            return VerificationResult(
                reward=0.0, success=False,
                stderr=f"执行超时（>{timeout}s）",
                runtime_seconds=elapsed,
                exception_info={"type": "TimeoutExpired"},
            )
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            return VerificationResult(
                reward=0.0, success=False,
                stderr=str(exc),
                runtime_seconds=elapsed,
                exception_info={"type": type(exc).__name__, "message": str(exc)},
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 函数测试验证器（function_test 模式）
# ---------------------------------------------------------------------------


class FunctionTestVerifier(BaseVerifier):
    """
    注入生成代码中的入口函数，逐个运行 test_cases，计算通过率。

    奖励规则：
      - 全部通过 → reward = 1.0
      - 部分通过 → reward = 0.0（论文使用严格 binary reward）
      - 无测试用例 → reward = 0.0
    """

    def verify(self, task: "Task", generated_code: str) -> VerificationResult:
        spec = task.verification
        if not spec.test_cases:
            return VerificationResult(reward=0.0, success=False, stderr="无测试用例")

        # 构建完整测试脚本
        test_lines = [
            "import json",
            generated_code.strip(),
            "",
            "# ---- 自动生成的函数测试 ----",
            "import sys",
            "_results = {}",
            "_failures = []",
        ]

        for i, tc in enumerate(spec.test_cases):
            desc = tc.description or f"test_case_{i}"
            # ensure_ascii=False 保留中文字符，生成的脚本仍为合法 Python 字符串字面量
            safe_desc = json.dumps(desc, ensure_ascii=False)
            inputs_repr = ", ".join(repr(x) for x in tc.inputs)
            expected_repr = repr(tc.expected)
            # 注意：不在生成代码中使用 f-string / !r 语法，避免转义冲突
            # 改为显式 repr() 拼接，和 _debug_verifier.py 验证的方式一致
            block = f"""
try:
    _out = {spec.entry_point}({inputs_repr})
    _ok = _out == {expected_repr}
    _results[{safe_desc}] = _ok
    if not _ok:
        _failures.append({safe_desc} + ": got " + repr(_out) + ", want " + repr({expected_repr}))
except Exception as _e:
    _results[{safe_desc}] = False
    _failures.append({safe_desc} + ": exception " + type(_e).__name__ + ": " + str(_e))
"""
            test_lines.append(block)

        test_lines += [
            "_total = len(_results)",
            "_passed = sum(1 for v in _results.values() if v)",
            'print(json.dumps({"passed": _passed, "total": _total, "failures": _failures}, ensure_ascii=False))',
            "sys.exit(0 if _passed == _total else 1)",
        ]

        full_script = "\n".join(test_lines)
        t_start = time.monotonic()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(full_script)
            tmp_path = f.name

        try:
            proc = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True, text=True,
                timeout=spec.timeout_seconds,
            )
            elapsed = time.monotonic() - t_start

            # 解析 JSON 输出
            subtest_results: dict[str, bool] = {}
            subtest_failures: list[str] = []
            try:
                parsed = json.loads(proc.stdout.strip().split("\n")[-1])
                passed = parsed.get("passed", 0)
                total = parsed.get("total", 1)
                subtest_failures = parsed.get("failures", [])
                # 重建 subtest_results（简化：只记录 pass/fail 计数）
                subtest_results = {
                    f"case_{i}": (i < passed)
                    for i in range(total)
                }
            except Exception:
                pass

            success = proc.returncode == 0
            return VerificationResult(
                reward=1.0 if success else 0.0,
                success=success,
                stdout=proc.stdout[:2000],
                stderr=proc.stderr[:2000],
                subtest_results=subtest_results,
                subtest_failures=subtest_failures,
                runtime_seconds=elapsed,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t_start
            return VerificationResult(
                reward=0.0, success=False,
                stderr=f"执行超时（>{spec.timeout_seconds}s）",
                runtime_seconds=elapsed,
                exception_info={"type": "TimeoutExpired"},
            )
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            return VerificationResult(
                reward=0.0, success=False,
                stderr=str(exc),
                runtime_seconds=elapsed,
                exception_info={"type": type(exc).__name__, "message": str(exc)},
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 输出匹配验证器（output_match 模式）
# ---------------------------------------------------------------------------


class OutputMatchVerifier(BaseVerifier):
    """
    执行生成代码，检查 stdout 中是否包含期望字符串。
    """

    def verify(self, task: "Task", generated_code: str) -> VerificationResult:
        spec = task.verification
        expected = spec.expected_output or ""
        result = PythonSandboxVerifier()._run_script(
            generated_code.strip(), spec.timeout_seconds
        )
        if expected in result.stdout:
            return VerificationResult(
                reward=1.0, success=True,
                stdout=result.stdout, stderr=result.stderr,
                runtime_seconds=result.runtime_seconds,
            )
        return VerificationResult(
            reward=0.0, success=False,
            stdout=result.stdout, stderr=result.stderr,
            runtime_seconds=result.runtime_seconds,
        )


# ---------------------------------------------------------------------------
# SkillFlow 脚本验证器（skillflow_script 模式，真实 SkillFlow 任务用）
# ---------------------------------------------------------------------------


class SkillFlowScriptVerifier(BaseVerifier):
    """
    真实 SkillFlow 任务的验证器：在**已经填好 agent 产出物的工作区目录**里
    执行 test_script（cwd=workspace_dir），退出码 0 视为通过。

    与其他三种验证器不同，本验证器不对"单段生成代码"操作，而是对接
    executor/skillflow_executor.py 的临时工作区流程（对应真实 SkillFlow
    任务的 tests/ 目录里的验证脚本，需要在包含 environment/ 输入文件 +
    agent 生成文件的完整目录下运行）。

    因此不通过通用 verify(task, generated_code) 接口调用——那里调用会
    抛出 NotImplementedError，明确提示改用 verify_in_workspace()。
    """

    def verify(self, task: "Task", generated_code: str) -> VerificationResult:
        raise NotImplementedError(
            "SkillFlowScriptVerifier 不支持通用 verify() 接口，"
            "请使用 verify_in_workspace(task, workspace_dir)，"
            "由 executor/skillflow_executor.py 调用。"
        )

    def verify_in_workspace(self, task: "Task", workspace_dir: Path) -> VerificationResult:
        """在 workspace_dir（已含 task.files + agent 生成文件）内执行 test_script。"""
        spec = task.verification
        script = spec.test_script or ""
        script_path, use_pytest, test_files = self._materialize_test_script(script, workspace_dir)

        t_start = time.monotonic()
        try:
            command = (
                [sys.executable, "-m", "pytest", str(script_path), "-q"]
                if use_pytest else [sys.executable, str(script_path)]
            )
            proc = subprocess.run(
                command,
                cwd=str(workspace_dir),
                capture_output=True, text=True, timeout=spec.timeout_seconds,
            )
            elapsed = time.monotonic() - t_start
            success = proc.returncode == 0
            return VerificationResult(
                reward=1.0 if success else 0.0,
                success=success,
                stdout=proc.stdout[:2000],
                stderr=proc.stderr[:2000],
                runtime_seconds=elapsed,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t_start
            return VerificationResult(
                reward=0.0, success=False,
                stderr=f"执行超时（>{spec.timeout_seconds}s）",
                runtime_seconds=elapsed,
                exception_info={"type": "TimeoutExpired"},
            )
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            return VerificationResult(
                reward=0.0, success=False,
                stderr=str(exc),
                runtime_seconds=elapsed,
                exception_info={"type": type(exc).__name__, "message": str(exc)},
            )
        finally:
            for path in test_files:
                path.unlink(missing_ok=True)

    _TEST_FILE_MARKER_RE = re.compile(r"^# --- (?P<filename>[^\r\n]+\.py) ---\s*$", re.MULTILINE)

    @classmethod
    def _materialize_test_script(cls, script: str, workspace_dir: Path) -> tuple[Path, bool, list[Path]]:
        """把 SkillFlow 拼接测试恢复为文件，返回入口脚本和是否用 pytest。"""
        parts = list(cls._TEST_FILE_MARKER_RE.finditer(script))
        if not parts:
            script_path = workspace_dir / "_skillflow_test.py"
            script_path.write_text(script, encoding="utf-8")
            return script_path, False, [script_path]

        entry_path: Path | None = None
        test_files: list[Path] = []
        for index, match in enumerate(parts):
            filename = match.group("filename").strip()
            start = match.end()
            end = parts[index + 1].start() if index + 1 < len(parts) else len(script)
            content = script[start:end].lstrip("\r\n")
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"非法测试文件路径: {filename!r}")
            path = workspace_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            test_files.append(path)
            if entry_path is None:
                entry_path = path

        if entry_path is None:
            script_path = workspace_dir / "_skillflow_test.py"
            script_path.write_text(script, encoding="utf-8")
            return script_path, False, [script_path]
        return entry_path, True, test_files

    # -----------------------------------------------------------------
    # Phase14 任务1新增：自包含的"真实 SkillFlow benchmark execution"入口
    # -----------------------------------------------------------------

    def run_full_verification(
        self,
        task: "Task",
        generated_code: str,
        workspace_root: Path | None = None,
    ) -> VerificationResult:
        """
        独立、自包含地完成一次真实 SkillFlow 任务验证的全部五个步骤：

            1. workspace preparation      —— 创建临时工作区目录
            2. task files copy            —— 写入 task.files（environment/ 输入文件）
            3. （写入并执行 agent 生成的解答代码 generated_code）
            4. generated files detection  —— 对比执行前后的文件快照，识别新增/修改文件
            5. verification script execution + reward calculation
               —— 委托给现有 verify_in_workspace()，不重复实现 subprocess 执行逻辑

        与 verify_in_workspace() 的区别：verify_in_workspace() 假定调用方
        （executor/skillflow_executor.py::SkillFlowTaskExecutor.run()）已经
        准备好工作区（写好 task.files + agent 生成文件）；本方法不依赖
        executor，可独立用于离线重新验证/审计/单测。两者互不冲突，
        executor 现有调用路径（verify_in_workspace）完全不受影响。

        Args:
            task: 待验证任务（真实 SkillFlow 任务，verification.type 通常为
                "skillflow_script"）
            generated_code: agent 生成的解答代码，会被写入
                task.metadata["solution_filename"]（默认 "solution.py"）并执行
            workspace_root: 若提供，则工作区固定建在该目录（不清理，便于调试/
                审计留痕）；不提供则使用临时目录，函数返回前自动清理

        Returns:
            VerificationResult，相比 verify_in_workspace() 额外填充：
              - generated_files: 执行 generated_code 后新增/修改的文件相对路径列表
              - stdout/stderr:  额外前置了 generated_code 自身的执行输出
        """
        solution_filename = task.metadata.get("solution_filename", "solution.py")

        do_cleanup = workspace_root is None
        workspace_dir = (
            Path(workspace_root)
            if workspace_root is not None
            else Path(tempfile.mkdtemp(prefix=f"skillflow_full_{task.task_id}_"))
        )
        workspace_dir.mkdir(parents=True, exist_ok=True)

        t_start = time.monotonic()
        try:
            # -- 1+2: workspace preparation + task files copy --
            for rel_path, content in task.files.items():
                dest = workspace_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")

            files_before = self._snapshot_files(workspace_dir)

            # -- 3: 写入并执行生成的解答代码 --
            solution_path = workspace_dir / solution_filename
            solution_path.parent.mkdir(parents=True, exist_ok=True)
            solution_path.write_text(generated_code, encoding="utf-8")

            exec_stdout, exec_stderr = "", ""
            timeout = task.verification.timeout_seconds
            try:
                proc = subprocess.run(
                    [sys.executable, str(solution_path)],
                    cwd=str(workspace_dir),
                    capture_output=True, text=True, timeout=timeout,
                )
                exec_stdout, exec_stderr = proc.stdout, proc.stderr
            except subprocess.TimeoutExpired:
                exec_stderr = f"生成解答执行超时（>{timeout}s）"
            except Exception as exc:
                exec_stderr = f"生成解答执行异常: {type(exc).__name__}: {exc}"

            # -- 4: generated files detection（前后文件快照对比） --
            files_after = self._snapshot_files(workspace_dir)
            generated_files = sorted(
                rel for rel, mtime in files_after.items()
                if rel != solution_filename
                and (rel not in files_before or files_before[rel] != mtime)
            )

            # -- 5: verification script execution + reward calculation --
            result = self.verify_in_workspace(task, workspace_dir)
            result.stdout = (exec_stdout + "\n" + result.stdout)[:2000]
            result.stderr = (exec_stderr + "\n" + result.stderr)[:2000]
            result.runtime_seconds = time.monotonic() - t_start
            result.generated_files = generated_files
            return result
        finally:
            if do_cleanup:
                shutil.rmtree(workspace_dir, ignore_errors=True)

    @staticmethod
    def _snapshot_files(workspace_dir: Path) -> dict[str, float]:
        """记录工作区内每个文件的 相对路径 -> mtime 快照，用于前后对比检测新增/修改文件。"""
        snapshot: dict[str, float] = {}
        for p in workspace_dir.rglob("*"):
            if p.is_file():
                snapshot[str(p.relative_to(workspace_dir))] = p.stat().st_mtime
        return snapshot


# ---------------------------------------------------------------------------
# Docker 容器验证器（docker 模式，Gap2 新增）
# ---------------------------------------------------------------------------


class DockerScriptVerifier(BaseVerifier):
    """
    真实 Docker 容器验证器，对应 VerificationSpec.type == "docker"（Gap2）。

    与 SkillFlowScriptVerifier 一样，需要一个已填好 agent 产出物的工作区
    目录（而非单段代码字符串），因此不支持通用 verify() 接口，请使用
    verify_in_workspace()。真正的 docker build/run 逻辑委托给
    executor/harbor_adapter.py::HarborExecutor（避免在 verifier.py 里重复
    实现 subprocess docker 调用，也避免循环导入）。
    """

    def verify(self, task: "Task", generated_code: str) -> VerificationResult:
        raise NotImplementedError(
            "DockerScriptVerifier 不支持通用 verify() 接口，"
            "请使用 verify_in_workspace(task, workspace_dir)，"
            "由 executor/skillflow_executor.py 调用。"
        )

    def verify_in_workspace(self, task: "Task", workspace_dir: Path) -> VerificationResult:
        """在 workspace_dir（需已含 Dockerfile + task.files + agent 生成文件）内构建镜像并执行。"""
        from executor.harbor_adapter import HarborExecutor
        return HarborExecutor().run_verification(task, workspace_dir)


# ---------------------------------------------------------------------------
# 无验证器（none 模式，诚实兜底，非 bug）
# ---------------------------------------------------------------------------


class NoVerificationVerifier(BaseVerifier):
    """
    对应 VerificationSpec.type == "none"。

    真实 SkillFlow 的 20 个 family（benchmark/families/*.json 中仅含官方
    任务描述、尚未接入官方 Harbor 容器测试脚本的那些 family）目前没有
    可执行的验证逻辑——这是数据集本身的限制（官方验证依赖 Harbor 容器内的
    专有测试脚本和真实 environment/ 输入文件，本仓库未下载/未接入这部分），
    不是代码 bug。

    在本类注册之前，get_verifier("none") 找不到注册项会抛 ValueError，
    被 client/executor.py::_verify() 的通用 except 捕获后包装成一条
    "验证器内部异常" 消息——这会掩盖"任务本来就没有验证脚本"这个真实
    原因，让人误以为是运行时错误。改为显式注册后，调用方能拿到清晰的
    exception_info={"type": "NoVerificationDefined"}，reward 明确记为 0.0
    （保守策略：视为"未验证=不计入成功"，不会被误当成某种"部分成功"）。
    """

    def verify(self, task: "Task", generated_code: str) -> VerificationResult:
        return VerificationResult(
            reward=0.0,
            success=False,
            stderr=(
                f"任务 {task.task_id} 的 verification.type='none'，没有可执行的验证脚本"
                "（真实 SkillFlow 官方 Harbor 测试尚未接入），本次统一记为 reward=0.0，"
                "不代表生成代码本身错误。"
            ),
            exception_info={"type": "NoVerificationDefined"},
        )


# ---------------------------------------------------------------------------
# 验证器注册表
# ---------------------------------------------------------------------------


_VERIFIER_REGISTRY: dict[str, type[BaseVerifier]] = {
    "python_test": PythonSandboxVerifier,
    "function_test": FunctionTestVerifier,
    "output_match": OutputMatchVerifier,
    "skillflow_script": SkillFlowScriptVerifier,
    "docker": DockerScriptVerifier,
    "none": NoVerificationVerifier,
}


def get_verifier(verification_type: str) -> BaseVerifier:
    """根据 VerificationSpec.type 获取对应验证器实例。"""
    cls = _VERIFIER_REGISTRY.get(verification_type)
    if cls is None:
        raise ValueError(f"未知的验证类型: {verification_type!r}，支持: {list(_VERIFIER_REGISTRY)}")
    return cls()
