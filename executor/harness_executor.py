"""
harness_executor.py — HarnessAwareExecutor：Agent Harness 感知的执行器

对应用户 Part6 要求的分层：

    AgentHarness Interface
        |
        |-- CLIHarness（claude-code / qwen-code / kimi-cli，真实 subprocess）
        |
        |-- APIWorkspaceHarness（保留，委托既有 AgentWorkspaceExecutor）

本类是 executor 层的**薄路由层**（与 executor/router_executor.py::
VerificationAwareExecutor 完全同构的设计风格）：按 profile.agent_harness +
构造时传入的 mode，用 harness/factory.py::get_harness() 拿到具体 Harness
实例，再调用其统一的 `.run(task, library, profile, round_idx)`——本类不
知道、也不需要知道 CLI 的具体调用细节。


BaseExecutor 契约：run() 必须返回 core.datatypes.Trajectory，本类通过
harness.run() 满足（BaseAgentHarness.run() 保证同样的返回类型）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.datatypes import WorkerProfile
from executor.base import BaseExecutor
from harness.factory import HarnessMode, get_harness
from llm.router import BackboneRouter

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary
    from core.datatypes import Trajectory

logger = logging.getLogger(__name__)


class HarnessAwareExecutor(BaseExecutor):
    """
    按 profile.agent_harness 分派到真实 CLI Harness 或 APIWorkspaceHarness
    的薄路由层，对外接口与 VerificationAwareExecutor/AgentWorkspaceExecutor
    完全一致：run(task, library, profile, round_idx)。

    Args:
        router: BackboneRouter，转发给具体 Harness 构造函数
        mode:   "strict"（真实 CLI subprocess，Part6 默认的
                "strict reproduction mode"）或 "debug"（API 回退）。
                本类构造时默认 "debug"——**这是本类自身的安全默认值**，
                与"整个实验入口默认不使用本类"是两件不同的事：只要调用方
                显式实例化 HarnessAwareExecutor 却不传 mode，也不会意外
                触发需要真实 CLI 二进制的路径。
        top_k_skills: 转发给具体 Harness（技能检索 top_k）
    """

    def __init__(self, router: BackboneRouter, mode: HarnessMode = "debug", top_k_skills: int = 3) -> None:
        self._router = router
        self._mode = mode
        self._top_k_skills = top_k_skills
        # 每个 worker 的 agent_harness 可能不同（Setting3/4 异构），因此
        # harness 实例按 agent_harness 名称缓存，不是构造时一次性建好。
        self._harness_cache: dict[str, object] = {}
        # 只读调用轨迹，风格与 VerificationAwareExecutor.dispatch_log 一致。
        self.dispatch_log: list[dict[str, object]] = []

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> "Trajectory":
        harness = self._get_or_build_harness(profile.agent_harness)
        logger.info(
            "[HARNESS] task=%s worker=%s agent_harness=%s mode=%s round=%d",
            task.task_id, profile.client_id, profile.agent_harness, self._mode, round_idx,
        )
        self.dispatch_log.append({
            "task_id": task.task_id,
            "worker_id": profile.client_id,
            "agent_harness": profile.agent_harness,
            "mode": self._mode,
            "round_idx": round_idx,
        })
        return harness.run(task=task, library=library, profile=profile, round_idx=round_idx)

    def _get_or_build_harness(self, agent_harness: str):
        cache_key = agent_harness if self._mode == "strict" else f"__debug__:{agent_harness}"
        if cache_key not in self._harness_cache:
            self._harness_cache[cache_key] = get_harness(
                agent_harness, self._mode, router=self._router, top_k_skills=self._top_k_skills,
            )
        return self._harness_cache[cache_key]

    def get_dispatch_trace(self) -> list[dict[str, object]]:
        return list(self.dispatch_log)
