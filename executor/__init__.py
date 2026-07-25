"""
executor package — Sprint 1（Real TaskExecutor 升级）

对应用户 9-Phase 计划 Phase 2：把单一的 client/executor.py 升级为三种执行模式：

  mock_executor.py       — 不调用真实 LLM/subprocess，用于测试/CI 快速验证管线连通性
  python_executor.py     — 自建 function_test/python_test/output_match benchmark，
                            薰 client.executor.TaskExecutor（已实现，避免重复）
  skillflow_executor.py  — 真实 SkillFlow 任务（skillflow_script 验证类型），
                            subprocess 隔离 + 临时工作区，不使用 Harbor/Docker

不修改 core/ server/ client/（Phase 1/2 约束）；client/executor.py 保持原样，
本包通过组合（composition）方式复用其逻辑。
"""

from executor.mock_executor import MockExecutor
from executor.python_executor import PythonTaskExecutor
from executor.skillflow_executor import SkillFlowTaskExecutor, SkillFlowExecutor

# Phase12 新增：BaseExecutor 契约 + 真实 agent workspace 模式执行器及其组件
from executor.base import BaseExecutor
from executor.environment import WorkspaceManager
from executor.runner import CommandRunner, CommandResult
from executor.trajectory import TrajectoryCollector
from executor.agent_executor import AgentWorkspaceExecutor

# 最终论文一致性收口 Priority 1 新增：按 verification.type 分派
# TaskExecutor / SkillFlowTaskExecutor 的薄路由层（见 router_executor.py docstring）
from executor.router_executor import VerificationAwareExecutor

__all__ = [
    "MockExecutor", "PythonTaskExecutor", "SkillFlowTaskExecutor", "SkillFlowExecutor",
    "BaseExecutor", "WorkspaceManager", "CommandRunner", "CommandResult",
    "TrajectoryCollector", "AgentWorkspaceExecutor",
    "VerificationAwareExecutor",
]
