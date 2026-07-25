"""
evolution.py — 联邦服务器总调度（FederatedServer）

对应论文 Algorithm 1 中服务器端的完整 round 逻辑：

    接收所有 worker 的 WorkerPatch δ_i^t
      ↓
    Stage 1：EvolutionPlanner → EvolutionPlan P^t
      ↓
    Stage 2：EvolutionExecutor (per worker) → MergedPatch Δ_i^t
      ↓
    返回 {worker_id: MergedPatch}，客户端执行 Apply(L_i^t, Δ_i^t)

设计约束：
  - 服务器只处理 patch（δ_i^t），不处理原始轨迹（B_i^t）
  - Stage2 独立 per-client（顺序执行；可并行，但本版保守顺序执行）
  - 完整 round 记录写入 RoundRecord 便于后续指标计算
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.datatypes import (
    DecisionLog,
    Directive,
    EvolutionPlan,
    LibraryDigest,
    LibraryFileEntry,
    LibrarySnapshot,
    MergedPatch,
    RoundRecord,
    WorkerPatch,
    WorkerProfile,
)
from llm.backbone import LLMBackbone
from server.capability import CapabilityTracker
from server.memory import EvolutionMemoryStore
from server.merge import EvolutionExecutor
from server.planner import EvolutionPlanner
from server.prompt_builder import Stage1PromptBuilder, Stage2PromptBuilder

logger = logging.getLogger(__name__)


class FederatedServer:
    """
    联邦服务器，协调 Stage1（规划）和 Stage2（per-client 演化）。

    典型使用方式：
        server = FederatedServer.create(
            server_backbone=LLMBackbone.from_worker_profile(server_profile),
            family_name="Production-Capacity-Planning",
            worker_profiles={"u0": p0, "u1": p1, "u2": p2},
        )
        merged_patches = server.run_round(
            round_idx=0,
            patches=worker_patches,
            library_snapshots={"u0": snap0, "u1": snap1, "u2": snap2},
        )
    """

    def __init__(
        self,
        planner: EvolutionPlanner,
        executor: EvolutionExecutor,
        capability_tracker: CapabilityTracker,
        memory_store: EvolutionMemoryStore,
        family_name: str,
        worker_profiles: dict[str, WorkerProfile],
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._capability = capability_tracker
        self._memory = memory_store
        self._family_name = family_name
        self._worker_profiles = worker_profiles
        self._round_records: list[RoundRecord] = []

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        server_backbone: LLMBackbone,
        family_name: str,
        worker_profiles: dict[str, WorkerProfile],
    ) -> "FederatedServer":
        """
        便捷工厂：一行创建完整的 FederatedServer。
        """
        capability = CapabilityTracker(
            family_name=family_name,
            worker_ids=list(worker_profiles.keys()),
        )
        memory = EvolutionMemoryStore(
            family_name=family_name,
            worker_profiles=worker_profiles,
        )
        planner = EvolutionPlanner(
            server_backbone=server_backbone,
            prompt_builder=Stage1PromptBuilder(),
        )
        executor = EvolutionExecutor(
            server_backbone=server_backbone,
            prompt_builder=Stage2PromptBuilder(),
        )
        return cls(
            planner=planner,
            executor=executor,
            capability_tracker=capability,
            memory_store=memory,
            family_name=family_name,
            worker_profiles=worker_profiles,
        )

    # ------------------------------------------------------------------
    # 核心：run_round
    # ------------------------------------------------------------------

    def run_round(
        self,
        round_idx: int,
        patches: dict[str, WorkerPatch],
        library_snapshots: dict[str, LibrarySnapshot],
        task_assignments: dict[str, str] | None = None,
        disable_capability_matrix: bool = False,
    ) -> dict[str, MergedPatch]:
        """
        执行一个完整的联邦演化 round。

        Args:
            round_idx:                  当前 round 序号（从 0 开始）
            patches:                    {worker_id: WorkerPatch}，本轮所有 worker 的 patch
            library_snapshots:          {worker_id: LibrarySnapshot}，每个 worker 的库快照
            task_assignments:           {worker_id: task_name}，可选，用于记录日志
            disable_capability_matrix:  A1 ablation：禁用能力矩阵，Stage1 不更新覆盖状态
                                        对应 ablation_a1_no_capability_matrix.yaml

        Returns:
            {worker_id: MergedPatch}，客户端调用 library.apply_patch(delta) 应用更新
        """
        t_start = time.time()
        logger.info(
            "Round %d 开始: family=%s workers=%d patches=%d",
            round_idx, self._family_name, len(self._worker_profiles), len(patches),
        )

        # -- 构建 library_digests（Stage1 只看摘要）
        library_digests = self._build_library_digests(library_snapshots)

        # ================================================================
        # Stage 1: Evolution Planning
        # ================================================================
        logger.info("Stage1 开始...")

        # 初始化能力矩阵；A1 ablation：随后立即清空（Stage1 看不到覆盖历史）
        self._capability.init_from_patches(patches, round_idx)
        if disable_capability_matrix:
            self._capability.clear()
            logger.info("[Ablation A1] capability_matrix 已清空（disable_capability_matrix=True）")

        evolution_plan: EvolutionPlan = self._planner.plan(
            round_idx=round_idx,
            family_name=self._family_name,
            patches=patches,
            library_digests=library_digests,
            capability_tracker=self._capability,
            memory_store=self._memory,
            worker_profiles=self._worker_profiles,
        )
        logger.info(
            "Stage1 完成: directives=%d", len(evolution_plan.directives)
        )

        # ================================================================
        # Stage 2: Per-Client Personalized Evolution
        # ================================================================
        logger.info("Stage2 开始（per-client，顺序执行）...")
        merged_patches: dict[str, MergedPatch] = {}
        decision_logs: list[DecisionLog] = []

        for worker_id, profile in sorted(self._worker_profiles.items()):
            # Algorithm Fidelity Fix — Multi-Directive Execution：
            # 论文 Section 4 中 Stage1 为一个 worker 下发的指令集合是
            # D_i^t = {d1, d2, ..., dk}（复数，见 Directive/EvolutionPlan
            # 的 docstring），Stage2 "processes these directives"同样是复数
            # 语义。此前这里只取 get_directives_for() 排序后的第一条
            # （worker_directives[0]），把 D_i^t 错误退化为单个 d1，同一
            # worker 本轮其余优先级更低的 directive 被静默丢弃、且不会带
            # 到下一轮（下一轮 Stage1 会重新规划，不是"延后执行"）。
            # 修复：遍历该 worker 本轮全部 directives，逐条真实经过
            # Stage2 Evolution Agent（每条都独立调用 LLM、独立产生
            # DecisionLog/merge action/audit trace，不允许绕过/直接复制/
            # if-else 规则合并），再把各条的结果按执行顺序合并成一份最终
            # 回传给 client 的 MergedPatch（client.apply_update() 每个
            # worker 每轮只应用一次，接口契约不变）。
            worker_directives = evolution_plan.get_directives_for(worker_id)

            # 同伴 patches（排除自身）
            peer_patches: dict[str, WorkerPatch] = {
                wid: p for wid, p in patches.items() if wid != worker_id
            }
            peer_profiles = {wid: p for wid, p in self._worker_profiles.items()
                             if wid != worker_id}

            # 当前库快照
            current_snapshot = library_snapshots.get(
                worker_id,
                LibrarySnapshot(worker_id=worker_id, round_idx=round_idx, files=[]),
            )

            # 官方 merge_skill/SKILL.md Inputs 清单中的 peer_libraries/<peer>/
            # ——每个同伴 worker 完整技能库的 name+description 级摘要（复用
            # Stage1 已计算的 library_digests，避免重复解析 SKILL.md）。
            # 此前 Stage2 只能看到「本轮同伴 patch」，看不到同伴库里本轮
            # 未被触碰的既有技能，导致「跨 worker 命名对齐」「伞形结构共识」
            # 两条规则缺输入数据、无法真正执行——这里补上。
            peer_library_digests = {
                wid: d for wid, d in library_digests.items() if wid != worker_id
            }

            merged, worker_logs = self._execute_worker_directives(
                worker_id=worker_id,
                profile=profile,
                worker_directives=worker_directives,
                current_snapshot=current_snapshot,
                peer_patches=peer_patches,
                peer_profiles=peer_profiles,
                peer_library_digests=peer_library_digests,
                round_idx=round_idx,
                task_assignments=task_assignments,
                capability_tracker=self._capability,
            )
            merged_patches[worker_id] = merged
            decision_logs.extend(worker_logs)

        # -- 记录 round 信息
        elapsed = time.time() - t_start
        rewards = {wid: p.reward for wid, p in patches.items()}
        _assignments = [(wid, (task_assignments or {}).get(wid, "")) for wid in patches]
        record = RoundRecord(
            round_idx=round_idx,
            family_name=self._family_name,
            assignments=_assignments,
            worker_patches=patches,
            evolution_plan=evolution_plan,
            merged_patches=merged_patches,
            decision_logs=decision_logs,
            rewards=rewards,
            elapsed_seconds=elapsed,
        )
        self._round_records.append(record)

        logger.info(
            "Round %d 完成: mean_reward=%.3f elapsed=%.1fs",
            round_idx,
            record.mean_reward or 0.0,
            elapsed,
        )
        return merged_patches

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _execute_worker_directives(
        self,
        worker_id: str,
        profile: WorkerProfile,
        worker_directives: list[Directive],
        current_snapshot: LibrarySnapshot,
        peer_patches: dict[str, WorkerPatch],
        peer_profiles: dict[str, WorkerProfile],
        round_idx: int,
        task_assignments: dict[str, str] | None,
        peer_library_digests: dict[str, list[LibraryDigest]] | None = None,
        capability_tracker: CapabilityTracker | None = None,
    ) -> tuple[MergedPatch, list[DecisionLog]]:
        """
        对单个 worker 本轮 Stage1 下发的**全部** directives（D_i^t = {d1,...,dk}，
        已按 priority 降序排好序，见 EvolutionPlan.get_directives_for()）逐条
        执行 Stage2 Evolution Agent，而不是只执行优先级最高的一条。

        每条 directive：
          - 独立调用一次 `EvolutionExecutor.execute_for_worker()`（真实 LLM
            调用，不允许绕过、不允许直接复制 patch、不允许 if/else 规则合并）；
          - 独立产生一条 DecisionLog（写入 directive_id 区分同轮同 worker 的
            多条决策）；
          - 独立产生一次 audit trace / cost ledger 记录（由
            EvolutionExecutor 内部已有的旁路记录器完成，见 server/merge.py）。

        为了让后一条 directive 看到前一条 directive 刚做出的改动（避免两条
        directive 各自基于同一份"过期"快照重复决策同一个文件），每执行完
        一条 directive 就把它产出的 MergedPatch 应用到一份内存中的工作快照
        （`_apply_patch_to_snapshot`，纯内存操作，不触碰磁盘/不调用
        `client/library.py::SkillLibrary`），供下一条 directive 的 Stage2
        prompt 使用。

        最终把所有 directive 的合并结果按执行顺序叠加成【一份】MergedPatch
        返回（先删后写，与 client/library.py::apply_patch() 的落盘顺序语义
        一致），因为 `run_round()` 的对外契约（`dict[str, MergedPatch]`，
        每个 worker 每轮由 client 侧应用一次）保持不变——只恢复"该 worker
        本轮全部 directive 都必须真实执行"这一层 cardinality，不改变
        Capability Matrix / Two-level Memory / SkillPatch schema / WorkerPatch
        schema / Stage1 Planner / Prompt architecture / Merge action space。

        无指令时（worker_directives 为空列表）保持原有行为：仍调用一次
        execute_for_worker(directive=None) 走空 patch 早退路径。
        """
        task_id = (task_assignments or {}).get(worker_id)
        peer_library_digests = peer_library_digests or {}

        if not worker_directives:
            merged, log = self._executor.execute_for_worker(
                target_worker_id=worker_id,
                target_profile=profile,
                directive=None,
                current_snapshot=current_snapshot,
                peer_patches=peer_patches,
                peer_profiles=peer_profiles,
                peer_library_digests=peer_library_digests,
                capability_tracker=capability_tracker,
                memory_store=self._memory,
                round_idx=round_idx,
                family_id=self._family_name,
                task_id=task_id,
                directive_id=None,
            )
            return merged, [log]

        working_snapshot = current_snapshot
        combined_upserts: dict[str, str] = {}
        combined_deletions: list[str] = []
        combined_cost_usd = 0.0
        combined_summaries: list[str] = []
        decision_logs: list[DecisionLog] = []

        for idx, directive in enumerate(worker_directives):
            directive_id = f"round_{round_idx}_worker_{worker_id}_directive_{idx}"

            merged, log = self._executor.execute_for_worker(
                target_worker_id=worker_id,
                target_profile=profile,
                directive=directive,
                current_snapshot=working_snapshot,
                peer_patches=peer_patches,
                peer_profiles=peer_profiles,
                peer_library_digests=peer_library_digests,
                capability_tracker=capability_tracker,
                memory_store=self._memory,
                round_idx=round_idx,
                # Result Reconstruction Audit（Appendix A）+ Algorithm
                # Fidelity Fix（Multi-Directive Execution）：family_id/
                # task_id/directive_id 均为纯审计字段，不参与任何合并决策。
                family_id=self._family_name,
                task_id=task_id,
                directive_id=directive_id,
            )
            decision_logs.append(log)

            # 按"先删后写"的顺序叠加进最终返回给该 worker 的合并 patch
            for path in merged.deletions:
                combined_upserts.pop(path, None)
                if path not in combined_deletions:
                    combined_deletions.append(path)
            for path, content in merged.upserts.items():
                if path in combined_deletions:
                    combined_deletions.remove(path)
                combined_upserts[path] = content
            combined_cost_usd += merged.cost_usd
            if merged.summary:
                combined_summaries.append(f"[directive {idx}] {merged.summary}")

            # 让下一条 directive 在"已应用前面几条 directive"之后的库状态上
            # 继续决策（纯内存快照，不落盘）
            working_snapshot = self._apply_patch_to_snapshot(working_snapshot, merged, round_idx)

        final_merged = MergedPatch(
            worker_id=worker_id,
            round_idx=round_idx,
            upserts=combined_upserts,
            deletions=combined_deletions,
            summary="; ".join(combined_summaries),
            cost_usd=combined_cost_usd,
        )
        return final_merged, decision_logs

    @staticmethod
    def _apply_patch_to_snapshot(
        snapshot: LibrarySnapshot, patch: MergedPatch, round_idx: int,
    ) -> LibrarySnapshot:
        """
        纯内存版 `client/library.py::SkillLibrary.apply_patch()`：把一份
        MergedPatch（先删后写）应用到一份 LibrarySnapshot 上，返回新的
        LibrarySnapshot，不写入任何磁盘文件。仅供同一 round 内后续 directive
        的 Stage2 决策使用"更新后的库视图"，与真实客户端落盘（Phase3 Apply
        Phase，由 `experiments/federated.py` 调用）是两回事，互不影响。
        """
        files: dict[str, str] = snapshot.to_path_content_dict()
        for path in patch.deletions:
            files.pop(path, None)
        for path, content in patch.upserts.items():
            files[path] = content
        return LibrarySnapshot(
            worker_id=snapshot.worker_id,
            round_idx=round_idx,
            files=[LibraryFileEntry(path=p, content=c) for p, c in files.items()],
        )

    def _build_library_digests(
        self, library_snapshots: dict[str, LibrarySnapshot]
    ) -> dict[str, list[LibraryDigest]]:
        """
        从每个 worker 的完整库快照提取描述级摘要（Stage1 信息最小化）。
        解析 SKILL.md 的 YAML 前置元数据，只保留 name + description。
        """
        import re
        import yaml

        FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
        digests: dict[str, list[LibraryDigest]] = {}

        for worker_id, snapshot in library_snapshots.items():
            worker_digests: list[LibraryDigest] = []
            skill_mds = snapshot.filter_skill_mds()
            for entry in skill_mds:
                m = FM_RE.match(entry.content)
                if not m:
                    continue
                try:
                    fm = yaml.safe_load(m.group(1)) or {}
                except yaml.YAMLError:
                    continue
                if fm.get("name"):
                    dir_name = entry.path.split("/")[0] if "/" in entry.path else ""
                    worker_digests.append(LibraryDigest(
                        skill_name=str(fm["name"]),
                        description=str(fm.get("description", "")),
                        directory=dir_name,
                        tags=list(fm.get("tags", [])) if isinstance(fm.get("tags"), list) else [],
                    ))
            digests[worker_id] = worker_digests

        return digests

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def round_records(self) -> list[RoundRecord]:
        return list(self._round_records)

    @property
    def current_capability(self) -> CapabilityTracker:
        return self._capability

    @property
    def memory_store(self) -> EvolutionMemoryStore:
        return self._memory

    @property
    def family_name(self) -> str:
        """Experiment Integrity Hardening TASK4：供 runner 层组装
        experiment_execution_trace.jsonl 的 family 字段时使用。"""
        return self._family_name

    def set_decision_logger(self, decision_logger) -> None:
        """
        把审计日志器转发给 Stage2 执行器（`server/merge.py::EvolutionExecutor`）。

        由 `experiments/federated.py::FederatedRunner` 在构造好
        `DecisionLogger`（或 output_dir 未提供时为 None）之后调用，使
        Stage2 每次 PaperMergeAction 完成后能在 memory 提交之前写入
        DECISIONS.md（论文 Section 4.2.2）。不改变 FederatedServer.create()
        的既有签名/调用点。
        """
        self._executor.set_decision_logger(decision_logger)

    def set_audit_trace_recorder(self, audit_trace_recorder) -> None:
        """
        把 Appendix A 审计追踪器转发给 Stage2 执行器（`server/merge.py::
        EvolutionExecutor`），转发方式与上面的 `set_decision_logger()` 完全
        一致（TASK3：Result Reconstruction Audit 新增，纯审计旁路，不改变
        FederatedServer.create() 的既有签名/调用点，不影响任何合并决策）。

        由 `experiments/federated.py::FederatedRunner` 在构造好
        `evaluation/audit_trace.py::AuditTraceRecorder`（或 output_dir 未
        提供时为 None）之后调用。
        """
        self._executor.set_audit_trace_recorder(audit_trace_recorder)

    def set_cost_recorder(self, cost_recorder) -> None:
        """
        把成本核算器同时转发给 Stage1 规划器（`server/planner.py::
        EvolutionPlanner`）与 Stage2 执行器（`server/merge.py::
        EvolutionExecutor`）——这是与 Stage2-only 的 `set_decision_logger()`/
        `set_audit_trace_recorder()` 唯一的区别：成本核算需要同时覆盖
        Stage1 和 Stage2 两次服务器 LLM 调用（Appendix C 成本复现审计，
        TASK4：此前这两处的 `call_result.cost_usd` 都在计算完之后被直接
        丢弃，从未计入任何论文成本曲线）。不改变 FederatedServer.create()
        的既有签名/调用点，不影响任何规划/合并决策。

        由 `experiments/federated.py::FederatedRunner` 在构造好
        `evaluation/cost_accounting.py::CostAccountant`（或 output_dir 未
        提供时为 None）之后调用。
        """
        self._planner.set_cost_recorder(cost_recorder)
        self._executor.set_cost_recorder(cost_recorder)

    def set_trace_recorder(self, trace_recorder) -> None:
        """
        把执行轨迹记录器同时转发给 Stage1 规划器与 Stage2 执行器（Experiment
        Integrity Hardening TASK4：experiment_execution_trace.jsonl）。与
        set_cost_recorder() 完全对等的转发方式，不改变 FederatedServer.create()
        的既有签名/调用点，不影响任何规划/合并决策。

        由 `experiments/federated.py::FederatedRunner` 在构造好
        `evaluation/integrity_logs.py::ExecutionTraceRecorder`（或 output_dir
        未提供时为 None）之后调用。
        """
        self._planner.set_trace_recorder(trace_recorder)
        self._executor.set_trace_recorder(trace_recorder)

    def set_fusion_trace_recorder(self, fusion_trace_recorder) -> None:
        """
        把 Skill Fusion 追踪器转发给 Stage2 执行器（Full Reproduction
        Alignment Audit TASK4）。与 set_audit_trace_recorder() 完全对等的
        转发方式，不改变 FederatedServer.create() 的既有签名/调用点。
        """
        self._executor.set_fusion_trace_recorder(fusion_trace_recorder)

    def set_memory_trace_recorder(self, memory_trace_recorder) -> None:
        """
        把两级记忆访问追踪器同时转发给 Stage1 规划器（记录 high-level 读写）
        与 Stage2 执行器（记录 low-level 读写），对应论文 Section 4.2.1 的
        完整"读 -> 用 -> 更新"闭环（Full Reproduction Alignment Audit
        TASK5）。转发方式与 set_cost_recorder() 完全对等。
        """
        self._planner.set_memory_trace_recorder(memory_trace_recorder)
        self._executor.set_memory_trace_recorder(memory_trace_recorder)

    def set_transfer_trace_recorder(self, transfer_trace_recorder) -> None:
        """
        把跨客户端迁移追踪器转发给 Stage2 执行器（Full Reproduction
        Alignment Audit TASK6）。与 set_audit_trace_recorder() 完全对等的
        转发方式，不改变 FederatedServer.create() 的既有签名/调用点。
        """
        self._executor.set_transfer_trace_recorder(transfer_trace_recorder)

    def summary(self) -> dict[str, Any]:
        """返回完整运行摘要，供 Evaluator 计算指标使用。"""
        return {
            "family_name": self._family_name,
            "total_rounds": len(self._round_records),
            "workers": list(self._worker_profiles.keys()),
            "per_round_mean_rewards": [
                r.mean_reward for r in self._round_records
            ],
            "final_capability_matrix": self._capability.to_dict(),
        }
