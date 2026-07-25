"""
evaluation/cost_accounting.py — Appendix C 成本复现审计：统一 LLM 调用成本核算
                                  + client↔server 通信字节审计

背景（用户审计发现的真实缺口）：
    此前只有 client 执行任务时的 LLM 调用成本（`Trajectory.cost_usd`，经
    `evaluation.metrics.TrialSnapshot.cost_usd` 汇总为 `total_cost_usd`）
    被计入论文 Table/Figure 4 的成本曲线。但联邦流程里至少还有三处同样会
    真实调用 LLM、真实产生费用的地方从未被计入：

        1. Patch Distillation（client/distiller.py::PatchDistiller._step5_call_llm()）
           —— 每个 worker 每轮都会额外调一次自己的 backbone 把轨迹蒸馏成 patch，
           这次调用的 `llm_result.cost_usd`/`prompt_tokens`/`completion_tokens`
           此前被算出来后直接丢弃（只写进 debug log，从未进入任何统计结构）。
        2. Stage1 Evolution Planning（server/planner.py::EvolutionPlanner.plan()）
           —— 每轮一次服务器 backbone 调用，`call_result.cost_usd` 同样只写进
           info log，`EvolutionPlan` 本身也没有 cost 字段承载它。
        3. Stage2 Personalized Evolution（server/merge.py::EvolutionExecutor.
           execute_for_worker()）—— 每个 worker 每轮一次服务器 backbone 调用；
           `MergedPatch.cost_usd` **确实**被填充了，但从未被任何下游汇总逻辑
           读取过（`experiments/federated.py::_run_round()` 的 Apply 阶段只用
           `merged.upserts` 估算 `patch_tokens`，从不读 `merged.cost_usd`）。

    如果论文要复现 Appendix C 的通信/算力成本曲线，只用（1）会系统性低估
    真实总成本（尤其是 Stage1/Stage2 都用更贵的服务器 backbone 时）。

本模块【只做审计核算，不改变任何执行/决策逻辑】：
  - 不修改 EvolutionPlan/MergedPatch/WorkerPatch 的 schema（现有的
    `MergedPatch.cost_usd` 字段保留不变，本模块只是多一个平行的、更细粒度的
    记录路径）。
  - 不修改 Stage1/Stage2/PatchDistiller 如何决定 upsert/delete/reward 的任何
    判断逻辑——所有接入点都是"LLM 调用已经发生、结果已经拿到"之后追加的一次
    只读记录，与已有的 `server/logging.py::DecisionLogger`、
    `evaluation/audit_trace.py::AuditTraceRecorder` 是完全同构的旁路模式
    （可选构造参数 + `set_xxx_recorder()` + 默认 None 时零行为变化）。

字段设计（对齐用户给出的示例结构）：

    LLMCallCostRecord {
        component:      client_execution | patch_distiller |
                        stage1_planner | stage2_merge
        tokens_input:   prompt tokens（来自 BackboneCallResult.prompt_tokens；
                        client_execution 组件只有聚合 total_tokens，无法拆分
                        输入/输出，此时为 None，实际数值落在 tokens_total_hint）
        tokens_output:  completion tokens（同上，client_execution 为 None）
        usd_cost:       该次调用的真实/估算费用（litellm.completion_cost()）
    }

    client_cost  = Σ usd_cost，component ∈ {client_execution, patch_distiller}
    server_cost  = Σ usd_cost，component ∈ {stage1_planner, stage2_merge}
    total_cost   = client_cost + server_cost

通信审计（CommunicationAuditRecord）：
    对应用户要求"audit communication: patch bytes / library snapshot
    bytes / trajectory bytes(not transmitted)"。测量的是真实跨
    client→server 边界的两个对象（`WorkerPatch`/`LibrarySnapshot`，均是
    `experiments/federated.py::_run_round()` 里真实传给
    `FederatedServer.run_round()` 的参数）序列化后的字节数；`trajectory_bytes`
    **硬编码为 0**——不是"刚好测出来是 0"，而是结构性保证：本模块的测量函数
    从不、也无法把 `Trajectory` 内容计入"已传输"字节统计，因为真实流水线里
    `Trajectory` 从未作为参数传给 `FederatedServer.run_round()`（其签名只接受
    `patches: dict[str, WorkerPatch]` + `library_snapshots` + `task_assignments:
    dict[str, str]`，见 server/evolution.py）。`trajectory_bytes_if_transmitted`
    是可选的假设性参考值（如果传入 trajectory 对象则计算其序列化字节数），
    仅用于在报告里对比展示"隐私设计省了多少通信量"，不代表真实发生的传输。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Component = Literal["client_execution", "patch_distiller", "stage1_planner", "stage2_merge"]

#: client_cost = 这两个组件之和（均发生在 client 本地：任务执行 LLM + 蒸馏 LLM）
CLIENT_COMPONENTS: frozenset[str] = frozenset({"client_execution", "patch_distiller"})
#: server_cost = 这两个组件之和（均发生在服务器：Stage1 规划 + Stage2 合并）
SERVER_COMPONENTS: frozenset[str] = frozenset({"stage1_planner", "stage2_merge"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# LLMCallCostRecord + CostAccountant
# ---------------------------------------------------------------------------


@dataclass
class LLMCallCostRecord:
    """单次 LLM 调用的统一成本记录。"""

    component: Component | str
    usd_cost: float = 0.0
    tokens_input: int | None = None
    tokens_output: int | None = None
    #: client_execution 组件专用：Trajectory 只暴露聚合 total_tokens，无法拆分
    #: 输入/输出，此时 tokens_input/tokens_output 均为 None，聚合值记在这里。
    tokens_total_hint: int | None = None
    worker_id: str | None = None
    round_idx: int | None = None
    family_id: str | None = None
    task_id: str | None = None
    timestamp: str = field(default_factory=_now_iso)

    @property
    def tokens_total(self) -> int:
        """优先用 input+output 精确求和；两者都缺失时回退到聚合 hint。"""
        if self.tokens_input is not None or self.tokens_output is not None:
            return (self.tokens_input or 0) + (self.tokens_output or 0)
        return self.tokens_total_hint or 0


class CostAccountant:
    """
    收集 LLMCallCostRecord 并支持按 component/round 聚合 + 落盘
    `cost_ledger.jsonl`（一行一个 JSON 对象）。

    使用方式（与 DecisionLogger/AuditTraceRecorder 完全对等的旁路接入）：
        accountant = CostAccountant()
        planner.set_cost_recorder(accountant)
        executor.set_cost_recorder(accountant)
        distiller.set_cost_recorder(accountant)   # 经 FederatedClient 转发
        ...
        accountant.flush(output_dir)
    """

    def __init__(self) -> None:
        self._records: list[LLMCallCostRecord] = []

    def record_call(
        self,
        component: Component | str,
        usd_cost: float,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        tokens_total_hint: int | None = None,
        worker_id: str | None = None,
        round_idx: int | None = None,
        family_id: str | None = None,
        task_id: str | None = None,
    ) -> LLMCallCostRecord:
        rec = LLMCallCostRecord(
            component=component,
            usd_cost=usd_cost,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_total_hint=tokens_total_hint,
            worker_id=worker_id,
            round_idx=round_idx,
            family_id=family_id,
            task_id=task_id,
        )
        self._records.append(rec)
        return rec

    @property
    def records(self) -> list[LLMCallCostRecord]:
        return list(self._records)

    def records_for_round(self, round_idx: int) -> list[LLMCallCostRecord]:
        return [r for r in self._records if r.round_idx == round_idx]

    @staticmethod
    def _sum_by_components(records: list[LLMCallCostRecord], components: frozenset[str]) -> float:
        return sum(r.usd_cost for r in records if r.component in components)

    def client_cost(self, round_idx: int | None = None) -> float:
        records = self._records if round_idx is None else self.records_for_round(round_idx)
        return self._sum_by_components(records, CLIENT_COMPONENTS)

    def server_cost(self, round_idx: int | None = None) -> float:
        records = self._records if round_idx is None else self.records_for_round(round_idx)
        return self._sum_by_components(records, SERVER_COMPONENTS)

    def total_cost(self, round_idx: int | None = None) -> float:
        return self.client_cost(round_idx) + self.server_cost(round_idx)

    @property
    def total_cost_usd(self) -> float:
        """
        FederatedSkill Cost Accounting Consistency Fix TASK4：`total_cost()`
        （全程，不按 round 过滤）的属性访问别名。调用方（run_experiment.py /
        baseline.py / federated.py 的 CLI 输出层）应统一读取本属性，而不是
        旧的 `sum(s.cost_usd for s in snapshots)`（TrialSnapshot.cost_usd 在
        CLI harness 模式下恒为 0，会让真实调用的费用被静默吞掉）。按 round
        过滤的场景仍用 `total_cost(round_idx=...)`（属性不能带参数）。
        """
        return self.total_cost()

    def total_by_component(self, round_idx: int | None = None) -> dict[str, float]:
        records = self._records if round_idx is None else self.records_for_round(round_idx)
        totals: dict[str, float] = {}
        for r in records:
            totals[r.component] = totals.get(r.component, 0.0) + r.usd_cost
        return totals

    def summary(self) -> dict[str, Any]:
        return {
            "client_cost": self.client_cost(),
            "server_cost": self.server_cost(),
            "total_cost": self.total_cost(),
            "by_component": self.total_by_component(),
            "n_calls": len(self._records),
        }

    def flush(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "cost_ledger.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        logger.info("cost_ledger.jsonl 已写入: %s（%d 条记录，总成本 $%.4f）",
                    path, len(self._records), self.total_cost())
        return path


# ---------------------------------------------------------------------------
# CommunicationAuditRecord + CommunicationAuditor
# ---------------------------------------------------------------------------


def _json_bytes(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def measure_patch_bytes(patch: Any) -> int:
    """
    WorkerPatch δ_i^t 序列化后的字节数——`experiments/federated.py::_run_round()`
    里真实传给 `FederatedServer.run_round(patches=...)` 的对象。
    """
    return _json_bytes({
        "worker_id": patch.worker_id,
        "upserts": patch.upserts,
        "deletions": patch.deletions,
        "reward": patch.reward,
        "summary": patch.summary,
    })


def measure_snapshot_bytes(snapshot: Any) -> int:
    """
    LibrarySnapshot 序列化后的字节数——同样真实传给
    `FederatedServer.run_round(library_snapshots=...)` 的对象。
    """
    return _json_bytes(snapshot.to_path_content_dict())


def measure_trajectory_bytes_if_transmitted(trajectory: Any) -> int:
    """
    假设性参考值：如果 Trajectory 被完整序列化上传会有多大。

    ⚠️ 仅用于报告里对比展示"隐私设计省了多少通信量"，绝不代表真实传输——
    真实流水线中 `Trajectory` 从未作为参数传给 `FederatedServer.run_round()`。
    """
    dump = trajectory.model_dump(mode="json") if hasattr(trajectory, "model_dump") else vars(trajectory)
    return _json_bytes(dump)


@dataclass
class CommunicationAuditRecord:
    """一个 worker 一轮的 client↔server 通信字节审计记录。"""

    round_idx: int
    worker_id: str
    patch_bytes: int
    library_snapshot_bytes: int
    #: 恒为 0——结构性保证，见模块 docstring；不是运行时算出来的"恰好是 0"。
    trajectory_bytes: int = 0
    #: 假设性参考值，默认 0（未提供 trajectory 时不计算）。
    trajectory_bytes_if_transmitted: int = 0
    timestamp: str = field(default_factory=_now_iso)

    @property
    def total_transmitted_bytes(self) -> int:
        """真实跨边界传输的总字节数（不含假设性的 trajectory_bytes_if_transmitted）。"""
        return self.patch_bytes + self.library_snapshot_bytes + self.trajectory_bytes


def build_communication_record(
    round_idx: int,
    worker_id: str,
    patch: Any,
    snapshot: Any,
    trajectory: Any | None = None,
) -> CommunicationAuditRecord:
    """从真实 WorkerPatch + LibrarySnapshot（+ 可选 Trajectory 参考值）构建一条审计记录。"""
    return CommunicationAuditRecord(
        round_idx=round_idx,
        worker_id=worker_id,
        patch_bytes=measure_patch_bytes(patch),
        library_snapshot_bytes=measure_snapshot_bytes(snapshot),
        trajectory_bytes=0,
        trajectory_bytes_if_transmitted=(
            measure_trajectory_bytes_if_transmitted(trajectory) if trajectory is not None else 0
        ),
    )


class CommunicationAuditor:
    """
    收集 CommunicationAuditRecord 并支持聚合 + 落盘 `communication_audit.jsonl`。

    使用方式（与 CostAccountant 完全对等）：
        auditor = CommunicationAuditor()
        auditor.record(round_idx, worker_id, patch, snapshot, trajectory=trajectory)
        ...
        auditor.flush(output_dir)
    """

    def __init__(self) -> None:
        self._records: list[CommunicationAuditRecord] = []

    def record(
        self,
        round_idx: int,
        worker_id: str,
        patch: Any,
        snapshot: Any,
        trajectory: Any | None = None,
    ) -> CommunicationAuditRecord:
        rec = build_communication_record(round_idx, worker_id, patch, snapshot, trajectory)
        self._records.append(rec)
        return rec

    @property
    def records(self) -> list[CommunicationAuditRecord]:
        return list(self._records)

    def records_for_round(self, round_idx: int) -> list[CommunicationAuditRecord]:
        return [r for r in self._records if r.round_idx == round_idx]

    def total_communication_bytes(self, round_idx: int | None = None) -> int:
        records = self._records if round_idx is None else self.records_for_round(round_idx)
        return sum(r.total_transmitted_bytes for r in records)

    def summary(self) -> dict[str, Any]:
        return {
            "total_patch_bytes": sum(r.patch_bytes for r in self._records),
            "total_library_snapshot_bytes": sum(r.library_snapshot_bytes for r in self._records),
            # 恒为 0：Trajectory 从不跨 client→server 边界传输（隐私保证）。
            "total_trajectory_bytes": sum(r.trajectory_bytes for r in self._records),
            "total_trajectory_bytes_if_transmitted": sum(
                r.trajectory_bytes_if_transmitted for r in self._records
            ),
            "total_communication_bytes": self.total_communication_bytes(),
            "n_records": len(self._records),
        }

    def flush(self, output_dir: Path | str) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "communication_audit.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        logger.info(
            "communication_audit.jsonl 已写入: %s（%d 条记录，trajectory_bytes 恒为 0）",
            path, len(self._records),
        )
        return path
