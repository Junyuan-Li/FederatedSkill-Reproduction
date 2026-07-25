"""
federated_client.py — 联邦客户端协调器（FederatedClient）

把三个客户端组件封装为统一接口：
  - SkillLibrary   (L_i^t  — 本地技能库)
  - PatchDistiller (g_i    — patch 蒸馏)
  - 库更新         (Apply  — 接收 MergedPatch 更新库)

对应论文中客户端侧的完整生命周期：
  execute_trial()   → τ   （外部 Agent Harness 执行，返回 Trajectory）
  distill_patch()   → δ_i^t（PatchDistiller 蒸馏）
  apply_update()    → L_i^{t+1}（Apply(L_i^t, Δ_i^t)）
  library_snapshot() → 供服务端 Stage2 使用的快照
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.datatypes import LibrarySnapshot, MergedPatch, Trajectory, WorkerPatch, WorkerProfile
from client.distiller import PatchDistiller
from client.library import SkillLibrary
from evaluation.cost_accounting import CostAccountant
from llm.backbone import LLMBackbone
from llm.router import BackboneRouter, make_single_worker_router

logger = logging.getLogger(__name__)


class FederatedClient:
    """
    联邦客户端，协调 SkillLibrary + PatchDistiller。

    不含任务执行逻辑（Agent Harness）——那属于外部系统（claude-code/qwen-code/kimi-cli）
    的职责，本复现用 SimulatedHarness 或真实 CLI 替换。

    Args:
        profile:       WorkerProfile ρ_i（不可变）
        library_root:  技能库根目录路径
        router:        可选的共享 BackboneRouter。传入时 distiller 与外部
                       TaskExecutor 使用同一个 backbone 实例，对应论文
                       "patcher shares the same backbone LLM as the execution LLM"；
                       也便于单测/冒烟测试注入 mock backbone。
                       None → 内部自建单 worker 路由（生产默认路径）。
    """

    def __init__(
        self,
        profile: WorkerProfile,
        library_root: Path,
        router: BackboneRouter | None = None,
    ) -> None:
        self._profile = profile
        self._library = SkillLibrary(root=library_root, worker_id=profile.client_id)

        # 未注入 router 时，为该 worker 自建单 worker 路由
        if router is None:
            router = make_single_worker_router(profile.client_id, profile)
        self._distiller = PatchDistiller(router=router)

        logger.info(
            "FederatedClient 初始化: worker=%s model=%s harness=%s library=%s",
            profile.client_id, profile.backbone_model,
            profile.agent_harness, library_root,
        )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def distill_patch(self, trajectory: Trajectory) -> WorkerPatch:
        """
        论文 Section 4.1.2: δ_i^t = g_i(L_i^t, B_i^t, ρ_i)

        Args:
            trajectory: 原始执行轨迹 B_i^t（本地保留，不上传）

        Returns:
            WorkerPatch δ_i^t，上传给服务器
        """
        return self._distiller.distill(
            trajectory=trajectory,
            library=self._library,
            profile=self._profile,
        )

    def apply_update(self, merged_patch: MergedPatch) -> None:
        """
        论文 Section 4.2.2: L_i^{t+1} = Apply(L_i^t, Δ_i^t)

        Args:
            merged_patch: 服务器下发的 MergedPatch Δ_i^t
        """
        self._library.apply_patch(merged_patch)
        logger.info(
            "库更新完成: worker=%s upserts=%d deletions=%d",
            self._profile.client_id,
            len(merged_patch.upserts),
            len(merged_patch.deletions),
        )

    def set_cost_recorder(self, cost_recorder: CostAccountant | None) -> None:
        """
        设置/替换成本审计器，转发到内部 PatchDistiller（Appendix C 成本复现
        审计新增，TASK4）。None（默认）时零行为变化。
        """
        self._distiller.set_cost_recorder(cost_recorder)

    def library_snapshot(self, round_idx: int = 0) -> LibrarySnapshot:
        """
        返回当前库快照，供服务端 Stage2 使用。
        """
        return self._library.snapshot(round_idx=round_idx)

    def validate_library(self) -> list[str]:
        """返回库结构问题列表（供调试使用）。"""
        return self._library.validate()

    @property
    def profile(self) -> WorkerProfile:
        return self._profile

    @property
    def library(self) -> SkillLibrary:
        return self._library

    @property
    def worker_id(self) -> str:
        return self._profile.client_id
