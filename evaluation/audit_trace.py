"""
evaluation/audit_trace.py — Appendix A 复现能力：EvolutionTraceRecord 审计追踪

背景（论文 Appendix A — Per-Cell Case Analysis）：
    论文用具体的跨轮次案例（例如 "Round 2 Qwen 的 patch 把
    `Demand[Week4]` 改成 `Demand[first_period]` -> ... -> Round 6 Kimi
    执行成功"）来论证"联邦为什么有效"。要复现这类案例分析，需要能够按
    round/family/task/client 精确重建"某一次演化事件"的完整上下文：
    谁的 patch 被采纳、动作是什么（ABSORB/REPAIR/REFACTOR/NO_UPDATE）、
    改了哪个技能文件、改动前后内容的哈希/diff、以及决策理由。

本模块【只做审计日志，不实现/不影响任何算法】：
  - 不修改 EvolutionPlan（server/planner.py，Stage1 规划逻辑如何选择 action/
    priority/source_worker_id）。
  - 不修改 Merge 算法（server/merge.py 的 `_parse_output()` 如何决定
    ABSORB/REPAIR/REFACTOR/NO_UPDATE、如何构建 upserts/deletions，本模块
    完全不触碰，只在决策**已经产出之后**读取其结果）。
  - 不修改 Capability Matrix（server/capability.py）。
  - `EvolutionTraceRecord` 的数据来源是 `server/merge.py` 已经产出的
    `core.datatypes.DecisionLog`（同一份数据）——本模块与已有的
    `server/logging.py::DecisionLogger` 是完全对等的"第二个只读审计消费者"，
    两者互不干扰，都只在 Stage2 决策完成后被动接收结果，不参与、不读取任何
    决策分支。唯一的接入点（server/merge.py::EvolutionExecutor）新增的是一个
    可选的、默认为 None 的 `audit_trace_recorder` 参数 + 一次 `.record()`
    调用，与早已存在、已通过 tests/test_stage2_logging_order.py 验证的
    `decision_logger` 参数是同一种"旁路审计"接入模式，调用顺序保持
    `merge decision -> audit log(s) -> memory update` 不变。

字段对照（用户 Appendix A 需求 -> 本模块实现，字段命名对齐仓库既有惯例
`round_idx`/`worker_id`，同时保留 `client_id`/`source_patch_client` 这两个
用户显式要求的字段名）：

    round               -> EvolutionTraceRecord.round_idx
    family_id           -> EvolutionTraceRecord.family_id
    task_id             -> EvolutionTraceRecord.task_id
    client_id           -> EvolutionTraceRecord.client_id       (= DecisionLog.worker_id)
    source_patch_client -> EvolutionTraceRecord.source_patch_client (= DecisionLog.source_worker_id)
    reward              -> EvolutionTraceRecord.reward
    action              -> EvolutionTraceRecord.action           ("absorb"/"repair"/"refactor"/"no_update")
    skill_path          -> 每个受影响文件一条记录（与 server/logging.py::DecisionEntry 的拆分方式一致）
    before_hash/after_hash -> 改动前后内容的 sha256 十六进制摘要
    diff                -> unified diff 文本（difflib）
    decision_reason     -> EvolutionTraceRecord.decision_reason  (= DecisionLog.reason)

保真度说明（content_fidelity 字段）：
    若调用方能提供改动前的 `LibrarySnapshot`（current_snapshot）和/或改动后
    的 `MergedPatch`（merged_patch），before_hash/after_hash/diff 基于**完整
    文件内容**计算，content_fidelity="full"。
    若两者都未提供，退化为使用 `DecisionLog.before_content_preview`/
    `after_content_preview`（仅 200 字符预览，见 core/datatypes.py），
    content_fidelity="preview_only"，此时的 hash/diff 只能反映预览片段，
    不代表整份文件的真实差异——调用方在做 Appendix A 案例重建时应优先走
    "full" 路径（即在 Stage2 决策现场调用 `AuditTraceRecorder.record()`，
    而不是事后从只含预览的 DecisionLog 反推）。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core.datatypes import DecisionLog, LibrarySnapshot, MergedPatch, PaperMergeAction, SkipUpdate

logger = logging.getLogger(__name__)

ContentFidelity = Literal["full", "preview_only", "unavailable"]


@dataclass
class EvolutionTraceRecord:
    """
    单条可审计的演化事件记录，供重建论文 Appendix A 案例分析使用。

    每条记录对应一次 Stage2 决策里"一个受影响文件"（与
    `server/logging.py::DecisionEntry` 的粒度一致）；没有受影响文件时
    （如 NO_UPDATE）仍产出一条 skill_path="(no file changes)" 的汇总记录，
    避免该决策在审计轨迹里完全消失。
    """

    round_idx: int
    family_id: str | None
    task_id: str | None
    client_id: str
    source_patch_client: str | None
    reward: float
    action: str
    skill_path: str
    before_hash: str | None
    after_hash: str | None
    diff: str
    decision_reason: str
    content_fidelity: ContentFidelity = "unavailable"
    #: [EXTENSION，Algorithm Fidelity Fix 新增] 来自 DecisionLog.directive_id，
    #: 区分同一 (family_id, round_idx, client_id) 下的多个 directive（例如
    #: 'round_2_worker_u0_directive_0'），None 表示旧调用方未提供（保持
    #: 向后兼容，不影响 hash/diff 等既有字段的计算方式）。
    directive_id: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# 纯函数：哈希 / diff / 从 DecisionLog 构建记录
# ---------------------------------------------------------------------------


def compute_hash(content: str | None) -> str | None:
    """对文件内容计算 sha256 十六进制摘要；content 为 None 时返回 None。"""
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_diff(before: str | None, after: str | None, skill_path: str) -> str:
    """生成 unified diff 文本（before/after 任一为 None 时视为空文件）。"""
    before_lines = (before or "").splitlines(keepends=True)
    after_lines = (after or "").splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        before_lines, after_lines,
        fromfile=f"a/{skill_path}", tofile=f"b/{skill_path}", lineterm="",
    ))
    return "\n".join(diff_lines)


def _action_str(action: PaperMergeAction | SkipUpdate | str) -> str:
    return action.value if isinstance(action, (PaperMergeAction, SkipUpdate)) else str(action)


def build_trace_records(
    log: DecisionLog,
    current_snapshot: LibrarySnapshot | None = None,
    merged_patch: MergedPatch | None = None,
) -> list[EvolutionTraceRecord]:
    """
    从一条已经产出的 `DecisionLog` 构建 EvolutionTraceRecord 列表（纯函数，
    不修改传入的任何对象，不参与任何决策）。

    Args:
        log:               `server/merge.py::EvolutionExecutor` 已经产出的
            DecisionLog（Stage2 决策完成后的最终结果）。
        current_snapshot:  该 worker 改动前的库快照（Stage2 输入时的
            `current_snapshot`），提供时可获得 content_fidelity="full"。
        merged_patch:       该 worker 本轮的合并结果（`execute_for_worker()`
            返回的第一个元素），提供时可获得 content_fidelity="full"。
    """
    before_map = current_snapshot.to_path_content_dict() if current_snapshot is not None else {}
    after_map = merged_patch.upserts if merged_patch is not None else {}

    if current_snapshot is not None or merged_patch is not None:
        fidelity: ContentFidelity = "full"
    elif log.before_content_preview is not None or log.after_content_preview is not None:
        fidelity = "preview_only"
    else:
        fidelity = "unavailable"

    paths = log.affected_files or ["(no file changes)"]
    action_str = _action_str(log.action)
    records: list[EvolutionTraceRecord] = []
    for path in paths:
        if fidelity == "full":
            before_content = before_map.get(path)
            after_content = after_map.get(path)
        elif fidelity == "preview_only":
            before_content = log.before_content_preview
            after_content = log.after_content_preview
        else:
            before_content = after_content = None

        records.append(EvolutionTraceRecord(
            round_idx=log.round_idx,
            family_id=log.family_id,
            task_id=log.task_id,
            client_id=log.worker_id,
            source_patch_client=log.source_worker_id,
            reward=log.reward,
            action=action_str,
            skill_path=path,
            before_hash=compute_hash(before_content),
            after_hash=compute_hash(after_content),
            diff=compute_diff(before_content, after_content, path),
            decision_reason=log.reason,
            content_fidelity=fidelity,
            directive_id=log.directive_id,
            timestamp=log.timestamp,
        ))
    return records


# ---------------------------------------------------------------------------
# AuditTraceRecorder：内存缓冲 + 落盘 evolution_trace.jsonl
# ---------------------------------------------------------------------------


class AuditTraceRecorder:
    """
    收集 EvolutionTraceRecord 并落盘为 `evolution_trace.jsonl`（一行一个 JSON
    对象，便于按 round/family/task/client 过滤重建 Appendix A 案例）。

    使用方式（与 `server/logging.py::DecisionLogger` 完全对等的旁路接入）：
        recorder = AuditTraceRecorder()
        executor.set_audit_trace_recorder(recorder)
        ...
        recorder.flush(output_dir)
    """

    def __init__(self) -> None:
        self._records: list[EvolutionTraceRecord] = []

    def record(
        self,
        log: DecisionLog,
        current_snapshot: LibrarySnapshot | None = None,
        merged_patch: MergedPatch | None = None,
    ) -> list[EvolutionTraceRecord]:
        """记录一次 Stage2 决策产生的所有 EvolutionTraceRecord（内存缓冲）。"""
        new_records = build_trace_records(
            log, current_snapshot=current_snapshot, merged_patch=merged_patch
        )
        self._records.extend(new_records)
        return new_records

    @property
    def records(self) -> list[EvolutionTraceRecord]:
        return list(self._records)

    def flush(self, output_dir: Path | str) -> Path:
        """把所有记录写入 `<output_dir>/evolution_trace.jsonl`。"""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "evolution_trace.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        logger.info("evolution_trace.jsonl 已写入: %s（%d 条记录）", path, len(self._records))
        return path


# ---------------------------------------------------------------------------
# 读取 / 查询工具（供 Appendix A 案例重建使用）
# ---------------------------------------------------------------------------


def load_trace_jsonl(path: Path | str) -> list[dict]:
    """读取一份 `evolution_trace.jsonl`，返回 dict 列表；文件不存在返回空列表。"""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def find_records(
    records: list[dict],
    round_idx: int | None = None,
    client_id: str | None = None,
    family_id: str | None = None,
    task_id: str | None = None,
    skill_path: str | None = None,
    source_patch_client: str | None = None,
) -> list[dict]:
    """
    按任意字段组合过滤 EvolutionTraceRecord dict 列表（未指定的条件不参与
    过滤），用于重建"某个 family 里 Round2 Qwen 的 patch 后续如何影响
    Round6 Kimi"这类 Appendix A 案例：先按 family_id+skill_path 找到所有
    相关记录，再按 round_idx 排序即可看到完整演化轨迹。
    """
    def _match(r: dict) -> bool:
        if round_idx is not None and r.get("round_idx") != round_idx:
            return False
        if client_id is not None and r.get("client_id") != client_id:
            return False
        if family_id is not None and r.get("family_id") != family_id:
            return False
        if task_id is not None and r.get("task_id") != task_id:
            return False
        if skill_path is not None and r.get("skill_path") != skill_path:
            return False
        if source_patch_client is not None and r.get("source_patch_client") != source_patch_client:
            return False
        return True

    return [r for r in records if _match(r)]


# ---------------------------------------------------------------------------
# Figure 3 技能增长明细（Result Reproduction Readiness Audit TASK4）
# ---------------------------------------------------------------------------
#
# 下面两个函数纯粹是对 evolution_trace.jsonl 已有记录的只读派生统计，
# 不修改/不重新计算 Stage2 的任何 ABSORB/REPAIR/REFACTOR/NO_UPDATE 决策，
# 也不引入新的"技能"概念——ADD/EDIT/DELETE 的判定规则完全基于
# before_hash/after_hash 两个已有字段。

#: skill_evolution.csv 列顺序（不带 family_id，供扁平/非 family-loop 模式使用）
SKILL_EVOLUTION_FIELDS: list[str] = [
    "round_idx", "worker_id", "added_skills", "edited_skills", "deleted_skills", "total_skills",
]

#: skill_evolution.csv 列顺序（带 family_id，供 family-loop 模式使用）
FAMILY_SKILL_EVOLUTION_FIELDS: list[str] = ["family_id"] + SKILL_EVOLUTION_FIELDS


def _classify_change(before_hash: str | None, after_hash: str | None) -> str | None:
    """
    按 before_hash/after_hash 判断单条 EvolutionTraceRecord 对应的技能文件
    变化类型：

        ADD:    before_hash is None and after_hash is not None
        EDIT:   before_hash is not None and after_hash is not None 且两者不同
        DELETE: before_hash is not None and after_hash is None

    两者都是 None（如 skill_path="(no file changes)" 的 NO_UPDATE 汇总记录，
    或 content_fidelity="unavailable" 时）、或两者相同（内容哈希未变）时
    返回 None，表示"不计入任何一类"，不会被误记为删除/新增。
    """
    if before_hash is None and after_hash is None:
        return None
    if after_hash is None:
        return "delete"
    if before_hash is None:
        return "add"
    if before_hash != after_hash:
        return "edit"
    return None


def build_skill_evolution_rows(
    trace_records: list[dict], family_id: str | None = None,
) -> list[dict]:
    """
    从 `load_trace_jsonl()` 读出的 evolution_trace.jsonl 记录派生
    Figure 3 技能增长明细：按 (round_idx, worker_id) 汇总 added/edited/
    deleted_skills 计数，以及该 worker 在该轮结束时的存活技能总数
    (total_skills)。

    total_skills 的计算方式：按记录在 jsonl 中出现的顺序（写入时即按决策
    发生顺序追加，天然有序），为每个 worker_id 维护一个"当前存活技能路径
    集合"，ADD 时把 skill_path 加入集合、DELETE 时移出集合、EDIT/None 不
    改变集合大小；每处理完一条记录就记下该 worker 当前集合大小，同一
    (round_idx, worker_id) 的最后一次记录决定该行最终的 total_skills。

    跳过 skill_path == "(no file changes)"（NO_UPDATE 的哨兵记录，不代表
    任何真实文件变化）。不依赖 skill_growth.csv/LibrarySnapshot 等任何
    其它数据源，只用 evolution_trace.jsonl 自身重建，避免引入第二套
    技能计数口径。
    """
    alive_paths: dict[str, set[str]] = {}
    buckets: dict[tuple[int, str], dict[str, int]] = {}

    for rec in trace_records:
        skill_path = rec.get("skill_path")
        if skill_path is None or skill_path == "(no file changes)":
            continue
        worker_id = rec.get("client_id")
        round_idx = rec.get("round_idx")
        if worker_id is None or round_idx is None:
            continue

        change = _classify_change(rec.get("before_hash"), rec.get("after_hash"))
        alive = alive_paths.setdefault(worker_id, set())
        bucket = buckets.setdefault(
            (round_idx, worker_id), {"added": 0, "edited": 0, "deleted": 0, "total": 0}
        )
        if change == "add":
            alive.add(skill_path)
            bucket["added"] += 1
        elif change == "edit":
            bucket["edited"] += 1
        elif change == "delete":
            alive.discard(skill_path)
            bucket["deleted"] += 1
        bucket["total"] = len(alive)

    rows: list[dict] = []
    for (round_idx, worker_id), bucket in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        row = {
            "round_idx": round_idx,
            "worker_id": worker_id,
            "added_skills": bucket["added"],
            "edited_skills": bucket["edited"],
            "deleted_skills": bucket["deleted"],
            "total_skills": bucket["total"],
        }
        if family_id is not None:
            row = {"family_id": family_id, **row}
        rows.append(row)
    return rows
