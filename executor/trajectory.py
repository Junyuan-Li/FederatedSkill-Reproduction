"""
trajectory.py — TrajectoryCollector：agent workspace 模式下的 Trajectory 构建器

[ENGINEERING] 本模块标签：工程实现细节（轨迹采集器），不是论文给出的算法组件。

对应论文核心公式：
    τ_i ~ π_i(·|L_i^t, ρ_i)

与 client/trajectory.py::TrajectoryCompressor 的区别（两者职责完全不同，不冲突）：
  - client/trajectory.py::TrajectoryCompressor —— 蒸馏阶段：把已经产出的完整
    Trajectory 压缩为 CompactedTrajectory（供 PatchDistiller 使用）
  - executor/trajectory.py::TrajectoryCollector（本模块）—— 执行阶段：在
    AgentWorkspaceExecutor 执行过程中，逐步累积 steps / actions / tool_calls /
    generated_files / exceptions，最终 finalize() 成一个完整的 Trajectory

TrajectoryCollector 产出的 Trajectory 使用 core.datatypes.Trajectory 里
Phase12 新增的 actions / generated_files / exceptions 字段，以及
tool_calls / token_usage 只读派生字段。
"""

from __future__ import annotations

import time
from typing import Any

from core.datatypes import Trajectory, TrajectoryStep


class TrajectoryCollector:
    """增量收集一次任务执行过程中的所有信息，最后一次性 finalize() 为 Trajectory。"""

    def __init__(self, task_name: str, worker_id: str, round_idx: int) -> None:
        self._task_name = task_name
        self._worker_id = worker_id
        self._round_idx = round_idx
        self._t_start = time.monotonic()
        self._steps: list[TrajectoryStep] = []
        self._actions: list[dict[str, Any]] = []
        self._generated_files: list[str] = []
        self._exceptions: list[dict[str, Any]] = []
        self._execution_logs: list[dict[str, Any]] = []
        self._total_tokens = 0
        self._cost_usd = 0.0
        self._stdout = ""
        self._stderr = ""
        self._final_message = ""

    # ------------------------------------------------------------------
    # 累积接口
    # ------------------------------------------------------------------

    def add_step(self, **kwargs: Any) -> TrajectoryStep:
        """添加一个 TrajectoryStep（role/content/tool_calls/tool_results/observation 等）。"""
        step = TrajectoryStep(step_index=len(self._steps), **kwargs)
        self._steps.append(step)
        return step

    def add_action(self, action_type: str, **detail: Any) -> None:
        """记录一次高层动作（setup_workspace / skill_retrieval / write_file / run_command / verify 等）。"""
        self._actions.append({"type": action_type, **detail})

    def add_exception(self, exc: Exception, context: str = "") -> None:
        """记录一次异常（agent workspace 模式下可能在多个步骤各自抛出异常）。"""
        self._exceptions.append({
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "context": context,
        })

    def add_generated_files(self, paths: list[str]) -> None:
        for p in paths:
            if p not in self._generated_files:
                self._generated_files.append(p)

    def add_execution_log(self, stage: str, **detail: Any) -> None:
        """官方对齐 Part2/Part3：记录 harness 强制执行生成代码这一步骤的日志
        （区别于 add_action("cli_invoke", ...) 记录的是 agent 自己的 CLI session）。"""
        self._execution_logs.append({"stage": stage, **detail})

    def add_tokens(self, tokens: int, cost_usd: float = 0.0) -> None:
        self._total_tokens += tokens
        self._cost_usd += cost_usd

    def set_stdio(self, stdout: str = "", stderr: str = "") -> None:
        self._stdout = stdout[:2000]
        self._stderr = stderr[:2000]

    def set_final_message(self, message: str) -> None:
        self._final_message = message[:1000]

    # ------------------------------------------------------------------
    # 结束：产出 Trajectory
    # ------------------------------------------------------------------

    def finalize(
        self,
        reward: float | None,
        verifier_output: str = "",
        verifier_subtest_failures: list[str] | None = None,
        soft_reward: float | None = None,
    ) -> Trajectory:
        """把已积累的所有信息组装为一个完整 Trajectory（τ_i）。"""
        elapsed = time.monotonic() - self._t_start
        exception_info = self._exceptions[-1] if self._exceptions else None
        return Trajectory(
            task_name=self._task_name,
            worker_id=self._worker_id,
            round_idx=self._round_idx,
            steps=self._steps,
            stdout=self._stdout,
            stderr=self._stderr,
            final_message=self._final_message,
            reward=reward,
            soft_reward=soft_reward,
            verifier_output=verifier_output,
            verifier_subtest_failures=verifier_subtest_failures or [],
            total_tokens=self._total_tokens,
            runtime_seconds=elapsed,
            cost_usd=self._cost_usd,
            exception_info=exception_info,
            actions=self._actions,
            generated_files=self._generated_files,
            exceptions=self._exceptions,
            execution_logs=self._execution_logs,
        )
