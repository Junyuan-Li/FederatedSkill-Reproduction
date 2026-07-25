"""
api_workspace_harness.py — APIWorkspaceHarness：debug/fallback 模式

用户明确要求（改 CLI 时的补充说明）：
    "不要要求删除 AgentWorkspaceExecutor。正确方式应该是：
        AgentWorkspaceExecutor
                ↓
        APIWorkspaceHarness（保留）"

本类**不重新实现**任何执行逻辑，`run()` 整体委托给既有、已测试的
`executor/agent_executor.py::AgentWorkspaceExecutor.run()`——字节级复用同一
7 步流程（Environment 初始化 -> Skill Retrieval -> Prompt Build -> LLM
Generation -> Command Execution -> Verification -> Environment 清理），
不修改 AgentWorkspaceExecutor 源码一行。

BaseAgentHarness 的四个抽象方法（initialize/execute_task/collect_trajectory/
cleanup）仍需实现以满足接口契约（保证 isinstance 检查、factory.py 的统一
类型标注成立），但它们**不会被本类的 run() 调用**——因为
AgentWorkspaceExecutor.run() 内部已经把这四步串联好了，重新拆分反而是
无意义的重复实现。这与 BaseAgentHarness 文档字符串里"模板方法 run()"的
默认组合方式不同，是本类刻意的、有文档说明的特化。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.datatypes import Trajectory, WorkerProfile
from executor.environment import WorkspaceManager
from harness.base_harness import BaseAgentHarness, HarnessExecutionResult

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary


class APIWorkspaceHarness(BaseAgentHarness):
    """debug/fallback 模式：整体委托给既有 AgentWorkspaceExecutor（不新增逻辑）。"""

    harness_name = "api-workspace"

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> Trajectory:
        # 唯一真正被调用的方法：直接复用既有 AgentWorkspaceExecutor.run()。
        return self._agent_ws_executor.run(task, library, profile, round_idx)

    # ------------------------------------------------------------------
    # 下面四个方法仅为满足 BaseAgentHarness 接口契约而实现，均不会被
    # 上面的 run() 调用（见本模块文档字符串）。
    # ------------------------------------------------------------------

    def initialize(self, task: "Task", profile: WorkerProfile) -> WorkspaceManager:
        raise NotImplementedError(
            "APIWorkspaceHarness.run() 整体委托给 AgentWorkspaceExecutor，"
            "不会单独调用 initialize()。"
        )

    def execute_task(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        workspace: WorkspaceManager,
    ) -> HarnessExecutionResult:
        raise NotImplementedError(
            "APIWorkspaceHarness.run() 整体委托给 AgentWorkspaceExecutor，"
            "不会单独调用 execute_task()。"
        )

    def collect_trajectory(
        self,
        task: "Task",
        profile: WorkerProfile,
        round_idx: int,
        workspace: WorkspaceManager,
        exec_result: HarnessExecutionResult,
        reward: float,
        verifier_output: str,
        verifier_subtest_failures: list[str],
    ) -> Trajectory:
        raise NotImplementedError(
            "APIWorkspaceHarness.run() 整体委托给 AgentWorkspaceExecutor，"
            "不会单独调用 collect_trajectory()。"
        )

    def cleanup(self, workspace: WorkspaceManager) -> None:
        raise NotImplementedError(
            "APIWorkspaceHarness.run() 整体委托给 AgentWorkspaceExecutor，"
            "不会单独调用 cleanup()（AgentWorkspaceExecutor 内部用 with "
            "WorkspaceManager(...) 已经保证清理）。"
        )
