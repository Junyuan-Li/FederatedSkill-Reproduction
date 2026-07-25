"""
client/agent_runtime — Agentic 执行运行时

对应论文 Section 4.1.1:
    '每个 worker 运行一个 agentic harness，
     在技能库 L_i^t 和 profile ρ_i 的条件下执行任务 x，
     生成轨迹 τ_i ~ π_i(·|L_i^t, ρ_i)'

子模块：
  agent.py   — AgentRuntime：完整 Planner-Action-Observation 循环
  tools.py   — ToolRegistry + 内置工具（python_execute / skill_search / file_write）
  executor.py — TaskExecutor：任务级入口，协调 agent + verifier + trajectory 构建
"""

from client.agent_runtime.executor import TaskExecutor
from client.agent_runtime.agent import AgentRuntime
from client.agent_runtime.tools import ToolRegistry, BuiltinTools

__all__ = ["TaskExecutor", "AgentRuntime", "ToolRegistry", "BuiltinTools"]
