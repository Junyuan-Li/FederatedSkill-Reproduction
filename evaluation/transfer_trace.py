"""
evaluation/transfer_trace.py — 跨客户端技能迁移可审计追踪 + transfer_report
导出（Full Reproduction Alignment Audit TASK6：Cross-client Transfer
Validation）。

Paper motivation:
    论文 Appendix A 的典型案例是 "Client A discovers a skill -> Evolution
    Agent 融合 -> Client B (heterogeneous backbone/harness) 受益"，用于说明
    个性化跨客户端迁移（而非简单参数平均）确实带来了能力提升。

Current mismatch（审计前状态）:
    `server/merge.py` 已经把 peer_patches / directive.source_worker_id 真实
    传给 Stage2 LLM（见 TASK5 审计报告 TASK5 的既有结论），个性化 per-worker
    推送（不是广播）也已确认成立，但此前没有一份"source -> target 迁移"的
    结构化记录，也没有把"迁移后 target worker 的下一轮 trajectory 是否真的
    改善"这条验证链路串起来。

Code change:
    新增本模块：
      1. TransferTraceRecord + TransferTraceRecorder —— 在 Stage2 决策产生
         且 action ∈ {absorb, refactor} 且 directive.source_worker_id 非空时
         （即真正发生了"引用同伴 patch"的迁移），记一条迁移记录（source
         patch 摘要/reward、target 的合并结果摘要），只读已有数据，不引入
         新的 LLM 调用。
      2. enrich_with_trajectory_improvement() —— 事后（下一轮的 reward 已
         经产生之后）用 reward 历史补齐 "trajectory improvement" 字段，
         生成可直接分析的 transfer_report.json。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.datatypes import DecisionLog, PaperMergeAction, WorkerPatch, WorkerProfile

logger = logging.getLogger(__name__)

#: 只有这两个动作涉及"引用了同伴的 patch"，对应论文 Appendix A 的迁移语义。
#: REPAIR 不需要 peer（就地修复），不构成跨客户端迁移。
_TRANSFER_ACTIONS = {PaperMergeAction.ABSORB, PaperMergeAction.REFACTOR}


@dataclass
class TransferTraceRecord:
    """一次「Client A 的 patch -> Evolution Agent -> Client B 的合并更新」记录。"""

    round_idx: int
    family_id: str | None
    workflow_name: str
    action: str
    directive_id: str | None
    source_worker_id: str
    source_backbone_model: str
    source_agent_harness: str
    source_patch_summary: str
    source_reward: float
    target_worker_id: str
    target_backbone_model: str
    target_agent_harness: str
    merged_update_summary: str
    #: 事后补齐：迁移发生前一轮 target worker 的 reward（None 表示尚未补齐）
    target_reward_before: float | None = None
    #: 事后补齐：迁移发生后一轮 target worker 的 reward（None 表示尚未补齐）
    target_reward_after: float | None = None
    #: after - before（两者都存在时才计算，否则为 None）
    trajectory_improvement: float | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def build_transfer_trace_record(
    log: DecisionLog,
    directive_workflow_name: str,
    source_patch: WorkerPatch | None,
    source_profile: WorkerProfile | None,
    target_profile: WorkerProfile,
    merged_summary: str,
) -> TransferTraceRecord | None:
    """
    从一次 Stage2 决策构建 TransferTraceRecord。

    仅当 action ∈ {absorb, refactor} 且 source_worker_id 非空时才构建
    （REPAIR / NO_UPDATE / 无 source 的情况返回 None，不构成跨客户端迁移）。
    """
    action = log.action
    if action not in _TRANSFER_ACTIONS:
        return None
    if not log.source_worker_id:
        return None

    return TransferTraceRecord(
        round_idx=log.round_idx,
        family_id=log.family_id,
        workflow_name=directive_workflow_name,
        action=action.value,
        directive_id=log.directive_id,
        source_worker_id=log.source_worker_id,
        source_backbone_model=source_profile.backbone_model if source_profile else "",
        source_agent_harness=source_profile.agent_harness if source_profile else "",
        source_patch_summary=source_patch.summary if source_patch else "",
        source_reward=source_patch.reward if source_patch else 0.0,
        target_worker_id=log.worker_id,
        target_backbone_model=target_profile.backbone_model,
        target_agent_harness=target_profile.agent_harness,
        merged_update_summary=merged_summary,
    )


class TransferTraceRecorder:
    """
    收集 TransferTraceRecord，并支持事后补齐 trajectory improvement 后导出
    `transfer_report.json`（供 heterogeneous skill transfer 分析使用）。
    """

    def __init__(self) -> None:
        self._records: list[TransferTraceRecord] = []

    def record(
        self,
        log: DecisionLog,
        directive_workflow_name: str,
        source_patch: WorkerPatch | None,
        source_profile: WorkerProfile | None,
        target_profile: WorkerProfile,
        merged_summary: str,
    ) -> TransferTraceRecord | None:
        rec = build_transfer_trace_record(
            log, directive_workflow_name, source_patch, source_profile,
            target_profile, merged_summary,
        )
        if rec is not None:
            self._records.append(rec)
        return rec

    @property
    def records(self) -> list[TransferTraceRecord]:
        return list(self._records)

    def enrich_with_trajectory_improvement(
        self,
        reward_history: dict[str, dict[int, float]],
    ) -> None:
        """
        用 reward 历史（{worker_id: {round_idx: reward}}）就地补齐每条记录的
        target_reward_before / target_reward_after / trajectory_improvement。

        before 取 round_idx 当轮（迁移发生前，target 仍在用旧库执行任务的
        那一轮 reward）；after 取 round_idx + 1（应用 MergedPatch 之后的
        下一轮 reward）。任一缺失时保持 None，不做插值/猜测。
        """
        for rec in self._records:
            history = reward_history.get(rec.target_worker_id, {})
            before = history.get(rec.round_idx)
            after = history.get(rec.round_idx + 1)
            rec.target_reward_before = before
            rec.target_reward_after = after
            if before is not None and after is not None:
                rec.trajectory_improvement = after - before

    def flush_jsonl(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "transfer_trace.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        logger.info("transfer_trace.jsonl 已写入: %s（%d 条记录）", path, len(self._records))
        return path

    def export_transfer_report(
        self,
        output_dir: Path | str,
        reward_history: dict[str, dict[int, float]] | None = None,
    ) -> Path:
        """导出 `transfer_report.json`（TASK6 要求的产物名）。

        若提供 reward_history，会先调用 enrich_with_trajectory_improvement()
        补齐轨迹改善字段；不提供时按已有状态（可能 before/after 为 None）导出。
        """
        if reward_history is not None:
            self.enrich_with_trajectory_improvement(reward_history)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "transfer_report.json"
        payload = [asdict(rec) for rec in self._records]
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info("transfer_report.json 已写入: %s（%d 条记录）", path, len(self._records))
        return path
