"""
logging.py — 服务端演化审计日志（DECISIONS.md 风格）

对应官方 merge_skill/SKILL.md 中的「DECISIONS.md audit log」要求：
  "For every upsert or delete, output a DECISIONS.md entry with:
   - path, action (keep/modify/replace/add/delete)
   - source: target_own | peer_<wid> | synthesized
   - vs_peers: match_peers | keep_target_with_evidence | target_only_skill
   - reason: concrete evidence (reward, round, what changed)"

本模块在 server 端（EvolutionExecutor）每次调用后自动追加日志行，
便于后续分析演化轨迹、论文消融复现和调试。

日志格式：
  results/<setting>/decisions/<worker_id>/DECISIONS.md
  results/<setting>/decisions/<worker_id>/memory.md  （per-worker 私有洞察）
"""

from __future__ import annotations

import json
import logging
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core.datatypes import DecisionLog, PaperMergeAction, SkipUpdate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

SourceType = Literal["target_own", "synthesized"] | str          # peer_<wid> 也合法
VsPeersType = Literal["match_peers", "keep_target_with_evidence", "target_only_skill"]
ActionType = Literal["keep", "modify", "replace", "add", "delete"]

# 把 core/datatypes.py::DecisionLog.action（PaperMergeAction 三态 / SkipUpdate
# 一态，驱动真实合并逻辑，结构不变）映射成本文件 DecisionEntry.action 的
# 文件级审计词汇（keep/modify/replace/add/delete，只用于生成 DECISIONS.md
# 展示文本，不反向影响任何决策逻辑）。与 experiments/federated.py 里同名映射
# 保持一致（历史遗留的两处小常量表，未合并成单一模块，避免跨层反向依赖）。
_MERGE_ACTION_TO_DECISION_ACTION: dict[str, str] = {
    PaperMergeAction.ABSORB.value: "replace",
    PaperMergeAction.REPAIR.value: "modify",
    PaperMergeAction.REFACTOR.value: "modify",
    SkipUpdate.NO_UPDATE.value: "keep",
}


@dataclass
class DecisionEntry:
    """
    单条 DECISIONS.md 记录。

    每条记录对应一次文件级决策（upsert 或 delete）。
    """

    round_idx: int
    worker_id: str
    path: str                               # 库相对路径，如 skill-name/SKILL.md
    action: ActionType                      # keep / modify / replace / add / delete
    source: SourceType                      # target_own / peer_u1 / synthesized
    vs_peers: VsPeersType                   # 与 peer 库的一致性判断
    reason: str                             # 具体证据（reward、round、变化内容）
    reward_signal: float = 0.0             # 触发此决策的 reward 信号
    merged_from: list[str] = field(default_factory=list)  # 合并来源 worker id 列表
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Result Reconstruction Audit（Appendix A）新增：纯审计字段，不参与
    # 任何 action/source/vs_peers 判断。family_id/task_id 使单条记录能自证属于
    # 哪个 family/task，不再依赖目录路径推断；before/after_content_preview 是
    # DecisionLog 同名字段的透传（200 字符预览）。
    family_id: str | None = None
    task_id: str | None = None
    before_content_preview: str | None = None
    after_content_preview: str | None = None


# ---------------------------------------------------------------------------
# DecisionLogger：写入 DECISIONS.md 和 memory.md
# ---------------------------------------------------------------------------


class DecisionLogger:
    """
    将 DecisionEntry 追加写入 DECISIONS.md 文件。

    每个 worker 一个独立的日志目录，结构为：
      decisions/<worker_id>/DECISIONS.md
      decisions/<worker_id>/memory.md

    使用方式：
        dlog = DecisionLogger(output_dir=Path("results/setting4_full_hetero"))
        dlog.log(entry)
        dlog.set_worker_memory(worker_id="u0", memory_text="...")
        dlog.flush_all()
    """

    def __init__(self, output_dir: Path | str) -> None:
        self._root = Path(output_dir) / "decisions"
        self._root.mkdir(parents=True, exist_ok=True)
        # 内存缓冲：worker_id → list[DecisionEntry]
        self._pending: dict[str, list[DecisionEntry]] = {}
        # 内存缓存：worker_id → memory text
        self._memory: dict[str, str] = {}

    @property
    def root(self) -> Path:
        """DECISIONS.md/memory.md 的落盘根目录（<output_dir>/decisions）。"""
        return self._root

    # ------------------------------------------------------------------
    # 日志记录
    # ------------------------------------------------------------------

    def log(self, entry: DecisionEntry) -> None:
        """追加一条决策记录（内存缓冲，需调用 flush_worker 或 flush_all 写盘）。"""
        self._pending.setdefault(entry.worker_id, []).append(entry)

    def log_merge_result(
        self,
        *,
        round_idx: int,
        worker_id: str,
        upsert_paths: list[str],
        delete_paths: list[str],
        patch_reward: float,
        source_worker: str | None,
        reason: str,
        vs_peers: VsPeersType = "target_only_skill",
    ) -> None:
        """
        从 MergedPatch 结果批量创建 DecisionEntry（便捷接口）。

        Args:
            upsert_paths:  被更新/新增的文件路径列表
            delete_paths:  被删除的文件路径列表
            patch_reward:  来源 patch 的 reward（0.0 = 无 patch）
            source_worker: 来源 worker id（None = 目标自身的 patch）
            reason:        决策理由
        """
        source: SourceType = (
            "target_own" if source_worker is None else f"peer_{source_worker}"
        )
        for path in upsert_paths:
            action: ActionType = "add"  # 可根据 .baseline_library 比较细化为 modify
            self.log(DecisionEntry(
                round_idx=round_idx,
                worker_id=worker_id,
                path=path,
                action=action,
                source=source,
                vs_peers=vs_peers,
                reason=reason,
                reward_signal=patch_reward,
                merged_from=[source_worker] if source_worker else [],
            ))
        for path in delete_paths:
            self.log(DecisionEntry(
                round_idx=round_idx,
                worker_id=worker_id,
                path=path,
                action="delete",
                source=source,
                vs_peers=vs_peers,
                reason=reason,
                reward_signal=patch_reward,
                merged_from=[source_worker] if source_worker else [],
            ))

    def log_decision(self, log: DecisionLog) -> None:
        """
        审计一条 Stage2 DecisionLog（`server/merge.py::EvolutionExecutor` 产出）。

        调用时机约定（Stage2 每次 PaperMergeAction 完成后，对应论文
        Section 4.2.2 "commit observations to low-level memory" 前的
        可审计决策日志要求）：

            merge decision（EvolutionExecutor._parse_output）
              -> log_decision()（本方法，缓冲成 DecisionEntry，供落盘 DECISIONS.md）
              -> EvolutionMemoryStore.update_low_level()（低层记忆提交）

        必须由调用方保证在 memory 提交之前调用本方法。

        DecisionLog.affected_files 是本次决策改动的文件列表；DECISIONS.md
        约定按文件逐行记录，因此每个受影响文件对应一条 DecisionEntry；
        没有受影响文件（如 NO_UPDATE）时仍写一条汇总记录，避免该决策在
        审计日志里完全消失。
        """
        action = _MERGE_ACTION_TO_DECISION_ACTION.get(log.action.value, "keep")
        source = f"peer_{log.source_worker_id}" if log.source_worker_id else "target_own"
        vs_peers: VsPeersType = (
            "match_peers" if log.source_worker_id
            else ("target_only_skill" if action == "keep" else "keep_target_with_evidence")
        )
        for path in (log.affected_files or ["(no file changes)"]):
            self.log(DecisionEntry(
                round_idx=log.round_idx,
                worker_id=log.worker_id,
                path=path,
                action=action,
                source=source,
                vs_peers=vs_peers,
                reason=log.reason,
                reward_signal=log.reward,
                merged_from=[log.source_worker_id] if log.source_worker_id else [],
                timestamp=log.timestamp,
                family_id=log.family_id,
                task_id=log.task_id,
                before_content_preview=log.before_content_preview,
                after_content_preview=log.after_content_preview,
            ))

    # ------------------------------------------------------------------
    # memory.md
    # ------------------------------------------------------------------

    def set_worker_memory(self, worker_id: str, memory_text: str) -> None:
        """更新 worker 的私有 memory.md 内容（Stage2 返回值）。"""
        self._memory[worker_id] = memory_text

    def get_worker_memory(self, worker_id: str) -> str:
        return self._memory.get(worker_id, "")

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def flush_worker(self, worker_id: str) -> None:
        """将 worker 的缓冲条目追加写入 DECISIONS.md，然后清空缓冲。"""
        entries = self._pending.pop(worker_id, [])
        if not entries:
            return
        worker_dir = self._root / worker_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        decisions_path = worker_dir / "DECISIONS.md"

        lines = []
        # 若文件不存在，写表头
        if not decisions_path.exists():
            lines.append(_DECISIONS_HEADER)

        for e in entries:
            diff_preview = ""
            if e.before_content_preview is not None or e.after_content_preview is not None:
                before = (e.before_content_preview or "(new file)").replace("|", "/").replace("\n", " ")
                after = (e.after_content_preview or "(deleted)").replace("|", "/").replace("\n", " ")
                diff_preview = f"`{before[:60]}` -> `{after[:60]}`"
            lines.append(
                f"\n| R{e.round_idx} "
                f"| {e.family_id or ''} "
                f"| {e.task_id or ''} "
                f"| `{e.path}` "
                f"| {e.action} "
                f"| {e.source} "
                f"| {e.vs_peers} "
                f"| reward={e.reward_signal:.3f} "
                f"| {e.reason[:120].replace('|', '/')} "
                f"| {diff_preview} |"
            )

        with open(decisions_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        logger.debug("写入 DECISIONS.md: worker=%s entries=%d", worker_id, len(entries))

    def flush_all(self) -> None:
        """落盘所有 worker 的缓冲条目。"""
        for wid in list(self._pending.keys()):
            self.flush_worker(wid)
        # 同步写 memory.md
        for wid, mem in self._memory.items():
            worker_dir = self._root / wid
            worker_dir.mkdir(parents=True, exist_ok=True)
            memory_path = worker_dir / "memory.md"
            memory_path.write_text(mem, encoding="utf-8")
            logger.debug("写入 memory.md: worker=%s", wid)

    # ------------------------------------------------------------------
    # 摘要工具
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, dict]:
        """
        读取所有 worker 的 DECISIONS.md，返回统计摘要。

        Returns:
            {worker_id: {"decisions_file": str, "total_entries": int, ...}}
        """
        result = {}
        if not self._root.exists():
            return result
        for worker_dir in sorted(self._root.iterdir()):
            if not worker_dir.is_dir():
                continue
            dec_path = worker_dir / "DECISIONS.md"
            mem_path = worker_dir / "memory.md"
            entry_count = 0
            if dec_path.exists():
                content = dec_path.read_text(encoding="utf-8", errors="replace")
                entry_count = content.count("\n| R")
            result[worker_dir.name] = {
                "decisions_file": str(dec_path),
                "total_entries": entry_count,
                "has_memory": mem_path.exists(),
            }
        return result


# ---------------------------------------------------------------------------
# task_memory.md 写入工具
# ---------------------------------------------------------------------------


def write_task_memory(
    output_dir: Path | str,
    family_name: str,
    round_idx: int,
    coverage_text: str,
) -> Path:
    """
    将 Stage1 生成的覆盖矩阵写入 task_memory.md。

    对应官方的 task_memory.md 文件（由 task-update SKILL.md 产出）。

    Args:
        output_dir:     实验输出根目录
        family_name:    任务族名称
        round_idx:      当前 round 序号
        coverage_text:  Stage1 生成的覆盖矩阵 Markdown 文本

    Returns:
        写入文件的路径
    """
    task_mem_dir = Path(output_dir) / "task_memory" / family_name
    task_mem_dir.mkdir(parents=True, exist_ok=True)
    path = task_mem_dir / "task_memory.md"
    header = textwrap.dedent(f"""\
    # task_memory — {family_name}
    <!-- Round {round_idx} | Generated {datetime.now(timezone.utc).isoformat()} -->
    <!-- Per-worker coverage matrix produced by Stage1 (task-update) -->

    """)
    path.write_text(header + coverage_text, encoding="utf-8")
    logger.debug("写入 task_memory.md: family=%s round=%d path=%s",
                 family_name, round_idx, path)
    return path


def read_task_memory(
    output_dir: Path | str,
    family_name: str,
) -> str:
    """
    读取最新的 task_memory.md（Stage2 需要它作为输入）。

    Returns:
        文件内容字符串；若不存在则返回空字符串（Round 0 时为存根）。
    """
    path = Path(output_dir) / "task_memory" / family_name / "task_memory.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 内部常量
# ---------------------------------------------------------------------------

_DECISIONS_HEADER = textwrap.dedent("""\
# DECISIONS.md — merger audit log
<!-- Auto-generated by server/logging.py — DO NOT EDIT MANUALLY -->
<!-- Columns: Round | Family | Task | Path | Action | Source | vs_peers | Signal | Reason | Diff(before->after) -->
<!-- Family/Task/Diff 三列为 Result Reconstruction Audit 新增的纯审计字段，
     不影响 Action/Source/vs_peers 的合并决策语义 -->

| Round | Family | Task | Path | Action | Source | vs_peers | Signal | Reason | Diff(before->after) |
|---|---|---|---|---|---|---|---|---|---|""")
