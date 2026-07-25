"""
base.py — BaseExecutor：所有任务执行器的抽象基类

对应论文 Agent Harness 架构（Section 4.1.1）：

    Model -> Agent Framework -> Skill Retrieval -> Tool Calling -> Environment -> Test

以及核心公式：
    τ_i ~ π_i(·|L_i^t, ρ_i)

约束：run() 必须返回 core.datatypes.Trajectory，禁止返回裸 dict。

"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from core.datatypes import Trajectory, WorkerProfile

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary


class BaseExecutor(ABC):
    """
    任务执行器抽象基类。

    子类必须实现 run()，且 run() 必须返回 Trajectory
    （对应论文 τ_i ~ π_i(·|L_i^t, ρ_i)），不允许返回裸 dict。
    """

    @abstractmethod
    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> Trajectory:
        """执行单个任务，返回完整 Trajectory τ_i。"""
        raise NotImplementedError


def _register_existing_executors() -> None:
    """把既有执行器实现注册为 BaseExecutor 的虚拟子类（不修改其源码）。"""
    from executor.mock_executor import MockExecutor
    from executor.python_executor import PythonTaskExecutor
    from executor.skillflow_executor import SkillFlowTaskExecutor

    BaseExecutor.register(MockExecutor)
    BaseExecutor.register(PythonTaskExecutor)
    BaseExecutor.register(SkillFlowTaskExecutor)


_register_existing_executors()
