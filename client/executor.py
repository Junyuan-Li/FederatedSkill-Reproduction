"""
executor.py — 任务执行器（TaskExecutor）

对应论文公式：
    τ_i ~ π_i(·|L_i^t, ρ_i)

完整执行流程（5 步）：
  Step 1  Skill Retrieval  — 从 L_i^t 检索相关技能
  Step 2  Prompt Build     — 构建代码生成 prompt（技能 + 任务描述）
  Step 3  LLM Generation   — 调用 backbone 生成 Python 代码
  Step 4  Sandboxed Run    — 子进程执行代码（含超时保护）
  Step 5  Verification     — 调用 Verifier 计算 R_{i,x}(τ)

返回 Trajectory（含 reward），由 PatchDistiller 进一步蒸馏为 WorkerPatch。

"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.constants import MAX_TRAJECTORY_PROMPT_CHARS
from core.datatypes import Trajectory, TrajectoryStep, WorkerProfile
from core.exceptions import LLMCallError
from llm.router import BackboneRouter

if TYPE_CHECKING:
    from benchmark.task import Task
    from benchmark.verifier import VerificationResult
    from client.library import SkillLibrary

logger = logging.getLogger(__name__)

# 单次执行的代码生成最大 tokens（不依赖 constants，单独控制）
_CODE_GEN_MAX_TOKENS = 2048
# 传给 LLM 的最大技能字符数（避免 prompt 过长）
_MAX_SKILL_PROMPT_CHARS = 4000
# 代码执行超时（verifier 内已有 per-task timeout，这里是全局保护）
_GLOBAL_EXEC_TIMEOUT = 30

# 内层 pytest 进度条正则：匹配 ".FFFFFF [100%]" 形式
# 与官方 patcher_bridge.py 的 _INNER_DOT_RE 一致
_INNER_DOT_RE = re.compile(r"([.F]{2,})\s*\[100%\]")


def _compute_soft_reward(verifier_output: str, hard_reward: float | None) -> float:
    """
    计算软奖励（sub-test 通过率），匹配官方 patcher_bridge._compute_soft_reward()。

    规则：
      - hard_reward >= 1.0 → soft = 1.0（verifier 整体通过）
      - 否则解析内层 pytest 进度条（如 ".FFFFFF [100%]"），计算 p/(p+f)
      - 若无法解析则退化为 hard_reward（worst-case，不超过 hard）

    Args:
        verifier_output:  verifier stdout 字符串（含 pytest 输出）
        hard_reward:      外层二值奖励

    Returns:
        soft_reward ∈ [0.0, 1.0]
    """
    if hard_reward is not None and hard_reward >= 1.0:
        return 1.0
    m = _INNER_DOT_RE.search(verifier_output or "")
    if not m:
        return float(hard_reward or 0.0)
    run = m.group(1)
    p = run.count(".")
    f = run.count("F")
    if p + f == 0:
        return float(hard_reward or 0.0)
    return p / (p + f)


# ---------------------------------------------------------------------------
# TaskExecutor
# ---------------------------------------------------------------------------


class TaskExecutor:
    """
    任务执行器，将 Task × SkillLibrary × WorkerProfile 映射为 Trajectory。

    Args:
        router:          BackboneRouter，按 worker_id 路由 LLM 调用
        top_k_skills:    检索时返回的最大相关技能数（默认 3）
    """

    def __init__(
        self,
        router: BackboneRouter,
        top_k_skills: int = 3,
    ) -> None:
        self._router = router
        self._top_k = top_k_skills

    # ------------------------------------------------------------------
    # 主接口
    # ------------------------------------------------------------------

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> Trajectory:
        """
        执行单个任务，返回完整 Trajectory（含 reward）。

        Args:
            task:       本轮要执行的 Task x
            library:    当前 worker 的技能库 L_i^t
            profile:    worker profile ρ_i
            round_idx:  当前 round 序号 t

        Returns:
            Trajectory τ_i（包含 reward = R_{i,x}(τ)）
        """
        t_start = time.monotonic()
        worker_id = profile.client_id
        steps: list[TrajectoryStep] = []
        total_tokens = 0
        total_cost = 0.0

        logger.info("TaskExecutor: worker=%s task=%s round=%d", worker_id, task.task_id, round_idx)

        # -- Step 1: Skill Retrieval --
        relevant_entries = self._retrieve_skills(task, library)
        skills_text = self._format_skills(relevant_entries)
        steps.append(TrajectoryStep(
            step_index=0,
            role="user",
            content=f"[Skill Retrieval] 检索到 {len(relevant_entries)} 个相关技能",
            observation="\n".join(e.path for e in relevant_entries) if relevant_entries else "（无相关技能）",
        ))

        # -- Step 2: Prompt Build --
        system_prompt = self._build_system_prompt(profile)
        user_prompt = self._build_user_prompt(task, skills_text)
        steps.append(TrajectoryStep(
            step_index=1,
            role="user",
            content=f"[Prompt] {user_prompt[:300]}…",
        ))

        # -- Step 3: LLM Generation --
        generated_code = ""
        exception_info: dict[str, Any] | None = None

        try:
            backbone = self._router.get(worker_id)
            llm_result = backbone.call(user_prompt, system_prompt)
            total_tokens += llm_result.total_tokens
            total_cost += llm_result.cost_usd
            generated_code = self._extract_code(llm_result.text)

            steps.append(TrajectoryStep(
                step_index=2,
                role="assistant",
                content=llm_result.text[:MAX_TRAJECTORY_PROMPT_CHARS],
                tokens_used=llm_result.total_tokens,
            ))
            logger.debug("代码生成完成: worker=%s tokens=%d", worker_id, llm_result.total_tokens)

        except LLMCallError as exc:
            logger.error("LLM 调用失败 worker=%s task=%s: %s", worker_id, task.task_id, exc)
            exception_info = {"exception_type": type(exc).__name__, "exception_message": str(exc)}
            steps.append(TrajectoryStep(
                step_index=2,
                role="assistant",
                content=f"[LLM 调用失败] {exc}",
            ))

        # -- Step 4: Sandboxed Execution (预执行，不验证，收集 stdout/stderr) --
        exec_stdout = ""
        exec_stderr = ""
        if generated_code:
            exec_stdout, exec_stderr = self._run_code_raw(generated_code, timeout=_GLOBAL_EXEC_TIMEOUT)
            steps.append(TrajectoryStep(
                step_index=3,
                role="tool",
                content=generated_code[:2000],
                tool_results=[{"stdout": exec_stdout[:500], "stderr": exec_stderr[:500]}],
                observation=(exec_stdout or exec_stderr)[:300],
            ))

        # -- Step 5: Verification --
        verification = self._verify(task, generated_code)
        reward = verification.reward
        # 软奖励：解析内层 pytest 进度条（匹配官方 patcher_bridge._compute_soft_reward）
        soft_reward = _compute_soft_reward(verification.stdout or str(verification), reward)

        steps.append(TrajectoryStep(
            step_index=4,
            role="user",
            content=f"[Verifier] {verification}",
            observation="\n".join(verification.subtest_failures[:5]),
        ))

        elapsed = time.monotonic() - t_start
        logger.info(
            "TaskExecutor 完成: worker=%s task=%s reward=%.1f soft=%.3f elapsed=%.2fs",
            worker_id, task.task_id, reward, soft_reward, elapsed,
        )

        return Trajectory(
            task_name=task.task_id,
            worker_id=worker_id,
            round_idx=round_idx,
            steps=steps,
            stdout=exec_stdout[:2000],
            stderr=exec_stderr[:2000],
            final_message=generated_code[:1000],
            reward=reward,
            soft_reward=soft_reward,
            verifier_output=str(verification),
            verifier_subtest_failures=verification.subtest_failures,
            total_tokens=total_tokens,
            runtime_seconds=elapsed,
            cost_usd=total_cost,
            exception_info=exception_info,
        )

    # ------------------------------------------------------------------
    # Step 1: Skill Retrieval
    # ------------------------------------------------------------------

    def _retrieve_skills(
        self, task: "Task", library: "SkillLibrary"
    ) -> list[Any]:
        """
        从技能库检索与任务最相关的技能，返回 LibraryFileEntry 列表。

        策略：
          1. Tag 精确匹配（required_skills 字段）
          2. 关键词重叠评分（任务描述 vs 技能文件名/内容首200字）
          3. 取 top-k
        """
        try:
            snapshot = library.snapshot(round_idx=0)
        except Exception:
            return []

        entries = snapshot.files
        if not entries:
            return []

        task_keywords = set(
            re.findall(r"\b[a-z_]{3,}\b", task.description.lower())
        ) | set(task.required_skills)

        scored: list[tuple[float, Any]] = []
        for entry in entries:
            score = 0.0
            path_lower = entry.path.lower()
            # 文件名关键词匹配
            for kw in task_keywords:
                if kw in path_lower:
                    score += 2.0
            # 内容关键词匹配（只看首 200 字）
            excerpt = entry.content[:200].lower()
            for kw in task_keywords:
                if kw in excerpt:
                    score += 1.0
            # 优先 SKILL.md 文件
            if path_lower.endswith("skill.md"):
                score += 0.5
            scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [e for _, e in scored[: self._top_k] if _ > 0]
        return top

    def _format_skills(self, entries: list[Any]) -> str:
        """将检索到的技能文件格式化为 prompt 友好的文本块。"""
        if not entries:
            return "（当前技能库为空，请从头实现）"
        parts = []
        total = 0
        for entry in entries:
            block = f"### {entry.path}\n```\n{entry.content}\n```\n"
            if total + len(block) > _MAX_SKILL_PROMPT_CHARS:
                remaining = _MAX_SKILL_PROMPT_CHARS - total
                if remaining > 100:
                    parts.append(block[:remaining] + "\n…（截断）\n")
                break
            parts.append(block)
            total += len(block)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Step 2: Prompt Build
    # ------------------------------------------------------------------

    def _build_system_prompt(self, profile: WorkerProfile) -> str:
        harness = profile.agent_harness.lower()
        if "claude" in harness:
            style = "你擅长编写简洁、地道的 Python 代码，使用类型注解，避免全局变量。"
        elif "qwen" in harness:
            style = "你擅长编写高效的 Python 代码，直接给出完整的函数实现。"
        else:
            style = "你擅长编写正确的 Python 代码。"

        return (
            f"你是一个专业的 Python 编程助手。{style}\n"
            "要求：\n"
            "  1. 只生成 Python 代码，不要包含任何解释文字（代码块外）。\n"
            "  2. 用 ```python ... ``` 包裹代码。\n"
            "  3. 不使用外部库（除非任务明确要求）。\n"
            "  4. 函数名必须与任务描述完全一致。\n"
        )

    def _build_user_prompt(self, task: "Task", skills_text: str) -> str:
        return (
            f"## 任务\n{task.description}\n\n"
            f"## 已有技能参考\n{skills_text}\n\n"
            "## 要求\n"
            "请根据任务描述，生成完整的 Python 函数实现。"
            "直接输出代码块，不需要其他说明。"
        )

    # ------------------------------------------------------------------
    # Step 3: Code Extraction
    # ------------------------------------------------------------------

    def _extract_code(self, llm_response: str) -> str:
        """从 LLM 响应中提取代码块（```python ... ``` 或裸代码）。"""
        # 优先提取 ```python ... ``` 块
        pattern = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
        matches = pattern.findall(llm_response)
        if matches:
            return "\n\n".join(m.strip() for m in matches)
        # 降级：尝试提取第一段看起来像 def 的代码
        def_match = re.search(r"(def\s+\w+.*)", llm_response, re.DOTALL)
        if def_match:
            return def_match.group(1).strip()
        # 最后降级：整个响应
        return llm_response.strip()

    # ------------------------------------------------------------------
    # Step 4: Raw Execution（收集 stdout/stderr，不影响 reward）
    # ------------------------------------------------------------------

    def _run_code_raw(self, code: str, timeout: int) -> tuple[str, str]:
        """在子进程中执行代码，返回 (stdout, stderr)。不影响 reward。"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp = f.name
        try:
            proc = subprocess.run(
                [sys.executable, tmp],
                capture_output=True, text=True, timeout=timeout,
            )
            return proc.stdout[:2000], proc.stderr[:2000]
        except subprocess.TimeoutExpired:
            return "", f"执行超时（>{timeout}s）"
        except Exception as exc:
            return "", str(exc)
        finally:
            Path(tmp).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Step 5: Verification
    # ------------------------------------------------------------------

    def _verify(self, task: "Task", generated_code: str) -> "VerificationResult":
        """调用对应的 Verifier 计算 R_{i,x}(τ)。"""
        from benchmark.verifier import get_verifier, VerificationResult

        if not generated_code.strip():
            return VerificationResult(
                reward=0.0, success=False,
                stderr="生成代码为空",
            )
        try:
            verifier = get_verifier(task.verification.type)
            return verifier.verify(task, generated_code)
        except Exception as exc:
            logger.error("验证器异常 task=%s: %s", task.task_id, exc)
            return VerificationResult(
                reward=0.0, success=False,
                stderr=f"验证器内部异常: {exc}",
                exception_info={"type": type(exc).__name__, "message": str(exc)},
            )
