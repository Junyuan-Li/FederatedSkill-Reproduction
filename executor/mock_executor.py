"""
mock_executor.py

用途：
  验证 server/planner、server/merge、evaluation 等下游管线的正确性时，
  不需要每次都走真实（或即使 mock 的）LLM 调用 + 代码沙箱执行，
  直接按预设 reward 生成 Trajectory，加快测试速度、避免测试脆弱。

与 client.executor.TaskExecutor 保持相同的 .run() 接口，可直接替换使用。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from core.datatypes import Trajectory, TrajectoryStep, WorkerProfile

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary

logger = logging.getLogger(__name__)


class MockExecutor:
    """
    Mock 执行器：不调用 LLM、不跑 subprocess，直接返回构造好的 Trajectory。

    Args:
        default_reward:  未显式指定 reward_fn 时，所有任务的默认 reward
        canned_code:     写入 Trajectory.final_message 的固定"生成代码"文本
        reward_fn:       可选，(task) -> float，用于按任务定制 reward
                         （例如模拟"某些任务总是失败"）
    """

    def __init__(
        self,
        default_reward: float = 1.0,
        canned_code: str = "# mock generated code\n",
        reward_fn=None,
    ) -> None:
        self._default_reward = default_reward
        self._canned_code = canned_code
        self._reward_fn = reward_fn

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> Trajectory:
        t_start = time.monotonic()
        reward = self._reward_fn(task) if self._reward_fn else self._default_reward

        steps = [
            TrajectoryStep(
                step_index=0, role="user",
                content=f"[Mock] task={task.task_id}",
            ),
            TrajectoryStep(
                step_index=1, role="assistant",
                content=self._canned_code,
            ),
            TrajectoryStep(
                step_index=2, role="user",
                content=f"[Mock Verifier] reward={reward}",
            ),
        ]

        elapsed = time.monotonic() - t_start
        logger.debug(
            "MockExecutor: worker=%s task=%s reward=%.1f", profile.client_id, task.task_id, reward
        )
        return Trajectory(
            task_name=task.task_id,
            worker_id=profile.client_id,
            round_idx=round_idx,
            steps=steps,
            final_message=self._canned_code,
            reward=reward,
            verifier_output=f"[mock] reward={reward}",
            total_tokens=0,
            runtime_seconds=elapsed,
            cost_usd=0.0,
        )
