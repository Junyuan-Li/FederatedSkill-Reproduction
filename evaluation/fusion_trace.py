"""
evaluation/fusion_trace.py — Skill Fusion 可审计追踪（Full Reproduction
Alignment Audit TASK4：Skill Fusion Fidelity）。

Paper motivation:
    论文 Section 4.2.2 描述 Stage2 Evolution Agent 执行的是一次真正的
    "Fusion(δ_1,...,δ_n, L_i^t, M^t)" —— 综合同伴 patch、目标 worker 当前
    技能库、两级记忆，产出语义连贯的合并技能（而不是字符串拼接/直接复制
    某个同伴的技能）。

Current mismatch（审计前状态）:
    `server/merge.py::EvolutionExecutor.execute_for_worker()` 内部确实把
    peer_patches / current_snapshot / low_level_memory_text 都传给了 LLM
    （见 Stage2PromptBuilder.build() 的入参），但"这次融合具体参考了哪些
    输入、采纳了同伴的哪部分信息、丢弃了哪些同伴信息、最终为什么这样改"
    这条推理链此前没有任何结构化记录——只能从 DecisionLog.reason 一句话
    里间接猜测，无法直接支撑论文 Appendix A 的"Qwen discovers rule → server
    merge → Kimi benefits"这类跨轮次案例重建。

Code change:
    新增本模块，作为与 `evaluation/audit_trace.py::AuditTraceRecorder`
    完全对等的旁路记录器（同样的 dataclass + Recorder + JSONL flush 模式），
    在 Stage2 决策完成后记录一条 FusionTraceRecord，取数完全来自已有的
    `raw` LLM 输出 / `peer_patches` / `log`，不改变 execute_for_worker()
    的任何决策逻辑，不引入新的 LLM 调用或新的 prompt 字段要求。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.datatypes import DecisionLog, WorkerPatch

logger = logging.getLogger(__name__)


@dataclass
class FusionTraceRecord:
    """
    单次 Stage2 Fusion(δ_1,...,δ_n, L_i^t, M^t) 的可审计快照。

    字段对照（用户 TASK4 需求 -> 本模块字段）：
        input_patches         -> input_patch_worker_ids（本次 fusion 上下文
                                  中实际可见的同伴 patch 来源 worker）
        selected_information  -> selected_source_worker_id / selected_affected_files
        discarded_information -> discarded_peer_worker_ids
        final_update_reason   -> final_update_reason
    """

    round_idx: int
    family_id: str | None
    task_id: str | None
    target_worker_id: str
    directive_id: str | None
    action: str
    workflow_name: str
    #: 本次 Stage2 调用时，peer_patches 里实际存在的所有同伴 worker_id
    #: （= Fusion 的候选输入集合 {δ_1,...,δ_n} 中，除目标 worker 自身外的部分）
    input_patch_worker_ids: list[str]
    #: 目标 worker 当前库快照的技能数量（Fusion 的 L_i^t 输入是否非空）
    target_library_skill_count: int
    #: 本次决策实际采纳的同伴来源（directive/decision_log 指认的 source_worker_id）
    selected_source_worker_id: str | None
    #: 本次决策实际改动的文件路径（Fusion 输出里真正落地的部分）
    selected_affected_files: list[str]
    #: 存在于 input_patch_worker_ids 中，但未被采纳为来源的同伴（"被丢弃的信息"）
    discarded_peer_worker_ids: list[str]
    #: 最终更新理由（直接取自 DecisionLog.reason，即 LLM 的融合决策解释）
    final_update_reason: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def build_fusion_trace_record(
    log: DecisionLog,
    peer_patches: dict[str, WorkerPatch],
    target_skill_count: int,
    workflow_name: str = "",
) -> FusionTraceRecord:
    """从一次 Stage2 决策的 DecisionLog + peer_patches 构建 FusionTraceRecord。

    纯函数，只读已有数据，不发起任何新的 LLM 调用或决策。
    """
    action_str = log.action.value if hasattr(log.action, "value") else str(log.action)
    input_ids = sorted(peer_patches.keys())
    selected_source = log.source_worker_id
    discarded = [wid for wid in input_ids if wid != selected_source]
    return FusionTraceRecord(
        round_idx=log.round_idx,
        family_id=log.family_id,
        task_id=log.task_id,
        target_worker_id=log.worker_id,
        directive_id=log.directive_id,
        action=action_str,
        workflow_name=workflow_name,
        input_patch_worker_ids=input_ids,
        target_library_skill_count=target_skill_count,
        selected_source_worker_id=selected_source,
        selected_affected_files=list(log.affected_files),
        discarded_peer_worker_ids=discarded,
        final_update_reason=log.reason,
    )


class FusionTraceRecorder:
    """
    收集 FusionTraceRecord 并落盘为 `fusion_trace.jsonl`。

    使用方式（与 AuditTraceRecorder / DecisionLogger 完全对等的旁路接入）：
        recorder = FusionTraceRecorder()
        executor.set_fusion_trace_recorder(recorder)
        ...
        recorder.flush(output_dir)
    """

    def __init__(self) -> None:
        self._records: list[FusionTraceRecord] = []

    def record(
        self,
        log: DecisionLog,
        peer_patches: dict[str, WorkerPatch],
        target_skill_count: int,
        workflow_name: str = "",
    ) -> FusionTraceRecord:
        rec = build_fusion_trace_record(log, peer_patches, target_skill_count, workflow_name)
        self._records.append(rec)
        return rec

    @property
    def records(self) -> list[FusionTraceRecord]:
        return list(self._records)

    def flush(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "fusion_trace.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        logger.info("fusion_trace.jsonl 已写入: %s（%d 条记录）", path, len(self._records))
        return path
