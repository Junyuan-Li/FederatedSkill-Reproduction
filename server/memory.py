"""
memory.py — 两级演化记忆存储（EvolutionMemoryStore）

对应论文 Section 4.2.1 中的两级记忆 M^t：

    '高层记忆（high-level memory）由 Family 共享，记录跨客户端全局观察，
     如哪些工作流仍未被任何客户端解决；
     低层记忆（low-level memory）对每个客户端私有，以 ρ_i 为键，
     跨轮累积模型特定的失败模式，如对某工具的频繁误用。'

设计：
  - HighLevelMemory: 字符串形式的共享知识库（Stage1 LLM 更新）
  - LowLevelMemory: 按 ρ_i（WorkerProfile.profile_hash，即 backbone+harness 的
    等价类）分桶的私有文本记忆（Stage2 LLM 更新）。同一 backbone+harness 组合的
    多个 worker（如 Setting2 的 3×GLM-5+Claude-Code）共享同一份记忆桶，
    对应论文"keyed by ρ_i"的字面含义——不是按 worker 身份分桶。
    公开接口（update_low_level/get_worker_memory*）仍按 worker_id 查询/更新，
    内部自动路由到该 worker 所属的 ρ_i 分桶，调用方无需感知这层间接。
  - 所有更新幂等（可重放）：按 round_idx 记录，支持回放

Paper Fidelity Audit（Phase15 P1-2）：
原实现按 worker_id 逐个分桶，Setting2 三个同构 worker 会各自累积独立记忆，
未体现论文"同 ρ_i 共享记忆"的语义，现已修复。
"""

from __future__ import annotations

from core.datatypes import HighLevelMemory, LowLevelMemory, WorkerProfile


class EvolutionMemoryStore:
    """
    两级演化记忆存储器。

    对应论文变量：M^t = (M_high^t, {M_low_i^t})

    注意：原版 memory 在 Stage1 规划完成后更新 high-level，
    在 Stage2 每个 client 演化完成后更新 low-level。
    本实现支持两种更新入口。
    """

    def __init__(
        self,
        family_name: str,
        worker_profiles: dict[str, WorkerProfile],
    ) -> None:
        self._family_name = family_name
        # 高层共享记忆
        self._high = HighLevelMemory(
            family_name=family_name,
            content="# 高层共享记忆\n\n暂无记录。\n",
            last_updated_round=-1,
        )
        # worker_id -> profile_hash（ρ_i 等价类键），供公开接口做路由
        self._worker_to_key: dict[str, str] = {
            wid: profile.profile_hash for wid, profile in worker_profiles.items()
        }
        # 低层私有记忆：{profile_hash: LowLevelMemory}——同 ρ_i 的 worker 共享一份
        self._low: dict[str, LowLevelMemory] = {}
        for wid, profile in worker_profiles.items():
            key = profile.profile_hash
            if key in self._low:
                # 已有同 ρ_i 的 worker 建过桶，记录共享关系，不覆盖已有内容
                existing = self._low[key]
                if wid not in existing.shared_worker_ids:
                    existing.shared_worker_ids.append(wid)
                continue
            self._low[key] = LowLevelMemory(
                worker_id=wid,
                profile_key=key,
                shared_worker_ids=[wid],
                backbone_model=profile.backbone_model,
                agent_harness=profile.agent_harness,
                content=f"# {profile.backbone_model}+{profile.agent_harness} 私有记忆\n\n暂无记录。\n",
                last_updated_round=-1,
            )

    # ------------------------------------------------------------------
    # 高层记忆
    # ------------------------------------------------------------------

    def update_high_level(self, new_content: str, round_idx: int) -> None:
        """
        由 Stage1 LLM 调用：用新内容替换高层记忆（完整替换，非追加）。
        论文：高层记忆记录「全局已观察到的失败/成功模式」。
        """
        self._high = HighLevelMemory(
            family_name=self._family_name,
            content=new_content,
            last_updated_round=round_idx,
        )

    @property
    def high_level(self) -> HighLevelMemory:
        return self._high

    # ------------------------------------------------------------------
    # 低层记忆
    # ------------------------------------------------------------------

    def update_low_level(
        self,
        worker_id: str,
        new_content: str,
        round_idx: int,
    ) -> None:
        """
        由 Stage2 LLM 调用（每 client 演化后）：更新该 worker 所属 ρ_i 分桶的
        私有记忆。若多个 worker 共享同一 ρ_i（如 Setting2），更新会对所有
        共享该分桶的 worker 同时可见——这正是论文"keyed by ρ_i"的语义。
        保留原有 backbone_model / agent_harness / shared_worker_ids（从旧记录继承）。
        """
        key = self._worker_to_key.get(worker_id, worker_id)
        old = self._low.get(key)
        self._low[key] = LowLevelMemory(
            worker_id=old.worker_id if old else worker_id,
            profile_key=key,
            shared_worker_ids=list(old.shared_worker_ids) if old else [worker_id],
            backbone_model=old.backbone_model if old else "",
            agent_harness=old.agent_harness if old else "",
            content=new_content,
            last_updated_round=round_idx,
        )

    def get_worker_memory(self, worker_id: str) -> LowLevelMemory | None:
        """获取 worker 所属 ρ_i 分桶的低层私有记忆（供 Stage2 提示词使用）。"""
        key = self._worker_to_key.get(worker_id, worker_id)
        return self._low.get(key)

    def get_worker_memory_text(self, worker_id: str) -> str:
        """返回文本内容，便于直接嵌入提示词。"""
        key = self._worker_to_key.get(worker_id, worker_id)
        mem = self._low.get(key)
        return mem.content if mem else "暂无记录。"

    # ------------------------------------------------------------------
    # 批量接口（Stage1 统一更新）
    # ------------------------------------------------------------------

    def apply_plan_memory_update(
        self,
        high_level_content: str,
        low_level_updates: dict[str, str],
        round_idx: int,
    ) -> None:
        """
        Stage1 完成后，一次性应用所有记忆更新。

        Args:
            high_level_content:  新的高层记忆文本
            low_level_updates:   {worker_id: 新的低层记忆文本}
            round_idx:           当前 round
        """
        self.update_high_level(high_level_content, round_idx)
        for wid, content in low_level_updates.items():
            self.update_low_level(wid, content, round_idx)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """返回可序列化的字典，用于持久化和提示词格式化。"""
        return {
            "high_level": {
                "family": self._family_name,
                "content": self._high.content,
                "last_updated_round": self._high.last_updated_round,
            },
            # 按 worker_id 展开（而不是按内部 profile_hash 分桶键）导出，
            # 保持对外 JSON 结构不变；共享同一 ρ_i 分桶的 worker 会导出相同内容。
            "low_level": {
                wid: {
                    "backbone_model": self._low[key].backbone_model,
                    "agent_harness": self._low[key].agent_harness,
                    "content": self._low[key].content,
                    "last_updated_round": self._low[key].last_updated_round,
                    "shared_worker_ids": list(self._low[key].shared_worker_ids),
                }
                for wid, key in self._worker_to_key.items()
                if key in self._low
            },
        }
