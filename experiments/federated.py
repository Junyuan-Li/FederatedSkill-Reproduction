"""
federated.py — Federated Evolution Runner（Settings 2-4：联邦协作进化）

对应论文 Algorithm 1 完整流程（Section 4）：
  每一轮 t：
    ┌─ Client Phase ──────────────────────────────────────────────┐
    │  for each client i:                                         │
    │      τ_i^t  ← Execute(L_i^t, ρ_i, x ∼ D_i)  §4.1.1       │
    │      δ_i^t  ← g_i(L_i^t, τ_i^t, ρ_i)         §4.1.2      │
    │      Upload δ_i^t to server                                 │
    └─────────────────────────────────────────────────────────────┘
    ┌─ Server Phase ───────────────────────────────────────────────┐
    │  Receive {δ_1^t, ..., δ_N^t}                                │
    │  Stage1: P^t ← EvolutionPlanner({Digest(L_i^t)})  §4.2.1   │
    │  Stage2: for each i:                               §4.2.2   │
    │             Δ_i^t ← Merge(δ_i^t, P^t, L_i^t, ρ_i)         │
    └─────────────────────────────────────────────────────────────┘
    ┌─ Apply Phase ────────────────────────────────────────────────┐
    │  for each client i:                                          │
    │      L_i^{t+1} ← Apply(L_i^t, Δ_i^t)                       │
    └─────────────────────────────────────────────────────────────┘

三个 Federated 设置：
  Setting 2 - Homo Fed:        3 × GLM-5 + Claude Code（同质化）
  Setting 3 - Hetero Backbone: Qwen/GLM/Kimi + Claude Code（backbone 异构）
  Setting 4 - Full Hetero:     Qwen+QwenCode / GLM+CC / Kimi+KimiCLI（完全异构）
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from benchmark.sampler import TaskSampler
from benchmark.task import Task
from client.executor import TaskExecutor
from client.federated_client import FederatedClient
from core.datatypes import DecisionLog, LibrarySnapshot, MergedPatch, PaperMergeAction, SkipUpdate, Trajectory, WorkerPatch
from core.exceptions import ArtifactRecordingError, PatchDistillationFailure
from evaluation.capability_tracker import CapabilityEvolutionTracker
from evaluation.cost_accounting import CostAccountant, CommunicationAuditor
from evaluation.evaluator import ExperimentEvaluator, ExperimentResult
from evaluation.integrity_logs import CapabilityMatrixRecorder, DistillationFailureRecorder, ExecutionTraceRecorder
from evaluation.metrics import TrialSnapshot
from evaluation.reporter import ResultReporter
from evaluation.selr import compute_selr_from_texts
from server.evolution import FederatedServer
from server.logging import DecisionEntry, DecisionLogger
from evaluation.audit_trace import AuditTraceRecorder
# Full Reproduction Alignment Audit TASK4/5/6：三个模块本身 + server 层的
# set_fusion_trace_recorder()/set_memory_trace_recorder()/
# set_transfer_trace_recorder() 转发方法（server/evolution.py）早已存在，
# 但此前从未在真实实验入口（本文件 FederatedRunner）被构造/注入/落盘——
# 与 AuditTraceRecorder/CostAccountant 是完全对等的旁路审计消费者，此处补齐
# 同样的接入方式（output_dir 非空时创建 + 转发 + run() 结束时 flush）。
from evaluation.fusion_trace import FusionTraceRecorder
from evaluation.memory_trace import MemoryTraceRecorder
from evaluation.transfer_trace import TransferTraceRecorder
from executor.environment import TrialIsolationSpec, isolated_task_trial
from experiments.baseline import _is_retryable_infrastructure_failure
from experiments.task_checkpoint import TaskCheckpointStore

logger = logging.getLogger(__name__)
# 映射成 server/logging.py::DecisionEntry.action 
_MERGE_ACTION_TO_DECISION_ACTION: dict[str, str] = {
    PaperMergeAction.ABSORB.value: "replace",
    PaperMergeAction.REPAIR.value: "modify",
    PaperMergeAction.REFACTOR.value: "modify",
    SkipUpdate.NO_UPDATE.value: "keep",
}


def _decision_log_to_entries(log: DecisionLog) -> list[DecisionEntry]:

    action = _MERGE_ACTION_TO_DECISION_ACTION.get(log.action.value, "keep")
    source = f"peer_{log.source_worker_id}" if log.source_worker_id else "target_own"
    vs_peers = (
        "match_peers" if log.source_worker_id
        else ("target_only_skill" if action == "keep" else "keep_target_with_evidence")
    )
    paths = log.affected_files or ["(no file changes)"]
    return [
        DecisionEntry(
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
        )
        for path in paths
    ]


class FederatedRunner:
    """
    Settings 2-4：联邦协作进化。

    与 SelfEvolutionRunner 的区别：
      - 有 FederatedServer 参与 Stage1/Stage2 推理
      - 上传 WorkerPatch（δ_i^t）而非原始轨迹（隐私保护）
      - 应用 server 返回的 MergedPatch（Δ_i^t）而非自己的 patch

    Args:
        clients:                   参与联邦的 FederatedClient 列表
        server:                    FederatedServer 实例
        executor:                  具备 .run(task, library, profile, round_idx) 接口的
            执行器  本类不关心具体是哪个实现，
            只负责统一调用（Phase16 路由链路审计已确认）。
        sampler:                   任务采样器
        rounds:                    联邦轮数 T
        setting_name:              实验设置名
        reporter:                  ResultReporter
        disable_capability_matrix: A1 ablation — 禁用能力矩阵（Stage1 不使用覆盖历史）
        disable_patch_distillation:A3 ablation — 跳过蒸馏，上传轨迹摘要代替 patch
        output_dir:                
    """

    def __init__(
        self,
        clients: list[FederatedClient],
        server: FederatedServer,
        executor: TaskExecutor,
        sampler: TaskSampler,
        rounds: int = 8,
        setting_name: str = "FederatedSkill",
        reporter: ResultReporter | None = None,
        disable_capability_matrix: bool = False,
        disable_patch_distillation: bool = False,
        output_dir: Path | str | None = None,
        distillation_failure_mode: str = "strict",
        strict_artifact_mode: bool = True,
        max_retry: int = 2,
    ) -> None:
        self._clients = clients
        self._server = server
        self._executor = executor
        self._sampler = sampler
        self._rounds = rounds
        self._setting_name = setting_name
        self._reporter = reporter or ResultReporter()
        self._evaluator = ExperimentEvaluator(setting_name=setting_name)
        self._disable_capability_matrix = disable_capability_matrix
        self._disable_patch_distillation = disable_patch_distillation
        # Phase14 新增：跨轮能力矩阵历史记录器，
        # 用于为 paper_export.py 的 capability.csv 提供真实的
        # covered/absorbing/broken/gap 四态数据。
        self._capability_history = CapabilityEvolutionTracker()
        # FederatedSkill Artifact Fidelity Hardening TASK1：与上面各旧有
        # recorder 完全对等的旁路审计消费者，同样仅在 output_dir 非空时创建，
        # 向后兼容旧调用方/已有测试。落盘完整 C^t 逐 cell 状态
        # （capability_matrix.jsonl），补齐 CapabilityEvolutionTracker 只有
        # 聚合计数、无法恢复逐 cell 矩阵这一缺口。
        self._capability_matrix_recorder = (
            CapabilityMatrixRecorder() if output_dir is not None else None
        )
        # FederatedSkill Artifact Fidelity Hardening TASK2：
        self._strict_artifact_mode = strict_artifact_mode

        self._decision_logger = DecisionLogger(output_dir) if output_dir is not None else None
        # Result Reconstruction Audit（Appendix A ，TASK3）：
        self._audit_trace_recorder = AuditTraceRecorder() if output_dir is not None else None
        # Appendix C 成本复现审计（TASK4）：
        # 与上面 DecisionLogger/AuditTraceRecorder
     
        self._cost_accountant = CostAccountant() if output_dir is not None else None
        self._communication_auditor = CommunicationAuditor() if output_dir is not None else None
        # Full Reproduction Alignment Audit TASK4/5/6：与上面各 recorder 完全
        # 对等的旁路审计消费者，同样只在 output_dir 非空时创建。
        self._fusion_trace_recorder = FusionTraceRecorder() if output_dir is not None else None
        self._memory_trace_recorder = MemoryTraceRecorder() if output_dir is not None else None
        self._transfer_trace_recorder = TransferTraceRecorder() if output_dir is not None else None
        # TASK6：export_transfer_report() 需要事后补齐 trajectory improvement，
        # 用真实 reward 历史（{worker_id: {round_idx: reward}}），在 _run_round()
        # 里逐轮追加，不重新计算——直接复用已有的 trajectories_info[wid]["reward"]。
        self._reward_history: dict[str, dict[int, float]] = {}
        self._output_dir = output_dir
        if max_retry < 0:
            raise ValueError("max_retry 必须 >= 0")
        self._max_retry = max_retry
        self._checkpoint_store = TaskCheckpointStore(output_dir)

        # Experiment Integrity Hardening TASK1
        if distillation_failure_mode not in ("strict", "audit"):
            raise ValueError(
                f"distillation_failure_mode 必须为 'strict' 或 'audit'，收到: "
                f"{distillation_failure_mode!r}"
            )
        self._distillation_failure_mode = distillation_failure_mode
        # Experiment Integrity Hardening TASK1/TASK4：与上面各旧有 recorder 完全
        # 对等的新增旁路审计消费者，同样仅在 output_dir 非空时创建，向后
        # 兼容旧调用方/已有测试。
        self._distillation_failure_recorder = (
            DistillationFailureRecorder() if output_dir is not None else None
        )
        self._execution_trace = (
            # FederatedSkill Cost Accounting Consistency Fix TASK3：
            # federated 结构上 planner/distillation/stage2 三个环节都存在，
          
            ExecutionTraceRecorder(setting_type="federated") if output_dir is not None else None
        )
        # 问题3 Action 3：把 DecisionLogger 转发进 server
        self._server.set_decision_logger(self._decision_logger)
        self._server.set_audit_trace_recorder(self._audit_trace_recorder)
        # Appendix C 成本复现审计（TASK4）：转发到 server（同时覆盖 Stage1
        # 规划器 + Stage2 执行器，见 server/evolution.py::set_cost_recorder），
        # 并转发到每个 client（同时覆盖其内部 PatchDistiller）。output_dir 未
        # 提供时转发 None，等价于关闭成本审计，行为不变。
        self._server.set_cost_recorder(self._cost_accountant)
        for client in self._clients:
            client.set_cost_recorder(self._cost_accountant)
        # Experiment Integrity Hardening TASK4：转发执行轨迹记录器到 server
        # output_dir 未提供时转发 None，等价于关闭执行轨迹记录，行为不变。
        self._server.set_trace_recorder(self._execution_trace)
        # Full Reproduction Alignment Audit TASK4/5/6：转发到 server
        # （FederatedServer.set_fusion_trace_recorder/set_memory_trace_recorder/
        # set_transfer_trace_recorder 早已存在，见 server/evolution.py，
        # output_dir 未提供时转发 None，等价于关闭这三项审计，行为不变。
        self._server.set_fusion_trace_recorder(self._fusion_trace_recorder)
        self._server.set_memory_trace_recorder(self._memory_trace_recorder)
        self._server.set_transfer_trace_recorder(self._transfer_trace_recorder)

        if disable_capability_matrix:
            logger.info("[Ablation A1] disable_capability_matrix=True")
        if disable_patch_distillation:
            logger.info("[Ablation A3] disable_patch_distillation=True")

    @property
    def capability_history(self) -> CapabilityEvolutionTracker:
        """跨轮能力矩阵历史（Phase14：供 run_experiment.py 将真实四态数据持久化到 round JSON）。"""
        return self._capability_history

    def run(self) -> ExperimentResult:
        """
        运行完整的 Federated 实验，返回 ExperimentResult。

        Algorithm 1 完整流程（论文 Section 4）。
        """
        logger.info(
            "FederatedRunner 开始: %d 轮, %d clients, setting=%s",
            self._rounds, len(self._clients), self._setting_name,
        )
        t_exp_start = time.monotonic()

        for round_idx in range(self._rounds):
            snapshots = self._run_round(round_idx)
            round_result = self._evaluator.record_round(round_idx, snapshots)
            # Appendix C 成本复现审计（TASK4）：额外把本轮的统一成本/通信字节
            # 注入 round_result.metrics
            if self._cost_accountant is not None:
                round_result.metrics["client_cost_usd"] = self._cost_accountant.client_cost(round_idx)
                round_result.metrics["server_cost_usd"] = self._cost_accountant.server_cost(round_idx)
                round_result.metrics["total_cost_usd_unified"] = self._cost_accountant.total_cost(round_idx)
                round_result.metrics["total_cost_usd"] = round_result.metrics["total_cost_usd_unified"]
                by_component = self._cost_accountant.total_by_component(round_idx)
                round_result.metrics["client_execution_cost"] = by_component.get("client_execution", 0.0)
                round_result.metrics["patch_distill_cost"] = by_component.get("patch_distiller", 0.0)
                round_result.metrics["stage1_cost"] = by_component.get("stage1_planner", 0.0)
                round_result.metrics["stage2_cost"] = by_component.get("stage2_merge", 0.0)
            if self._communication_auditor is not None:
                round_comm_records = self._communication_auditor.records_for_round(round_idx)
                round_result.metrics["communication_bytes"] = sum(
                    r.total_transmitted_bytes for r in round_comm_records
                )
                
                round_result.metrics["communication_trajectory_bytes"] = sum(
                    r.trajectory_bytes for r in round_comm_records
                )
            self._reporter.print_round(round_result)

        result = self._evaluator.finalize()
        elapsed = time.monotonic() - t_exp_start
        result.metadata.update({
            "elapsed_seconds": elapsed,
            "server_summary": self._server.summary(),
        })
        # Appendix C 成本复现审计（TASK4）：全程累积摘要，供报告/人工核对使用
        # （不影响 final_metrics 里已有的任何键，纯新增 metadata 字段）。
        if self._cost_accountant is not None:
            result.metadata["cost_accounting_summary"] = self._cost_accountant.summary()
            # 全程汇总的Total Cost 同样统一改为读取 CostAccountant，
            # 不再是finalize() 内部对 TrialSnapshot.cost_usd 的旧式求和。
            result.final_metrics["total_cost_usd"] = self._cost_accountant.total_cost_usd
        if self._communication_auditor is not None:
            result.metadata["communication_audit_summary"] = self._communication_auditor.summary()
        self._reporter.print_summary(result)

        # 最终论文一致性收口 Priority 2：落盘 DECISIONS.md + memory.md
        # （对应论文 Section 4.2.2 的可审计决策日志要求）。output_dir 未提供时
        # self._decision_logger 为 None，跳过，不影响未传该参数的旧调用方。
        if self._decision_logger is not None:
            for client in self._clients:
                memory_text = self._server.memory_store.get_worker_memory_text(client.worker_id)
                if memory_text:
                    self._decision_logger.set_worker_memory(client.worker_id, memory_text)
            self._decision_logger.flush_all()
            logger.info("DECISIONS.md / memory.md 已落盘: %s", self._decision_logger.root)

        # Result Reconstruction Audit（Appendix A 复现能力，TASK3）：落盘
        # evolution_trace.jsonl。与上面 DecisionLogger 落盘同样只在 output_dir
        # 非空时执行，不影响未传该参数的旧调用方。
        if self._audit_trace_recorder is not None:
            trace_path = self._audit_trace_recorder.flush(self._output_dir)
            logger.info("evolution_trace.jsonl 已落盘: %s", trace_path)

        if self._cost_accountant is not None:
            cost_path = self._cost_accountant.flush(self._output_dir)
            logger.info("cost_ledger.jsonl 已落盘: %s", cost_path)
        if self._communication_auditor is not None:
            comm_path = self._communication_auditor.flush(self._output_dir)
            logger.info("communication_audit.jsonl 已落盘: %s", comm_path)

        if self._distillation_failure_recorder is not None:
            failed_path = self._distillation_failure_recorder.flush(self._output_dir)
            logger.info("distillation_failed.csv 已落盘: %s", failed_path)
        if self._execution_trace is not None:
            trace_jsonl_path = self._execution_trace.flush(self._output_dir)
            logger.info("experiment_execution_trace.jsonl 已落盘: %s", trace_jsonl_path)

    
        if self._capability_matrix_recorder is not None:
            capability_matrix_path = self._capability_matrix_recorder.flush(self._output_dir)
            logger.info("capability_matrix.jsonl 已落盘: %s", capability_matrix_path)

        self._export_capability_evolution_csv()


        if self._fusion_trace_recorder is not None:
            fusion_path = self._fusion_trace_recorder.flush(self._output_dir)
            logger.info("fusion_trace.jsonl 已落盘: %s", fusion_path)

        # Full Reproduction Alignment Audit TASK5（Two-Level Memory
        # Alignment）：落盘 memory_access_trace.jsonl。
        if self._memory_trace_recorder is not None:
            memory_trace_path = self._memory_trace_recorder.flush(self._output_dir)
            logger.info("memory_access_trace.jsonl 已落盘: %s", memory_trace_path)

   
        if self._transfer_trace_recorder is not None:
            transfer_jsonl_path = self._transfer_trace_recorder.flush_jsonl(self._output_dir)
            logger.info("transfer_trace.jsonl 已落盘: %s", transfer_jsonl_path)
            transfer_report_path = self._transfer_trace_recorder.export_transfer_report(
                self._output_dir, reward_history=self._reward_history,
            )
            logger.info("transfer_report.json 已落盘: %s", transfer_report_path)

        logger.info("FederatedRunner 结束: 耗时 %.1fs", elapsed)
        return result

    def _run_round(self, round_idx: int) -> list[TrialSnapshot]:
        """执行单 round 的三阶段流程。"""
        if self._execution_trace is not None:
            self._execution_trace.start_round(round_idx, family_id=self._server.family_name)

        worker_ids = [c.worker_id for c in self._clients]
        assignments = self._sampler.sample_batch(worker_ids, round_idx)

        # ── Phase 1: Client Phase ────────────────────────────────────────
        patches: dict[str, WorkerPatch] = {}
        lib_snapshots_before: dict[str, LibrarySnapshot] = {}
        trajectories_info: dict[str, dict] = {}   # wid → {tokens, cost, reward, task_id}

        for client in self._clients:
            wid = client.worker_id
            task = assignments[wid]
            lib_snapshots_before[wid] = client.library.snapshot(round_idx)
            trajectory, patch = self._run_client_phase_with_retry(
                client=client,
                task=task,
                round_idx=round_idx,
                initial_library=lib_snapshots_before[wid],
            )
            patches[wid] = patch

            # Appendix C 通信审计（TASK4）：测量真实跨 client→server 边界传输的
            # 两个对象（WorkerPatch + LibrarySnapshot，均是下面 self._server.run_round()
            # 真实接收的参数）序列化后的字节数；trajectory 仅作为
            # trajectory_bytes_if_transmitted 的可选参考值传入（不代表真实
            # 传输，trajectory_bytes 本身恆为 0，见 cost_accounting.py docstring）。
            if self._communication_auditor is not None:
                self._communication_auditor.record(
                    round_idx, wid, patch, lib_snapshots_before[wid], trajectory=trajectory,
                )

            # Phase14 新增：基于真实文本计算 SELR（论文 Appendix E Eq.5）——
            # source = 完整轨迹文本，target = 即将上传 server 的 patch 文本，
            # 两边都只在 client 本地计算，不影响“patch 而不是轨迹上传 server”的
            # 隐私设计（Appendix E）。
            trajectory_text = "\n".join(step.content for step in trajectory.steps) + "\n" + (
                trajectory.final_message or ""
            )
            patch_text = "\n".join(patch.upserts.values())
            selr_info = compute_selr_from_texts(trajectory_text, patch_text)

            # 记录 traj 信息（用于后续指标计算）
            trajectories_info[wid] = {
                "traj_tokens": trajectory.total_tokens,
                "cost_usd": trajectory.cost_usd,
                "reward": trajectory.reward or 0.0,
                "task_id": task.task_id,
                "selr": selr_info["selr"],
                "n_sensitive": selr_info["n_sensitive"],
                "n_leaked": selr_info["n_leaked"],
            }
            logger.debug(
                "Client phase: round=%d worker=%s task=%s reward=%.1f",
                round_idx, wid, task.task_id, trajectory.reward or 0.0,
            )
            # Full Reproduction Alignment Audit TASK6（Cross-client Transfer
            # Validation）：累积每个 worker 每轮的真实 reward，供 run() 结束时
            # TransferTraceRecorder.export_transfer_report() 事后补齐
            # target_reward_before/after/trajectory_improvement（round_idx 与
            # round_idx+1 两轮 reward 之差），只读，不影响本轮任何决策。
            self._reward_history.setdefault(wid, {})[round_idx] = trajectory.reward or 0.0

        # ── Phase 2: Server Phase ────────────────────────────────────────
        # server.run_round 内部执行 Stage1（EvolutionPlanner）+ Stage2（EvolutionExecutor）
        # 只接收 patch（而非轨迹），保护隐私  (Appendix E)
        # Result Reconstruction Audit（Appendix A）新增：把本轮每个 worker 真实
        # 执行的 task_id 传给 server.run_round()——该形参此前一直存在
        # （server/evolution.py::run_round 签名早就有 task_assignments），但
        # 从未被本调用方真正传入，导致 DecisionLog/RoundRecord 无法自证本次
        # 决策对应哪个 task_id，只能靠"同一 round 目录下另一份 round JSON 的
        # snapshots 列表"间接反查。只是把已有信息透传，不改变 Stage1/Stage2
        # 任何决策逻辑。
        task_assignments = {wid: trajectories_info[wid]["task_id"] for wid in worker_ids}
        merged_patches: dict[str, MergedPatch] = self._server.run_round(
            round_idx=round_idx,
            patches=patches,
            library_snapshots=lib_snapshots_before,
            task_assignments=task_assignments,
            disable_capability_matrix=self._disable_capability_matrix,
        )

        # 问题3 Action 3：DecisionLog → DECISIONS.md 的落盘调用已下沉到
        # server/merge.py::EvolutionExecutor.execute_for_worker()（在 merge
        # decision 完成后、memory 提交之前调用，见该文件注释），此处不再
        # 重复调用 _decision_log_to_entries()/self._decision_logger.log()，
        # 避免同一条 DecisionLog 被记录两次。_decision_log_to_entries()
        # 函数本身保留（tests/test_decision_logger_wiring.py 仍直接测试其
        # 映射规则，且与 server/logging.py::DecisionLogger.log_decision()
        # 内部使用的是同一套映射规则）。

        # Phase14 新增 / FederatedSkill Artifact Fidelity Hardening TASK1/2：
        # 本轮结束后抓一份能力矩阵快照入历史记录器 + 落盘 capability_matrix.jsonl。
        # 抽成独立方法，便于单测直接验证 strict_artifact_mode 的两种行为。
        self._record_capability_matrix(round_idx)

        # ── Phase 3: Apply Phase ─────────────────────────────────────────
        snapshots: list[TrialSnapshot] = []
        for client in self._clients:
            wid = client.worker_id
            lib_before = lib_snapshots_before[wid].skill_count

            merged = merged_patches.get(wid)
            if merged is not None:
                client.apply_update(merged)
                # patch_tokens：以 upserts 内容字符数/4 估算
                patch_tokens = sum(
                    len(v) for v in merged.upserts.values()
                ) // 4
            else:
                logger.warning("Round %d: server 未返回 worker %s 的 MergedPatch", round_idx, wid)
                patch_tokens = 0

            lib_after = client.library.snapshot(round_idx).skill_count
            info = trajectories_info[wid]

            snap = TrialSnapshot(
                round_idx=round_idx,
                worker_id=wid,
                task_id=info["task_id"],
                reward=info["reward"],
                trajectory_tokens=info["traj_tokens"],
                patch_tokens=patch_tokens,
                library_size_before=lib_before,
                library_size_after=lib_after,
                cost_usd=info["cost_usd"],
                selr=info["selr"],
                n_sensitive_entities=info["n_sensitive"],
                n_leaked_entities=info["n_leaked"],
            )
            snapshots.append(snap)

            logger.debug(
                "Fed Round %d worker=%s reward=%.1f lib:%d→%d",
                round_idx, wid, info["reward"], lib_before, lib_after,
            )

        if self._execution_trace is not None:
            self._execution_trace.finish_round()
        return snapshots

    def _run_client_phase_with_retry(
        self,
        *,
        client: FederatedClient,
        task: Task,
        round_idx: int,
        initial_library: LibrarySnapshot,
    ) -> tuple[Trajectory, WorkerPatch]:
        """重试客户端 Execute+Distill；server phase 始终只执行一次。"""
        wid = client.worker_id
        for attempt in range(1, self._max_retry + 2):
            try:
                artifact_dir = self._checkpoint_store.trial_artifact_dir(
                    wid, task.task_id
                )
                if artifact_dir is None or task.metadata.get("source") != "skillflow_real":
                    trajectory = self._executor.run(
                        task=task,
                        library=client.library,
                        profile=client.profile,
                        round_idx=round_idx,
                    )
                else:
                    isolation = TrialIsolationSpec(
                        artifact_dir=artifact_dir / "workers" / wid,
                        task_id=task.task_id,
                        worker_id=wid,
                        round_idx=round_idx,
                        attempt=attempt,
                        instruction=task.description,
                        runtime_metadata={
                            "agent_timeout_seconds": task.metadata.get("agent_timeout_seconds"),
                            "agent_timeout_source": task.metadata.get("agent_timeout_source"),
                            "verifier_timeout_seconds": task.verification.timeout_seconds,
                            "verifier_timeout_source": task.metadata.get("verifier_timeout_source"),
                            "environment_timeout_seconds": task.metadata.get("environment_timeout_seconds"),
                            "environment_timeout_source": task.metadata.get("environment_timeout_source"),
                            "environment": task.metadata.get("environment", {}),
                        },
                    )
                    with isolated_task_trial(isolation):
                        trajectory = self._executor.run(
                            task=task,
                            library=client.library,
                            profile=client.profile,
                            round_idx=round_idx,
                        )
                self._checkpoint_store.save_trajectory_reward(trajectory, attempt)
                if self._cost_accountant is not None:
                    self._cost_accountant.record_call(
                        component="client_execution",
                        usd_cost=trajectory.cost_usd,
                        tokens_total_hint=trajectory.total_tokens,
                        worker_id=wid,
                        round_idx=round_idx,
                        task_id=task.task_id,
                    )

                if self._disable_patch_distillation:
                    patch = WorkerPatch(
                        worker_id=wid,
                        upserts={},
                        deletions=[],
                        reward=trajectory.reward or 0.0,
                        summary=trajectory.final_message[:500] if trajectory.final_message else "",
                    )
                    logger.debug("[Ablation A3] 跳过蒸馏，使用 trajectory summary as patch")
                    if self._execution_trace is not None:
                        self._execution_trace.record_distillation(
                            worker_id=wid, llm_called=False, patch_generated=True,
                        )
                else:
                    try:
                        patch = client.distill_patch(trajectory)
                    except PatchDistillationFailure as exc:
                        if self._distillation_failure_mode == "strict" or self._output_dir is None:
                            raise
                        logger.error(
                            "[audit 模式] 蒸馏失败，本轮 worker=%s 不上传 patch: %s",
                            wid, exc,
                        )
                        if self._distillation_failure_recorder is not None:
                            self._distillation_failure_recorder.record(
                                setting=self._setting_name,
                                family_id=self._server.family_name,
                                round_idx=round_idx,
                                worker_id=wid,
                                reason=str(exc),
                            )
                        if self._execution_trace is not None:
                            self._execution_trace.record_distillation(
                                worker_id=wid, llm_called=True, patch_generated=False,
                            )
                        patch = WorkerPatch(
                            worker_id=wid,
                            upserts={},
                            deletions=[],
                            reward=trajectory.reward or 0.0,
                            summary="[audit 模式] 蒸馏失败，本轮无 patch",
                        )
                    else:
                        if self._execution_trace is not None:
                            self._execution_trace.record_distillation(
                                worker_id=wid, llm_called=True, patch_generated=True,
                            )

                self._checkpoint_store.save_success(trajectory, patch, attempt)
                return trajectory, patch
            except Exception as exc:
                client.library.rollback(initial_library)
                retryable = _is_retryable_infrastructure_failure(exc)
                final = attempt > self._max_retry or not retryable
                self._checkpoint_store.save_failure(
                    worker_id=wid,
                    round_idx=round_idx,
                    task_id=task.task_id,
                    attempt=attempt,
                    failure_reason=f"{type(exc).__name__}: {exc}",
                    final=final,
                    retryable=retryable,
                    failure_class=(
                        "infrastructure" if retryable else "non_retryable"
                    ),
                )
                if final:
                    raise
                logger.warning(
                    "Task retry: worker=%s task=%s attempt=%d/%d failure=%s",
                    wid, task.task_id, attempt, self._max_retry + 1, exc,
                )
        raise AssertionError("unreachable")

    def _record_capability_matrix(self, round_idx: int) -> None:
        """
        本轮结束后抓一份能力矩阵快照：
          - 存入 self._capability_history（CapabilityEvolutionTracker，Phase14，
            供 capability_summary 聚合计数使用）；
          - 存入 self._capability_matrix_recorder（CapabilityMatrixRecorder，
            Artifact Fidelity Hardening TASK1，供 capability_matrix.jsonl 逐
            cell 落盘使用，直接调用 CapabilityTracker.to_dict()，不重新计算）。

        不新建接口：直接复用 FederatedServer.current_capability 这个已有公开
        属性（server/evolution.py 未变动），不改变 Capability Matrix 任何状态
        转移逻辑。

        Artifact Fidelity Hardening TASK2：失败时的处理方式由
        self._strict_artifact_mode 控制——
          - True（默认，真实实验）：直接 raise ArtifactRecordingError，实验
            中止，避免论文结果被静默污染成「capability_matrix 实际缺失但
            实验正常跑完」的假象；
          - False（仅供 mock/调试场景显式开启）：保留此前「记 warning、
            不中止」的行为。
        """
        if self._disable_capability_matrix:
            return
        try:
            matrix = self._server.current_capability.to_capability_matrix(round_idx)
            self._capability_history.record(matrix)
            if self._capability_matrix_recorder is not None:
                self._capability_matrix_recorder.record(
                    round_idx, self._server.current_capability.to_dict(),
                )
        except Exception as exc:
            if self._strict_artifact_mode:
                raise ArtifactRecordingError("capability_matrix", round_idx, exc) from exc
            logger.warning(
                "Round %d: capability_history.record 失败"
                "（strict_artifact_mode=False，仅告警不中止实验）: %s",
                round_idx, exc,
            )

    def _export_capability_evolution_csv(self) -> None:
        """
        实验结束时导出 `self._capability_history`（CapabilityEvolutionTracker）
        的两份 CSV：全局四态计数（capability_evolution.csv）+ per-worker 细分
        （capability_evolution_per_worker.csv）。

        缺口背景：`_record_capability_matrix()` 每轮都会调用
        `self._capability_history.record(matrix)` 摄入数据，但此前从未有任何
        调用方真正调用过 `CapabilityEvolutionTracker.to_csv()`/
        `per_worker_to_csv()` 本身——历史数据在内存里越攒越多，却从未落盘，
        与本仓库此前发现过的"recorder 建好、喂了数据，但从未真正落盘/接入"
        （fusion_trace/memory_trace/transfer_trace）是同一类缺口。

        只在 output_dir 非空且历史非空时导出，不影响未传 output_dir 的旧
        调用方/已有测试（对应 `self._output_dir is None` 或
        `disable_capability_matrix=True` 导致历史为空的两种早退场景）。
        """
        if self._output_dir is None or not self._capability_history.history:
            return
        cap_evo_path = self._capability_history.to_csv(
            Path(self._output_dir) / "capability_evolution.csv"
        )
        logger.info("capability_evolution.csv 已落盘: %s", cap_evo_path)
        cap_evo_worker_path = self._capability_history.per_worker_to_csv(
            Path(self._output_dir) / "capability_evolution_per_worker.csv"
        )
        logger.info("capability_evolution_per_worker.csv 已落盘: %s", cap_evo_worker_path)

