"""
merge.py — Stage 2 个性化演化执行器（EvolutionExecutor）

对应论文 Section 4.2.2: Stage 2 — Per-Client Personalized Evolution。

论文公式：
    L_i^{t+1} = Apply(L_i^t, Δ_i^t)

关键设计约束：
  - Stage2 **对每个 client 独立运行**（non-all-reduce）
  - 目标 worker 接收 peer patches 供参考，但最终 patch 必须适配自己的 ρ_i
    （"peer patches 供参考"这一输入结构对应保留的官方实验性 Prompt
     [prompts/stage2_prompt.txt] 中描述的 peer_libraries 只读快照机制，
     并非从官方 .py 源码复制）
  - 每次演化生成 DecisionLog（可审计：来源 patch + reward + 理由，
    对应论文 Section 4.2.2 'auditable decision log' 原文）
  - Stage2 更新 Low-Level Memory（不同于 Stage1 更新 High-Level Memory）

本类只负责三件事：拼装/调用 Stage2PromptBuilder 生成的提示词、调用服务器
backbone LLM、解析 LLM 返回的 JSON 为 (MergedPatch, DecisionLog)。不包含
从官方 server/merge.py 或 skillfl/skillflow_adapter/merge.py 复制的算法代码。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.datatypes import (
    DecisionLog,
    Directive,
    LibraryDigest,
    LibrarySnapshot,
    MergedPatch,
    SkipUpdate,
    WorkerPatch,
    WorkerProfile,
    validate_safe_rel_path,
)
from core.exceptions import LLMCallError
from llm.backbone import LLMBackbone
from evaluation.audit_trace import AuditTraceRecorder
from evaluation.cost_accounting import CostAccountant
from evaluation.fusion_trace import FusionTraceRecorder
from evaluation.integrity_logs import ExecutionTraceRecorder
from evaluation.memory_trace import MemoryTraceRecorder
from evaluation.transfer_trace import TransferTraceRecorder
from server.capability import CapabilityTracker
from server.logging import DecisionLogger
from server.memory import EvolutionMemoryStore
from server.prompt_builder import Stage2PromptBuilder

logger = logging.getLogger(__name__)


class EvolutionExecutor:
    """
    Stage 2 执行器，为每个 worker 生成个性化的 MergedPatch Δ_i^t。

    与 Stage1 不同：每次调用只处理一个目标 worker，而非全体 worker。
    Args:
        server_backbone: 服务器端 LLM backbone
        prompt_builder:  Stage2PromptBuilder；None → 使用默认实例
        decision_logger: 可选，`server/logging.py::DecisionLogger`。提供时，
            每次 PaperMergeAction 完成后会在 memory 提交*之前*调用
            `decision_logger.log_decision(log)`（对应论文 Section 4.2.2
            "commit observations to low-level memory" 之前的可审计决策日志
            要求）。为 None（默认）时跳过审计日志，行为与之前完全一致。
            也可以之后用 `set_decision_logger()` 补设。
        audit_trace_recorder: 可选，`evaluation/audit_trace.py::AuditTraceRecorder`
            （Result Reconstruction Audit — Appendix A 复现能力新增）。与
            decision_logger 是完全对等的旁路审计消费者：同样在 memory 提交
            *之前*、且紧跟 decision_logger 之后调用一次 `.record(log, ...)`，
            用于重建论文 Appendix A 的跨轮次案例分析（谁的 patch 被采纳、
            改了哪个文件、改动前后内容哈希/diff）。为 None（默认）时跳过，
            不影响未传该参数的旧调用方/已有测试。也可以之后用
            `set_audit_trace_recorder()` 补设。
        cost_recorder: 可选，`evaluation/cost_accounting.py::CostAccountant`
            （Appendix C 成本复现审计新增，TASK4）。与 decision_logger/
            audit_trace_recorder 完全对等的旁路消费者：LLM 调用成功后立即记
            一条 `component="stage2_merge"` 的 LLMCallCostRecord（此前
            `call_result.cost_usd` 虽被写入 `MergedPatch.cost_usd`，但从未
            被任何下游汇总逻辑读取）。为 None（默认）时跳过，不影响任何返
            回值。也可以之后用 `set_cost_recorder()` 补设。
    """

    def __init__(
        self,
        server_backbone: LLMBackbone,
        prompt_builder: Stage2PromptBuilder | None = None,
        decision_logger: DecisionLogger | None = None,
        audit_trace_recorder: AuditTraceRecorder | None = None,
        cost_recorder: CostAccountant | None = None,
        trace_recorder: ExecutionTraceRecorder | None = None,
        fusion_trace_recorder: FusionTraceRecorder | None = None,
        memory_trace_recorder: MemoryTraceRecorder | None = None,
        transfer_trace_recorder: TransferTraceRecorder | None = None,
    ) -> None:
        self._backbone = server_backbone
        self._prompt_builder = prompt_builder or Stage2PromptBuilder()
        self._decision_logger = decision_logger
        self._audit_trace_recorder = audit_trace_recorder
        self._cost_recorder = cost_recorder
        # Experiment Integrity Hardening TASK4：experiment_execution_trace.jsonl
        # 的旁路记录器，None（默认）时零行为变化。
        self._trace_recorder = trace_recorder
        # Full Reproduction Alignment Audit TASK4/5/6：三个新增的旁路审计
        # 记录器，均为 None（默认）时零行为变化，与上面几个记录器完全对等。
        self._fusion_trace_recorder = fusion_trace_recorder
        self._memory_trace_recorder = memory_trace_recorder
        self._transfer_trace_recorder = transfer_trace_recorder

    def set_decision_logger(self, decision_logger: DecisionLogger | None) -> None:
        """设置/替换审计日志器（FederatedServer.set_decision_logger 转发到此）。"""
        self._decision_logger = decision_logger

    def set_audit_trace_recorder(self, audit_trace_recorder: AuditTraceRecorder | None) -> None:
        """设置/替换 Appendix A 审计追踪器（FederatedServer.set_audit_trace_recorder 转发到此）。"""
        self._audit_trace_recorder = audit_trace_recorder

    def set_cost_recorder(self, cost_recorder: CostAccountant | None) -> None:
        """设置/替换成本审计器（FederatedServer.set_cost_recorder 转发到此）。"""
        self._cost_recorder = cost_recorder

    def set_trace_recorder(self, trace_recorder: ExecutionTraceRecorder | None) -> None:
        """设置/替换执行轨迹记录器（Experiment Integrity Hardening TASK4）。"""
        self._trace_recorder = trace_recorder

    def set_fusion_trace_recorder(self, fusion_trace_recorder: FusionTraceRecorder | None) -> None:
        """设置/替换 Skill Fusion 追踪器（TASK4：FederatedServer.set_fusion_trace_recorder 转发到此）。"""
        self._fusion_trace_recorder = fusion_trace_recorder

    def set_memory_trace_recorder(self, memory_trace_recorder: MemoryTraceRecorder | None) -> None:
        """设置/替换两级记忆访问追踪器（TASK5：FederatedServer.set_memory_trace_recorder 转发到此）。"""
        self._memory_trace_recorder = memory_trace_recorder

    def set_transfer_trace_recorder(self, transfer_trace_recorder: TransferTraceRecorder | None) -> None:
        """设置/替换跨客户端迁移追踪器（TASK6：FederatedServer.set_transfer_trace_recorder 转发到此）。"""
        self._transfer_trace_recorder = transfer_trace_recorder

    def execute_for_worker(
        self,
        target_worker_id: str,
        target_profile: WorkerProfile,
        directive: Directive | None,
        current_snapshot: LibrarySnapshot,
        peer_patches: dict[str, WorkerPatch],
        peer_profiles: dict[str, WorkerProfile],
        memory_store: EvolutionMemoryStore,
        round_idx: int,
        family_id: str | None = None,
        task_id: str | None = None,
        directive_id: str | None = None,
        peer_library_digests: dict[str, list[LibraryDigest]] | None = None,
        capability_tracker: CapabilityTracker | None = None,
    ) -> tuple[MergedPatch, DecisionLog]:
        """
        为单个 worker 执行个性化演化。

        Args:
            target_worker_id:  目标 worker ID
            target_profile:    目标 worker 的 ρ_i
            directive:         Stage1 下发的指令（None → 直接返回空 patch，维持现状）
            current_snapshot:  目标 worker 当前技能库快照
            peer_patches:      {peer_worker_id: WorkerPatch}（供 absorb/refactor 参考）
            peer_profiles:     同伴的 profile {worker_id: WorkerProfile}
            memory_store:      演化记忆存储器（用于读取和更新低层记忆）
            round_idx:         当前 round 序号
            family_id:         [EXTENSION，Result Reconstruction Audit 新增] 当前
                family（仅写入 DecisionLog.family_id 做审计，不参与任何决策）
            task_id:           [EXTENSION，同上] 本轮该 worker 执行的任务 ID
                （仅写入 DecisionLog.task_id 做审计，不参与任何决策）
            directive_id:      [EXTENSION，Algorithm Fidelity Fix 新增] 本次调用对应
                的 directive 在该 worker 本轮全部 directives 中的稳定标识，例如
                'round_2_worker_u0_directive_0'（仅写入 DecisionLog.directive_id/
                trace 做审计，不参与任何决策，None 时保持旧行为）
            peer_library_digests: [EXTENSION，官方 merge_skill/SKILL.md Inputs
                对齐新增] {peer_worker_id: [LibraryDigest]}——每个同伴完整技能库
                的 name+description 级摘要（不含正文），供"跨 worker 命名对齐"
                "伞形结构共识"两条规则使用。区别于 peer_patches（只反映本轮
                增量提案）：这里反映同伴库的**当前整体结构**，即便同伴本轮没有
                触碰某个技能，其名字仍会出现在这里。为 None（默认）时保持旧
                行为（空字典，等价于不提供该输入）。
            capability_tracker: [EXTENSION，官方 merge_skill/SKILL.md Inputs
                对齐新增] 官方要求的 `task_memory.md`（Stage1 产出的全体
                worker×workflow 覆盖矩阵）。此前 Stage2 只拿到 Stage1 已经
                降维成单条 directive 的结论，看不到矩阵全貌，无法判断
                "这个 gap/broken 是不是全体 worker 都覆盖不了的持续性问题"
                这类需要横向对比的信息。为 None（默认）时保持旧行为（不渲染
                该小节，向后兼容未传该参数的调用方/旧测试）。

        Returns:
            (MergedPatch Δ_i^t, DecisionLog)
        """
        peer_library_digests = peer_library_digests or {}
        # 无指令时直接返回空 patch（维持现状，不调 LLM）
        if directive is None:
            if self._trace_recorder is not None:
                self._trace_recorder.record_stage2(
                    worker_id=target_worker_id, llm_called=False, merge_action="no_directive",
                    directive_id=directive_id,
                )
            return self._empty_merged_patch(
                target_worker_id, round_idx, None,
                "无 Stage1 指令，维持现状",
                family_id=family_id, task_id=task_id, directive_id=directive_id,
            )

        logger.info(
            "Stage2 执行: worker=%s action=%s workflow=%s",
            target_worker_id, directive.action.value, directive.workflow_name,
        )

        # NO_UPDATE 快速路径（[EXTENSION]，非论文动作）：不需要 LLM 调用
        if directive.action == SkipUpdate.NO_UPDATE:
            if self._trace_recorder is not None:
                self._trace_recorder.record_stage2(
                    worker_id=target_worker_id, llm_called=False, merge_action="no_update",
                    directive_id=directive_id,
                )
            return self._empty_merged_patch(
                target_worker_id, round_idx, directive,
                f"NO_UPDATE: {directive.reason}",
                family_id=family_id, task_id=task_id, directive_id=directive_id,
            )

        # 构建提示词
        low_mem_text = memory_store.get_worker_memory_text(target_worker_id)
        # Full Reproduction Alignment Audit TASK5（Two-Level Memory
        # Alignment）：记录一次低层记忆读取事件——证明该段文本确实被
        # 传入了本次 Stage2 prompt（紧接下面 build() 调用）。仅取已有数据，
        # 不影响任何决策。
        if self._memory_trace_recorder is not None:
            self._memory_trace_recorder.record_read(
                round_idx=round_idx, family_id=family_id or "", stage="stage2_merge",
                memory_level="low_level", worker_id=target_worker_id, content=low_mem_text,
            )
        system_prompt, user_prompt = self._prompt_builder.build(
            round_idx=round_idx,
            target_profile=target_profile,
            directive=directive,
            current_snapshot=current_snapshot,
            peer_patches=peer_patches,
            peer_profiles=peer_profiles,
            peer_library_digests=peer_library_digests,
            capability_tracker=capability_tracker,
            low_level_memory_text=low_mem_text,
        )

        # 调用服务器 backbone
        try:
            raw_dict, call_result = self._backbone.call_json(user_prompt, system_prompt)
            logger.debug(
                "Stage2 LLM 返回: worker=%s tokens=%d cost=%.4f",
                target_worker_id, call_result.total_tokens, call_result.cost_usd,
            )
            # Appendix C 成本复现审计（TASK4）：紧跟 LLM 调用成功之后记一条只读
            # 记录，不影响 raw_dict/call_result 本身或后续解析逻辑。
            if self._cost_recorder is not None:
                self._cost_recorder.record_call(
                    component="stage2_merge",
                    usd_cost=call_result.cost_usd,
                    tokens_input=call_result.prompt_tokens,
                    tokens_output=call_result.completion_tokens,
                    worker_id=target_worker_id,
                    round_idx=round_idx,
                    family_id=family_id,
                    task_id=task_id,
                )
        except LLMCallError as exc:
            logger.error("Stage2 LLM 调用失败 worker=%s: %s", target_worker_id, exc)
            if self._trace_recorder is not None:
                self._trace_recorder.record_stage2(
                    worker_id=target_worker_id, llm_called=True, merge_action="llm_failed",
                    directive_id=directive_id,
                )
            return self._empty_merged_patch(
                target_worker_id, round_idx, directive,
                f"LLM 调用失败: {type(exc).__name__}",
                family_id=family_id, task_id=task_id, directive_id=directive_id,
            )

        # 解析输出（merge decision 完成）
        merged, log = self._parse_output(
            raw_dict, target_worker_id, round_idx, directive,
            call_result.cost_usd, current_snapshot,
            family_id=family_id, task_id=task_id, directive_id=directive_id,
        )

        # 审计日志（顺序约束：必须在 memory 提交之前，对应论文 Section 4.2.2
        # "commit observations to low-level memory" 之前的可审计决策日志要求。
        # merge decision -> audit log(s) -> memory update）
        if self._decision_logger is not None:
            self._decision_logger.log_decision(log)
        # Appendix A 复现能力（TASK3）：与上面 decision_logger 完全对等的旁路
        # 审计消费者，传入 current_snapshot（改动前）+ merged（改动后）以获得
        # content_fidelity="full" 的哈希/diff，不影响本方法任何返回值。
        if self._audit_trace_recorder is not None:
            self._audit_trace_recorder.record(
                log, current_snapshot=current_snapshot, merged_patch=merged
            )

        # Full Reproduction Alignment Audit TASK4（Skill Fusion Fidelity）：
        # 与上面 decision_logger/audit_trace_recorder 完全对等的旁路记录，
        # 取数全部来自已有的 log/peer_patches/current_snapshot，不引入新的
        # LLM 调用或 prompt 字段要求。
        if self._fusion_trace_recorder is not None:
            self._fusion_trace_recorder.record(
                log, peer_patches=peer_patches,
                target_skill_count=current_snapshot.skill_count,
                workflow_name=directive.workflow_name,
            )

        # Full Reproduction Alignment Audit TASK6（Cross-client Transfer
        # Validation）：仅当 action 真正引用了同伴 patch（absorb/refactor 且
        # source_worker_id 非空）时才会产生一条记录（见
        # build_transfer_trace_record() 内部过滤），REPAIR/NO_UPDATE 不会。
        if self._transfer_trace_recorder is not None:
            source_patch = peer_patches.get(directive.source_worker_id or "")
            source_profile = peer_profiles.get(directive.source_worker_id or "")
            self._transfer_trace_recorder.record(
                log, directive_workflow_name=directive.workflow_name,
                source_patch=source_patch, source_profile=source_profile,
                target_profile=target_profile, merged_summary=merged.summary,
            )

        # 更新低层记忆（Stage2 提交）
        new_mem_text = raw_dict.get("updated_low_level_memory", "")
        if isinstance(new_mem_text, str) and new_mem_text.strip():
            memory_store.update_low_level(target_worker_id, new_mem_text, round_idx)
            # TASK5：上面的写入确实发生了，补记一条更新事件（与前面的 record_read
            # 配对，形成读-用-写闭环的完整记录）。
            if self._memory_trace_recorder is not None:
                self._memory_trace_recorder.record_update(
                    round_idx=round_idx, family_id=family_id or "", stage="stage2_merge",
                    memory_level="low_level", worker_id=target_worker_id,
                    new_content=new_mem_text,
                )

        logger.info(
            "Stage2 完成: worker=%s upserts=%d deletions=%d",
            target_worker_id, len(merged.upserts), len(merged.deletions),
        )
        if self._trace_recorder is not None:
            self._trace_recorder.record_stage2(
                worker_id=target_worker_id, llm_called=True,
                merge_action=log.action.value if hasattr(log.action, "value") else str(log.action),
                directive_id=directive_id,
            )
        return merged, log

    # ------------------------------------------------------------------
    # 解析 Stage2 LLM 输出
    # ------------------------------------------------------------------

    def _parse_output(
        self,
        raw: dict[str, Any],
        worker_id: str,
        round_idx: int,
        directive: Directive,
        cost_usd: float,
        current_snapshot: LibrarySnapshot | None = None,
        family_id: str | None = None,
        task_id: str | None = None,
        directive_id: str | None = None,
    ) -> tuple[MergedPatch, DecisionLog]:
        """解析 Stage2 LLM 输出，构建 (MergedPatch, DecisionLog)。"""

        # 字段名归一化
        upserts_raw: dict = raw.get("upsert_files") or raw.get("upserts") or {}
        deletes_raw: list = raw.get("delete_paths") or raw.get("deletions") or []
        summary: str = str(raw.get("summary", ""))[:1000]

        # 路径安全验证
        safe_upserts: dict[str, str] = {}
        for path, content in (upserts_raw if isinstance(upserts_raw, dict) else {}).items():
            safe = validate_safe_rel_path(str(path))
            if safe and isinstance(content, str) and content.strip():
                safe_upserts[safe] = content

        safe_deletes: list[str] = []
        for path in (deletes_raw if isinstance(deletes_raw, list) else []):
            safe = validate_safe_rel_path(str(path))
            if safe:
                safe_deletes.append(safe)

        merged = MergedPatch(
            worker_id=worker_id,
            round_idx=round_idx,
            upserts=safe_upserts,
            deletions=safe_deletes,
            summary=summary,
            cost_usd=cost_usd,
        )

        # 解析 decision_log
        #
        # Full Reproduction Alignment Audit TASK3（Stage1/Stage2 Responsibility
        # Alignment）：
        # Paper motivation: 论文把 "What should happen"（决定 absorb/repair/
        # refactor 中的哪一个）划给 Stage1 Evolution Planning，Stage2 Per-Client
        # Personalized Evolution 只负责 "How to implement"（具体生成/合并哪些
        # 文件内容）。
        # Current mismatch: 此前这里优先读取 Stage2 LLM 在 decision_log.action
        # 字段里自称的动作（`raw_log.get("action", directive.action.value)`），
        # 仅在该字段缺失或无法解析时才回退到 directive.action——这在理论上
        # 允许 Stage2 的 LLM"重新决定"一个与 Stage1 directive.action 不同的
        # 动作类型，越界到 Stage1 的职责范围。
        # Code change: 强制 log_action = directive.action（Stage2 不得改写
        # Stage1 已经决定的 action），LLM 自称的 action 若与 directive 不一致，
        # 只记录一次审计 warning，不采纳，不影响 upsert/delete 内容本身
        # （Stage2 生成文件内容的自由度不受影响）。
        raw_log: dict = raw.get("decision_log") or {}
        log_action = directive.action
        llm_claimed_action = str(raw_log.get("action", "")).lower()
        if llm_claimed_action and llm_claimed_action != directive.action.value:
            logger.warning(
                "Stage2 LLM 在 decision_log.action 中自称 action=%r，与 Stage1 "
                "directive.action=%r 不一致；已强制采用 directive.action"
                "（Stage2 不应重新决定 directive，只记录审计告警）。"
                "worker=%s round=%d",
                llm_claimed_action, directive.action.value, worker_id, round_idx,
            )

        affected_files = list(raw_log.get("affected_files") or safe_upserts.keys())

        # Result Reconstruction Audit（Appendix A）新增：填充此前一直存在于
        # DecisionLog schema、但从未被赋值的 before/after 内容预览（纯审计
        # 便利字段，不影响上面已经决定好的 action/upserts/deletions）。
        # before：改动前 current_snapshot 里对应路径的内容（不存在则为新增文件，
        # 预览为 None）；after：本次 LLM 输出里对应路径的新内容。只取第一个
        # affected file 做预览，与 DecisionLog.before/after_content_preview
        # 字段是单个字符串（非逐文件列表）的既有 schema 保持一致。
        before_preview: str | None = None
        after_preview: str | None = None
        if affected_files:
            first_path = affected_files[0]
            if current_snapshot is not None:
                before_content = current_snapshot.to_path_content_dict().get(first_path)
                if before_content is not None:
                    before_preview = before_content[:200]
            after_content = safe_upserts.get(first_path)
            if after_content is not None:
                after_preview = after_content[:200]

        log = DecisionLog(
            worker_id=worker_id,
            round_idx=round_idx,
            action=log_action,
            source_worker_id=raw_log.get("source_worker_id") or directive.source_worker_id,
            affected_files=affected_files,
            reward=directive.source_reward or 0.0,
            reason=str(raw_log.get("reason") or directive.reason),
            timestamp=datetime.now(timezone.utc).isoformat(),
            before_content_preview=before_preview,
            after_content_preview=after_preview,
            family_id=family_id,
            task_id=task_id,
            directive_id=directive_id,
        )
        return merged, log

    def _empty_merged_patch(
        self,
        worker_id: str,
        round_idx: int,
        directive: Directive | None,
        reason: str,
        family_id: str | None = None,
        task_id: str | None = None,
        directive_id: str | None = None,
    ) -> tuple[MergedPatch, DecisionLog]:
        """生成空 Patch（无指令 / NO_UPDATE / LLM 调用失败降级场景）。

        directive 可能为 None（Stage1 没有为该 worker 下发任何指令，即
        execute_for_worker() 顶部的"无指令时直接返回空 patch"早退路径）——
        此前这里直接读 directive.action/.source_worker_id 会在该早退路径
        上抛 AttributeError（发现于 Phase1 --mock 结构验证：通用 mock
        backbone 不返回任何 directives 时，所有 worker 都会走到这条路径），
        真实实验中 Stage1 LLM 漏下发某个 worker 的指令时也会触发同样的
        崩溃，因此这不是 mock 特有问题，是一个真实的健壮性缺口，一并修复。
        """
        merged = MergedPatch(
            worker_id=worker_id,
            round_idx=round_idx,
            upserts={},
            deletions=[],
            summary=reason,
            cost_usd=0.0,
        )
        log = DecisionLog(
            worker_id=worker_id,
            round_idx=round_idx,
            action=directive.action if directive is not None else SkipUpdate.NO_UPDATE,
            source_worker_id=directive.source_worker_id if directive is not None else None,
            affected_files=[],
            reward=0.0,
            reason=reason,
            timestamp=datetime.now(timezone.utc).isoformat(),
            family_id=family_id,
            task_id=task_id,
            directive_id=directive_id,
        )
        # 审计日志：这三条早退路径（无指令 / NO_UPDATE / LLM 调用失败）都不会走到
        # memory_store.update_low_level()，顺序约束天然满足，仍记录以避免该决策
        # 在 DECISIONS.md 里完全消失（与之前 experiments/federated.py 批量落盘时
        # 的覆盖范围一致）。
        if self._decision_logger is not None:
            self._decision_logger.log_decision(log)
        # Appendix A 复现能力（TASK3）：早退路径没有 current_snapshot/merged
        # 可用（要么没有 patch，要么合并本身为空），退化为 content_fidelity=
        # "unavailable"，但仍产出一条记录，保证"每一次演化事件"都被追踪，
        # 不因为是早退路径而在 evolution_trace.jsonl 里丢失。
        if self._audit_trace_recorder is not None:
            self._audit_trace_recorder.record(log)
        return merged, log
