"""
executor.py — TaskExecutor（agent_runtime 版）

对应论文 Section 4.1.1:
    τ_i ~ π_i(·|L_i^t, ρ_i)

执行流程（5 步 pipeline，更完整的 agentic harness）：

    Task x
      │
      ▼  Step 1: 构建 ToolRegistry（注册内置工具）
      │
      ▼  Step 2: 运行 AgentRuntime（Planner-Action-Observation 循环）
      │           → 生成初始 Trajectory（无 reward）
      │
      ▼  Step 3: 从 Trajectory 提取最终代码块（用于 verifier）
      │
      ▼  Step 4: 调用 Verifier 计算 R_{i,x}(τ)
      │
      ▼  Step 5: 填充 reward → 完整 Trajectory τ_i

⚠️  隐私约束：Trajectory 在客户端本地保留，不上传服务器。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.datatypes import Trajectory, WorkerProfile
from core.exceptions import TaskExecutionError
from client.agent_runtime.agent import AgentRuntime
from client.agent_runtime.tools import ToolRegistry

if TYPE_CHECKING:
    from benchmark.task import Task
    from benchmark.verifier import VerificationResult
    from client.library import SkillLibrary
    from llm.router import BackboneRouter

logger = logging.getLogger(__name__)


class TaskExecutor:
    """
    任务执行器：协调 AgentRuntime + Verifier → 完整 Trajectory。

    相比 client/executor.py（简单 subprocess 版），这里的 executor：
    - 使用完整的 AgentRuntime（Planner-Action-Observation 循环）
    - 通过 ToolRegistry 规范化工具调用
    - 保持与 PatchDistiller 相同的接口（返回 Trajectory）

    对应论文：τ_i ~ π_i(·|L_i^t, ρ_i)

    Args:
        router:      BackboneRouter，按 worker_id 路由 LLM 调用
        top_k:       技能检索 top-k 数量（默认 3）
        max_steps:   最大 agent 步数（默认 20，对应 K_STEP）
    """

    def __init__(
        self,
        router: "BackboneRouter",
        top_k: int = 3,
        max_steps: int = 20,
    ) -> None:
        self._router = router
        self._top_k = top_k
        self._max_steps = max_steps

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> Trajectory:
        """
        执行单个任务，返回完整 Trajectory τ_i（含 reward）。

        Args:
            task:       本轮任务 x
            library:    当前技能库 L_i^t
            profile:    worker profile ρ_i
            round_idx:  当前 round 序号 t

        Returns:
            Trajectory τ_i（reward = R_{i,x}(τ)，由 verifier 计算）
        """
        worker_id = profile.client_id
        logger.info(
            "TaskExecutor.run: worker=%s task=%s round=%d",
            worker_id, task.task_id, round_idx,
        )

        # -- Step 1: 构建 ToolRegistry --
        tool_registry = ToolRegistry().register_builtins(library.root())

        # -- Step 2: 运行 AgentRuntime --
        backbone = self._router.get(worker_id)
        agent = AgentRuntime(
            backbone=backbone,
            tool_registry=tool_registry,
            max_steps=self._max_steps,
        )

        try:
            trajectory = agent.run(task, profile, round_idx)
        except Exception as exc:
            logger.error(
                "AgentRuntime 异常: worker=%s task=%s: %s",
                worker_id, task.task_id, exc,
            )
            # 返回失败 trajectory，不中断整个 round
            return Trajectory(
                task_name=task.task_id,
                worker_id=worker_id,
                round_idx=round_idx,
                reward=0.0,
                exception_info={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            )

        # -- Step 3 & 4: 提取最终代码 + Verification --
        reward, vr_output, vr_failures = self._verify(task, trajectory)

        # -- Step 5: 填充 reward 字段 --
        # Pydantic v2 model_copy 保持不可变性
        trajectory = trajectory.model_copy(update={
            "reward": reward,
            "verifier_output": vr_output,
            "verifier_subtest_failures": vr_failures,
        })

        logger.info(
            "TaskExecutor 完成: worker=%s task=%s reward=%.3f steps=%d",
            worker_id, task.task_id, reward, len(trajectory.steps),
        )
        return trajectory

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _verify(
        self,
        task: "Task",
        trajectory: Trajectory,
    ) -> tuple[float, str, list[str]]:
        """
        从 Trajectory 提取最终代码并调用 Verifier。

        对应论文 §4.1.1：
            'evaluated by the environment, which assigns R_{i,x}(τ)'

        Returns:
            (reward, verifier_stdout, failed_subtests)
        """
        from benchmark.verifier import get_verifier
        from client.agent_runtime.agent import AgentRuntime

        # 从最后一个 assistant 步骤提取代码
        final_code = ""
        for step in reversed(trajectory.steps):
            if step.role == "assistant" and step.content:
                code_blocks = AgentRuntime._extract_code_blocks(step.content)
                if code_blocks:
                    final_code = code_blocks[-1]
                    break

        if not final_code:
            # 从 final_message 尝试提取
            if trajectory.final_message:
                code_blocks = AgentRuntime._extract_code_blocks(trajectory.final_message)
                if code_blocks:
                    final_code = code_blocks[-1]

        if not final_code:
            return 0.0, "未能提取有效代码块", []

        try:
            verifier = get_verifier(task.verification)
            vr: VerificationResult = verifier.verify(final_code, task)
            return (
                vr.reward,
                f"success={vr.success} subtests={vr.subtest_results}",
                list(vr.subtest_failures),
            )
        except Exception as exc:
            logger.error("Verification 异常: task=%s: %s", task.task_id, exc)
            return 0.0, f"Verification 异常: {type(exc).__name__}: {exc}", []
