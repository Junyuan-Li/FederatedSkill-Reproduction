"""
benchmark/dependencies/ — 任务依赖图判定（纯函数，与采样策略/状态解耦）

对应 Official Implementation Alignment Audit 要求 A："Keep sampler separate
from algorithm"：依赖图判定被拆成独立的、无状态纯函数模块，不内联在任何
具体 Sampler 类里，也不属于 core/ 算法核心（δ蒸馏 / Stage1 / Stage2）——
它只是 benchmark 层描述"同一 family 内任务先后关系"的工具函数。
`benchmark/family_sampler.py::FamilyAwareSampler` 通过组合调用本模块，
而不是把依赖判定逻辑写在采样器类内部。

⚠️ Paper Fidelity 声明（与 docs/SIMPLIFICATIONS.md §2.4 一致）：
    "任务依赖图"这个概念本身仍然是 Phase12 自建、非论文/官方要求的实验性
    扩展——论文 Section 5.1 和官方 `skillfl/skillflow_adapter/partitioning.py`
    都没有这种机制。本模块只是把它从 FamilyAwareSampler 中拆分出来，方便
    独立测试/替换/审计，不代表这个概念本身获得了新的论文/官方依据。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from benchmark.family import TaskFamily
    from benchmark.task import Task

__all__ = ["is_unlocked", "eligible_tasks"]


def is_unlocked(task: "Task", solved_task_ids: set[str]) -> bool:
    """
    判断 task 的所有前置依赖（task.dependencies）是否都已出现在
    solved_task_ids 中。dependencies 为空时恒为 True（无前置要求）。
    """
    return all(dep in solved_task_ids for dep in task.dependencies)


def eligible_tasks(family: "TaskFamily", solved_task_ids: set[str]) -> list["Task"]:
    """
    返回 family 内"依赖已全部满足且尚未出现在 solved_task_ids 中"的任务列表，
    按 family.tasks 原始顺序返回（调用方可自行决定排序策略，如按难度取最小）。
    """
    return [
        task
        for task in family.tasks
        if task.task_id not in solved_task_ids and is_unlocked(task, solved_task_ids)
    ]
