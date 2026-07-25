"""
evaluation/memory_trace.py — 两级记忆读取/使用/更新可审计追踪（Full
Reproduction Alignment Audit TASK5：Two-Level Memory Alignment）。

Paper motivation:
    论文 Section 4.2.1 描述两级记忆 M^t 参与"读 -> 用于本轮演化决策 -> 更新"
    的完整闭环：high-level memory 在 Stage1 被读取并写入下一轮 prompt，
    low-level memory 在 Stage2 被读取（get_worker_memory_text）、注入
    Stage2 prompt、决策完成后又被回写（update_low_level）。

Current mismatch（审计前状态）:
    `server/planner.py` / `server/merge.py` 都已经真实做到了"读 -> 用 ->
    写"（见 server/memory.py 的 update_high_level / update_low_level 以及
    两处 prompt builder 对 memory_store 的调用），但这条链路此前没有任何
    结构化的可审计记录——无法证明"某一轮的 Stage2 决策，究竟有没有真的用到
    低层记忆里的内容"，只能靠阅读源码断言。

Code change:
    新增本模块，作为与 fusion_trace.py / audit_trace.py 完全对等的旁路
    记录器，在 Stage1（读 high-level+low-level，写 high-level）和 Stage2
    （读 low-level，写 low-level）两处各记一条 MemoryAccessRecord，只读
    已有的 memory_store 状态，不改变任何决策逻辑。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class MemoryAccessRecord:
    """
    单次「读取 -> 参与决策 -> （可选）更新」的记忆访问记录。

    memory_level: "high_level"（Stage1 专属）| "low_level"（Stage1 读 + Stage2 读写）
    stage:        "stage1_planning" | "stage2_merge"
    """

    round_idx: int
    family_id: str
    stage: str
    memory_level: str
    #: None 表示 high-level（family 级，无单一 worker），否则为该 worker_id
    worker_id: str | None
    read_content_hash: str
    read_content_preview: str
    #: 本次读取的内容是否被实际传入本次 LLM 决策的 prompt（恒为 True——
    #: 本模块只在真正调用 prompt builder 之前记录一次，若调用方跳过了
    #: LLM 调用（如 NO_UPDATE 快速路径），则根本不会产生这条记录）
    used_in_decision: bool
    updated: bool
    updated_content_hash: str | None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MemoryTraceRecorder:
    """
    收集 MemoryAccessRecord 并落盘为 `memory_access_trace.jsonl`。

    使用方式：
        recorder = MemoryTraceRecorder()
        planner.set_memory_trace_recorder(recorder)   # Stage1 读高层+低层
        executor.set_memory_trace_recorder(recorder)   # Stage2 读写低层
        ...
        recorder.flush(output_dir)
    """

    def __init__(self) -> None:
        self._records: list[MemoryAccessRecord] = []

    def record_read(
        self,
        round_idx: int,
        family_id: str,
        stage: str,
        memory_level: str,
        worker_id: str | None,
        content: str,
    ) -> MemoryAccessRecord:
        """记录一次记忆读取（读取后立即用于本次 LLM 决策 prompt）。"""
        rec = MemoryAccessRecord(
            round_idx=round_idx,
            family_id=family_id,
            stage=stage,
            memory_level=memory_level,
            worker_id=worker_id,
            read_content_hash=_content_hash(content),
            read_content_preview=content[:200],
            used_in_decision=True,
            updated=False,
            updated_content_hash=None,
        )
        self._records.append(rec)
        return rec

    def record_update(
        self,
        round_idx: int,
        family_id: str,
        stage: str,
        memory_level: str,
        worker_id: str | None,
        new_content: str,
    ) -> None:
        """
        标记"最近一次匹配的读取记录"随后确实被更新了（同一
        round/family/stage/memory_level/worker_id 的最后一条记录）。
        若没有匹配的先前读取记录（理论上不应发生，因为更新总是紧随读取
        之后），则新增一条 read_content_hash 为空的独立更新记录，保证
        更新事件本身不丢失。
        """
        for rec in reversed(self._records):
            if (
                rec.round_idx == round_idx
                and rec.family_id == family_id
                and rec.stage == stage
                and rec.memory_level == memory_level
                and rec.worker_id == worker_id
                and not rec.updated
            ):
                rec.updated = True
                rec.updated_content_hash = _content_hash(new_content)
                return
        self._records.append(MemoryAccessRecord(
            round_idx=round_idx,
            family_id=family_id,
            stage=stage,
            memory_level=memory_level,
            worker_id=worker_id,
            read_content_hash="",
            read_content_preview="",
            used_in_decision=False,
            updated=True,
            updated_content_hash=_content_hash(new_content),
        ))

    @property
    def records(self) -> list[MemoryAccessRecord]:
        return list(self._records)

    def flush(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "memory_access_trace.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        logger.info("memory_access_trace.jsonl 已写入: %s（%d 条记录）", path, len(self._records))
        return path
