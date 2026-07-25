"""
python_executor.py — 自建 benchmark（function_test / python_test / output_match）的执行器

Agent 输出 -> sandbox 子进程执行 -> verifier -> reward，与论文
τ_i ~ π_i(·|L_i^t, ρ_i) 对应的完整流程已经在 client.executor.TaskExecutor 里实现
（Skill Retrieval -> Prompt Build -> LLM Generation -> Sandboxed Run -> Verification）。

本模块不重新实现该逻辑，只做一层薄别名/适配，满足 Sprint 1 计划里
executor/python_executor.py 的目录结构要求，同时保持单一实现来源。
"""

from __future__ import annotations

from client.executor import TaskExecutor as PythonTaskExecutor

__all__ = ["PythonTaskExecutor"]
