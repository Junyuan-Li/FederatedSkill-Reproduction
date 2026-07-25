"""
evaluation/integrity_logs.py — Experiment Integrity Hardening：审计/校验旁路记录器

背景（用户 "FederatedSkill Experiment Integrity Hardening" 需求）：
    真实 API 实验开始前，需要消除可能导致论文结果被静默污染的隐藏 fallback。
    本模块只新增【错误处理 / 日志 / 配置校验】层面的旁路记录器，不触碰
    WorkerPatch schema、Capability Matrix、Memory 设计、Evolution Agent
    决策逻辑、Merge 动作空间——与已有的 `evaluation/cost_accounting.py::
    CostAccountant`、`evaluation/audit_trace.py::AuditTraceRecorder` 完全同构
    的"可选构造参数 + 默认 None 时零行为变化"旁路模式。

三个记录器：
    DistillationFailureRecorder — TASK1：Patch Distillation 蒸馏失败
        （`core.exceptions.PatchDistillationFailure`）在 audit 模式下的记录，
        落盘 `distillation_failed.csv`（列：setting,family_id,round,worker_id,
        reason）。strict 模式（默认）下异常直接向上抛出，不经过本记录器。
    InvalidActionRecorder — TASK2：Stage1 规划器解析到无法识别的 action 字符串
        时（`server/planner.py::EvolutionPlanner._parse_directives()`），拒绝
        该条 directive 并记录到 `invalid_action.log`（不再静默默认为
        ABSORB）。
    ExecutionTraceRecorder — TASK4：`experiment_execution_trace.jsonl`，每轮
        一条 JSON，记录 Stage1（llm_called/plan_generated/fallback_used）、
        Distillation（每 worker 的 llm_called/patch_generated）、Stage2
        （每 worker 的 llm_called/merge_action），用于证明真实实验执行路径
        符合论文（而非静默走了简化分支）。

        [FederatedSkill Cost Accounting Consistency Fix TASK3 扩展]
        `experiments/baseline.py::SelfEvolutionRunner`（Setting1 Self-Evolution，
        论文 Algorithm 1 去掉 server 的客户端部分）结构上没有 Stage1
        Planner/Stage2 Merge，只调用 record_distillation()，从不调用
        record_stage1()/record_stage2()——此前 trace schema 没有任何字段
        显式说明这一点，只能靠 stage1=None/stage2=[] 隐式推断，容易被误判
        成"数据缺失"而不是"该 setting 结构上不适用"。构造时新增可选参数
        `setting_type`（"self_evolution" | "federated" | None），每轮记录
        额外带上：
            setting_type       —— 原样回填构造参数
            planner_enabled    —— 该 setting 是否存在 Stage1 环节
            distillation_enabled —— 该 setting 是否存在 Distillation 环节
            stage2_enabled      —— 该 setting 是否存在 Stage2 环节
        这三个 *_enabled 是**新增的、与既有 stage1/distillation/stage2 字段
        并列的**平铺布尔值，不改变 stage1/distillation/stage2 本身的结构
        （stage1 仍是 dict|None，distillation/stage2 仍是 list），向后兼容
        `tests/test_multi_directive_execution.py` 里
        `rounds[0]["stage2"]` 的 list 遍历方式。setting_type=None（未指定，
        旧调用方的默认行为）时三个 *_enabled 均为 None（"未知"，不猜测）。

    CapabilityMatrixRecorder — FederatedSkill Artifact Fidelity Hardening
        TASK1：`capability_matrix.jsonl`，每轮一条 JSON，落盘完整的
        C^t = (workflow × client state matrix) 逐 cell 状态（直接调用
        `server/capability.py::CapabilityTracker.to_dict()`，不重新计算），
        补齐此前只落盘 covered/absorbing/broken/gap 聚合计数
        （`evaluation/capability_tracker.py::CapabilityEvolutionTracker`）
        无法恢复逐 cell 矩阵这一缺口。
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TASK1: DistillationFailureRecorder
# ---------------------------------------------------------------------------


@dataclass
class DistillationFailureRecord:
    setting: str
    family_id: str
    round_idx: int
    worker_id: str
    reason: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class DistillationFailureRecorder:
    """
    收集 audit 模式下被跳过的蒸馏失败事件，落盘为 `distillation_failed.csv`。

    仅在 `distillation_failure_mode="audit"` 时由 runner
    （`experiments/baseline.py`/`experiments/federated.py`）调用；strict
    模式（默认）下 `PatchDistillationFailure` 直接向上抛出，实验中止，
    不经过本记录器。
    """

    def __init__(self) -> None:
        self._records: list[DistillationFailureRecord] = []

    def record(
        self,
        setting: str,
        family_id: str,
        round_idx: int,
        worker_id: str,
        reason: str,
    ) -> None:
        self._records.append(DistillationFailureRecord(
            setting=setting, family_id=family_id, round_idx=round_idx,
            worker_id=worker_id, reason=reason,
        ))
        logger.error(
            "[audit mode] 蒸馏失败已记录并跳过: setting=%s family=%s round=%d "
            "worker=%s reason=%s",
            setting, family_id, round_idx, worker_id, reason,
        )

    @property
    def records(self) -> list[DistillationFailureRecord]:
        return list(self._records)

    def flush(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "distillation_failed.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["setting", "family_id", "round", "worker_id", "reason", "timestamp"])
            for rec in self._records:
                writer.writerow([
                    rec.setting, rec.family_id, rec.round_idx,
                    rec.worker_id, rec.reason, rec.timestamp,
                ])
        logger.info(
            "distillation_failed.csv 已写入: %s（%d 条记录）", path, len(self._records),
        )
        return path


# ---------------------------------------------------------------------------
# TASK2: InvalidActionRecorder
# ---------------------------------------------------------------------------


class InvalidActionRecorder:
    """
    收集 Stage1 规划解析到的无法识别 action 字符串（被拒绝的 directive），
    落盘为纯文本 `invalid_action.log`（一行一条）。
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def record(
        self,
        round_idx: int,
        family_name: str,
        target_worker_id: str | None,
        raw_action: object,
        error: Exception,
    ) -> None:
        line = (
            f"[{datetime.now(timezone.utc).isoformat()}] round={round_idx} "
            f"family={family_name!r} target_worker_id={target_worker_id!r} "
            f"raw_action={raw_action!r} error={type(error).__name__}: {error} "
            f"-> directive REJECTED（不再静默默认为 ABSORB）"
        )
        self._lines.append(line)
        logger.warning(line)

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    def flush(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "invalid_action.log"
        with path.open("w", encoding="utf-8") as f:
            for line in self._lines:
                f.write(line + "\n")
        logger.info("invalid_action.log 已写入: %s（%d 条记录）", path, len(self._lines))
        return path


# ---------------------------------------------------------------------------
# TASK4: ExecutionTraceRecorder
# ---------------------------------------------------------------------------


class ExecutionTraceRecorder:
    """
    每轮一条 JSON 记录，落盘为 `experiment_execution_trace.jsonl`，
    证明真实实验执行路径符合论文管线（而非静默走了简化/fallback 分支）。

    使用方式（与 CostAccountant/AuditTraceRecorder 一致的旁路接入）：
        recorder = ExecutionTraceRecorder(setting_type="federated")
        recorder.start_round(round_idx, family_id="...")
        recorder.record_distillation(worker_id="w0", llm_called=True, patch_generated=True)
        server.set_trace_recorder(recorder)   # 内部转发给 planner + executor
        ... server.run_round(...) ...          # planner/executor 各自调用 record_stage1/record_stage2
        recorder.finish_round()
        ...
        recorder.flush(output_dir)

    Args:
        setting_type: "self_evolution"（Setting1，无 server，只有
            distillation 环节）或 "federated"（Setting2-4，planner/
            distillation/stage2 三个环节都存在）。None（默认，向后兼容旧
            调用方/已有测试）时不写入任何 *_enabled 判断，落盘的 3 个
            *_enabled 字段均为 None（"未知"，不猜测）。
    """

    #: FederatedSkill Cost Accounting Consistency Fix TASK3：每种 setting_type
    #: 结构上天然具备/不具备哪些环节（与代码结构一一对应，不是猜测）：
    #: self_evolution（experiments/baseline.py::SelfEvolutionRunner）只调用
    #: record_distillation()；federated（experiments/federated.py::
    #: FederatedRunner）三个环节都会被 server/planner.py、server/merge.py 调用。
    _ENABLED_BY_SETTING_TYPE: dict[str, dict[str, bool]] = {
        "self_evolution": {"planner": False, "distillation": True, "stage2": False},
        "federated": {"planner": True, "distillation": True, "stage2": True},
    }

    def __init__(self, setting_type: str | None = None) -> None:
        self._rounds: list[dict] = []
        self._current: dict | None = None
        self._setting_type = setting_type
        self._enabled = self._ENABLED_BY_SETTING_TYPE.get(setting_type, {})

    def start_round(self, round_idx: int, family_id: str | None) -> None:
        self._current = {
            "round": round_idx,
            "family": family_id,
            # FederatedSkill Cost Accounting Consistency Fix TASK3：新增，
            # 与 stage1/distillation/stage2 并列的平铺字段，不改变后三者的
            # 既有结构（stage1 仍是 dict|None，distillation/stage2 仍是 list）。
            "setting_type": self._setting_type,
            "planner_enabled": self._enabled.get("planner"),
            "distillation_enabled": self._enabled.get("distillation"),
            "stage2_enabled": self._enabled.get("stage2"),
            "stage1": None,
            "distillation": [],
            "stage2": [],
        }

    def record_stage1(
        self, *, llm_called: bool, plan_generated: bool, fallback_used: bool,
    ) -> None:
        if self._current is None:
            logger.debug("ExecutionTraceRecorder.record_stage1() 在 start_round() 之前调用，已忽略")
            return
        self._current["stage1"] = {
            "llm_called": llm_called,
            "plan_generated": plan_generated,
            "fallback_used": fallback_used,
        }

    def record_distillation(
        self, *, worker_id: str, llm_called: bool, patch_generated: bool,
    ) -> None:
        if self._current is None:
            logger.debug("ExecutionTraceRecorder.record_distillation() 在 start_round() 之前调用，已忽略")
            return
        self._current["distillation"].append({
            "worker": worker_id,
            "llm_called": llm_called,
            "patch_generated": patch_generated,
        })

    def record_stage2(
        self, *, worker_id: str, llm_called: bool, merge_action: str,
        directive_id: str | None = None,
    ) -> None:
        if self._current is None:
            logger.debug("ExecutionTraceRecorder.record_stage2() 在 start_round() 之前调用，已忽略")
            return
        # Algorithm Fidelity Fix — Multi-Directive Execution：directive_id 为可选
        # 审计字段（None 时保持旧行为/旧格式不变），用于在同一 worker 本轮出现
        # 多条 stage2 记录时区分它们分别对应哪一个 directive（round_evolution.py
        # 恢复 directive cardinality 后，同一 worker 每轮可能产生 >1 条记录）。
        self._current["stage2"].append({
            "worker": worker_id,
            "llm_called": llm_called,
            "merge_action": merge_action,
            "directive_id": directive_id,
        })

    def finish_round(self) -> None:
        if self._current is not None:
            self._rounds.append(self._current)
            self._current = None

    @property
    def rounds(self) -> list[dict]:
        return list(self._rounds)

    def flush(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "experiment_execution_trace.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for row in self._rounds:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info(
            "experiment_execution_trace.jsonl 已写入: %s（%d 轮）", path, len(self._rounds),
        )
        return path


# ---------------------------------------------------------------------------
# FederatedSkill Artifact Fidelity Hardening TASK1: CapabilityMatrixRecorder
# ---------------------------------------------------------------------------


@dataclass
class CapabilityMatrixRoundRecord:
    """单轮完整的 C^t 快照（workflow × worker 逐 cell 状态），供落盘用。"""

    round_idx: int
    matrix: dict[str, dict[str, str]]
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "round_idx": self.round_idx,
            "timestamp": self.timestamp,
            "matrix": self.matrix,
        }


class CapabilityMatrixRecorder:
    """
    FederatedSkill Artifact Fidelity Hardening TASK1。

    背景（审计发现）：`evaluation/capability_tracker.py::
    CapabilityEvolutionTracker` 只落盘 covered/absorbing/broken/gap 的
    **聚合计数**（capability_summary），无法从磁盘恢复论文 Section 4.2.1
    定义的完整 C^t = (workflow × client state matrix) 逐 cell 状态。

    本类只做【记录 + 落盘】，不重新计算/不改变任何能力状态转移逻辑：
    每轮由调用方直接传入 `server.capability.CapabilityTracker.to_dict()`
    的返回值（已有方法，未新增/未修改），本类原样存下来、原样写盘。

    用法（与 ExecutionTraceRecorder/AuditTraceRecorder 完全同构的旁路接入）：
        recorder = CapabilityMatrixRecorder()
        recorder.record(round_idx, capability_tracker.to_dict())
        ...
        recorder.flush(output_dir)   # -> <output_dir>/capability_matrix.jsonl

    落盘格式（一行一个 JSON，对应一轮）：
        {"round_idx": 0, "timestamp": "...", "matrix": {"<workflow>": {"<worker_id>": "covered|absorbing|broken|gap"}}}
    """

    def __init__(self) -> None:
        self._rounds: list[CapabilityMatrixRoundRecord] = []

    def record(self, round_idx: int, matrix: dict[str, dict[str, str]]) -> None:
        """记录一轮完整的能力矩阵快照。`matrix` 必须是
        `CapabilityTracker.to_dict()` 的原样返回值，本方法不做任何重新计算。"""
        self._rounds.append(CapabilityMatrixRoundRecord(round_idx=round_idx, matrix=matrix))

    @property
    def rounds(self) -> list[CapabilityMatrixRoundRecord]:
        return list(self._rounds)

    def flush(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "capability_matrix.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for rec in self._rounds:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        logger.info(
            "capability_matrix.jsonl 已写入: %s（%d 轮）", path, len(self._rounds),
        )
        return path
