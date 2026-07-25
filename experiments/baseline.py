"""
baseline.py — Self-Evolution Runner（Setting 1：孤立自进化基线）

对应论文 Section 5.1 Implementation：
    "We establish isolated agent self-evolution as our non-collaborative baseline,
     wherein each client independently evolves its skill library relying solely on
     its own execution trajectories, without the influence of other clients."

每个 client 独立运行：
  1. 执行 task → Trajectory
  2. 蒸馏 Patch → WorkerPatch
  3. 直接 apply 到自己的库（无服务端）

与 FederatedRunner 的区别：
  - 无 FederatedServer
  - 应用的是 WorkerPatch（而非 server 返回的 MergedPatch）
  - 技能库只靠自身经验增长
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from benchmark.sampler import TaskSampler
from benchmark.task import Task
from client.executor import TaskExecutor
from client.federated_client import FederatedClient
from core.datatypes import WorkerPatch
from core.exceptions import LLMCallError, PatchDistillationFailure, TaskExecutionError
from evaluation.cost_accounting import CostAccountant
from evaluation.selr import compute_selr_from_texts
from evaluation.evaluator import ExperimentEvaluator, ExperimentResult
from evaluation.integrity_logs import DistillationFailureRecorder, ExecutionTraceRecorder
from executor.environment import TrialIsolationSpec, isolated_task_trial
from evaluation.metrics import TrialSnapshot
from evaluation.reporter import ResultReporter
from experiments.task_checkpoint import TaskCheckpointStore
from llm.router import BackboneRouter

logger = logging.getLogger(__name__)


def _is_retryable_infrastructure_failure(exc: Exception) -> bool:
    """仅识别 API 不可用或 agent 开始前的进程/连接故障。"""
    message = str(exc).lower()
    if any(token in message for token in ("timeout", "timed_out", "wall_clock")):
        return False
    infrastructure_markers = (
        "api unavailable", "service unavailable", "connection refused",
        "connection reset", "connection aborted", "network unreachable",
        "temporary failure in name resolution", "bad gateway",
    )
    if any(marker in message for marker in infrastructure_markers):
        return True
    if isinstance(exc, OSError):
        return True
    if isinstance(exc, TaskExecutionError):
        return "returncode=" in message and "missing_success_marker" not in message
    if isinstance(exc, LLMCallError):
        return any(marker in message for marker in infrastructure_markers)
    return False


class SelfEvolutionRunner:
    """
    Setting 1: 孤立自进化（SE 基线）。

    论文中 SE baseline 的完整实现：
      - 每个 client 独立执行任务、蒸馏 patch、更新自己的库
      - 没有服务器参与，没有跨 client 信息共享
      - 作为 FederatedSkill 的对比基线（Table 1 "SE" 列）

    Args:
        clients:       参与实验的 FederatedClient 列表
        executor:      具备 .run(task, library, profile, round_idx) 接口的执行器
            （duck typing）。真实入口传入的是
            executor/router_executor.py::VerificationAwareExecutor（Phase16
            路由链路审计已确认），本类不关心具体是哪个实现。
        sampler:       任务采样器
        rounds:        联邦轮数 T
        setting_name:  实验设置名（用于日志/报告）
        reporter:      ResultReporter；None → 使用默认实例
        distillation_failure_mode: "strict"（默认）或 "audit"。
            Experiment Integrity Hardening TASK1： client.distill_patch()
            在 LLM 调用失败时会抛出 PatchDistillationFailure。strict 模式下
            直接向上抛出（终止实验，防止污染论文结果）；audit 模式下
            记录失败并以“本轮不更新库”的方式继续（仅用于调试/对比，
            不得用于正式论文结果）。
        output_dir:    非 None 时启用 distillation_failed.csv /
            experiment_execution_trace.jsonl 的记录与落盘（Experiment
            Integrity Hardening TASK1/TASK4），以及 cost_ledger.jsonl 的记录
            与落盘（FederatedSkill Cost Accounting Consistency Fix TASK1）。
            None（默认）时不创建任何记录器，零行为变化。
    """

    def __init__(
        self,
        clients: list[FederatedClient],
        executor: TaskExecutor,
        sampler: TaskSampler,
        rounds: int = 8,
        setting_name: str = "SE_Self_Evolution",
        reporter: ResultReporter | None = None,
        disable_patch_distillation: bool = False,
        distillation_failure_mode: str = "strict",
        output_dir: Path | str | None = None,
        max_retry: int = 0,
    ) -> None:
        self._clients = clients
        self._executor = executor
        self._sampler = sampler
        self._rounds = rounds
        self._setting_name = setting_name
        self._reporter = reporter or ResultReporter()
        self._evaluator = ExperimentEvaluator(setting_name=setting_name)
        self._disable_patch_distillation = disable_patch_distillation
        if disable_patch_distillation:
            logger.info("[Ablation A3] disable_patch_distillation=True")

        # Experiment Integrity Hardening TASK1
        if distillation_failure_mode not in ("strict", "audit"):
            raise ValueError(
                f"distillation_failure_mode 必须为 'strict' 或 'audit'，收到: "
                f"{distillation_failure_mode!r}"
            )
        self._distillation_failure_mode = distillation_failure_mode

        # Experiment Integrity Hardening TASK1/TASK4：仅在提供 output_dir 时
        # 创建记录器，None（默认）时零行为变化。
        self._output_dir = Path(output_dir) if output_dir is not None else None
        if max_retry < 0:
            raise ValueError("max_retry 必须 >= 0")
        self._max_retry = max_retry
        self._checkpoint_store = TaskCheckpointStore(self._output_dir)
        self._distillation_failure_recorder = (
            DistillationFailureRecorder() if self._output_dir is not None else None
        )
        # FederatedSkill Cost Accounting Consistency Fix TASK3：Setting1（Self-
        # Evolution）没有 server，只会用到 client_execution/patch_distiller 两个
        # component，从不写入 stage1_planner/stage2_merge——setting_type
        # 标注写入 experiment_execution_trace.jsonl 的每一轮记录，供审计脚本
        # 区分"该 setting 结构上不存在这个环节"与"数据缺失"。
        self._execution_trace = (
            ExecutionTraceRecorder(setting_type="self_evolution")
            if self._output_dir is not None else None
        )
        # FederatedSkill Cost Accounting Consistency Fix TASK1：与
        # execution_trace/distillation_failure_recorder 完全对等的旁路审计
        # 消费者，仅在 output_dir 非空时创建，向后兼容旧调用方/已有测试
        # （不传 output_dir 时 self._cost_accountant 为 None，零行为变化）。
        self._cost_accountant = (
            CostAccountant() if self._output_dir is not None else None
        )
        if self._cost_accountant is not None:
            # PatchDistiller 已支持 set_cost_recorder()
            # （client/distiller.py），FederatedClient.set_cost_recorder() 转发
            # 给内部 distiller——与 experiments/federated.py 完全同构的接入
            # 方式，PatchDistiller 自身无需任何改动。
            for client in self._clients:
                client.set_cost_recorder(self._cost_accountant)

    def run(self) -> ExperimentResult:
        """
        运行完整的 SE 实验，返回 ExperimentResult。

        每轮流程（对应 Algorithm 1 客户端部分，去掉服务器）：
          for each client i:
              τ_i ← Execute(L_i^t, ρ_i, x ∼ D_i)    Section 4.1.1
              δ_i^t ← g_i(L_i^t, τ_i, ρ_i)           Section 4.1.2 Eq.(2)
              L_i^{t+1} ← Apply(L_i^t, δ_i^t)         直接 apply（无 Stage2）
        """
        logger.info("SelfEvolutionRunner 开始: %d 轮, %d 个 clients",
                    self._rounds, len(self._clients))
        t_exp_start = time.monotonic()

        for round_idx in range(self._rounds):
            snapshots = self._run_round(round_idx)
            round_result = self._evaluator.record_round(round_idx, snapshots)
            # FederatedSkill Cost Accounting Consistency Fix TASK4：CLI/报告
            # 输出的 Total Cost 统一改为读取 CostAccountant 的真实累积成本，
            # 不再读取旧的 TrialSnapshot.cost_usd 求和（CLI harness 模式下
            # 该值恒为 0，会让真实调用的费用被静默吞掉）。output_dir 未提供
            # （self._cost_accountant 为 None）时不覆盖，保持旧行为不变。
            if self._cost_accountant is not None:
                round_result.metrics["total_cost_usd"] = self._cost_accountant.total_cost(round_idx)
            self._reporter.print_round(round_result)

        result = self._evaluator.finalize()
        if self._cost_accountant is not None:
            # 同上，全程汇总的 Total Cost 也统一改为读取 CostAccountant，而
            # 不是 finalize() 内部对 TrialSnapshot.cost_usd 的旧式求和。
            result.final_metrics["total_cost_usd"] = self._cost_accountant.total_cost_usd
        elapsed = time.monotonic() - t_exp_start
        result.metadata["elapsed_seconds"] = elapsed
        self._reporter.print_summary(result)
        logger.info("SelfEvolutionRunner 结束: 耗时 %.1fs", elapsed)

        # Experiment Integrity Hardening TASK1/TASK4：实验结束时落盘（若启用）
        if self._output_dir is not None:
            if self._distillation_failure_recorder is not None:
                self._distillation_failure_recorder.flush(self._output_dir)
            if self._execution_trace is not None:
                self._execution_trace.flush(self._output_dir)
            # FederatedSkill Cost Accounting Consistency Fix TASK1：落盘
            # cost_ledger.jsonl，component 只会出现 client_execution/
            # patch_distiller（Setting1 无 server，不会生成
            # stage1_planner/stage2_merge 的虚假记录）。
            if self._cost_accountant is not None:
                cost_path = self._cost_accountant.flush(self._output_dir)
                logger.info("cost_ledger.jsonl 已写入: %s", cost_path)
        return result

    def _run_round(self, round_idx: int) -> list[TrialSnapshot]:
        """执行单 round，返回所有 client 的 TrialSnapshot。"""
        if self._execution_trace is not None:
            self._execution_trace.start_round(round_idx, family_id=self._setting_name)

        snapshots: list[TrialSnapshot] = []
        worker_ids = [c.worker_id for c in self._clients]

        # 批量采样（每个 client 得到一个 task）
        assignments = self._sampler.sample_batch(worker_ids, round_idx)

        for client in self._clients:
            task = assignments[client.worker_id]
            snap = self._run_client_trial(client, task, round_idx)
            snapshots.append(snap)

        if self._execution_trace is not None:
            self._execution_trace.finish_round()

        return snapshots

    def _run_client_trial(
        self,
        client: FederatedClient,
        task: Task,
        round_idx: int,
    ) -> TrialSnapshot:
        """运行 task；失败 attempt 回滚 library，最多额外重试 max_retry 次。

        [Runtime Protocol Alignment Issue2] 官方 SkillFlow harbor 运行记录
        （paper_logs/*/config.json::retry）为 max_retries=0，且显式把
        AgentTimeoutError 等排除在可重试异常之外——即 agent 执行/超时失败
        不重试，直接记为该 task 失败（reward=0），继续下一个 task，不中断
        整个 family。max_retry 默认值已改为 0。
        耗尽 max_retry 后，"任务执行层"失败（不再向上 raise，而是返回 reward=0 的
        TrialSnapshot；PatchDistillationFailure 的 strict 模式行为保持不变
        。
        """
        initial_library = client.library.snapshot(round_idx)
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retry + 2):
            try:
                snapshot, trajectory, patch = self._run_client_trial_attempt(
                    client, task, round_idx, attempt
                )
                self._checkpoint_store.save_success(trajectory, patch, attempt)
                return snapshot
            except PatchDistillationFailure:
                # 蒸馏完整性保护（Experiment Integrity Hardening TASK1）：
                # 不属于本次 retry-policy 调整范畴，strict 模式必须终止实验。
                client.library.rollback(initial_library)
                raise
            except Exception as exc:
                client.library.rollback(initial_library)
                last_exc = exc
                retryable = _is_retryable_infrastructure_failure(exc)
                final = attempt > self._max_retry or not retryable
                self._checkpoint_store.save_failure(
                    worker_id=client.worker_id,
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
                    break
                logger.warning(
                    "Task retry: worker=%s task=%s attempt=%d/%d failure=%s",
                    client.worker_id, task.task_id, attempt,
                    self._max_retry + 1, exc,
                )

        # [Runtime Protocol Alignment Issue2] 耗尽 max_retry（默认 0，即不
        # 重试）后仍失败：记为 reward=0，不中断 round/family，继续下一个 task。
        logger.error(
            "Task failed（无剩余 retry）: worker=%s task=%s round=%d 记为 "
            "reward=0，继续下一个 task。failure=%s",
            client.worker_id, task.task_id, round_idx, last_exc,
        )
        lib_size = client.library.snapshot(round_idx).skill_count
        return TrialSnapshot(
            round_idx=round_idx,
            worker_id=client.worker_id,
            task_id=task.task_id,
            reward=0.0,
            library_size_before=lib_size,
            library_size_after=lib_size,
        )

    def _run_client_trial_attempt(
        self,
        client: FederatedClient,
        task: Task,
        round_idx: int,
        attempt: int,
    ) -> tuple[TrialSnapshot, Any, WorkerPatch]:
        """
        单次 attempt 的完整流程；异常由外层负责回滚和 retry。

        1. 记录库状态（before）
        2. 执行任务 → Trajectory
        3. 蒸馏 patch → WorkerPatch
        4. 直接 apply 到自己的库
        5. 记录库状态（after）
        """
        wid = client.worker_id
        lib_before = client.library.snapshot(round_idx).skill_count

        # Step 1: Trial Execution  (Section 4.1.1)
        artifact_dir = self._checkpoint_store.trial_artifact_dir(wid, task.task_id)
        if artifact_dir is None or task.metadata.get("source") != "skillflow_real":
            trajectory = self._executor.run(
                task=task,
                library=client.library,
                profile=client.profile,
                round_idx=round_idx,
            )
        else:
            isolation = TrialIsolationSpec(
                artifact_dir=artifact_dir,
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
        reward = trajectory.reward or 0.0
        traj_tokens = trajectory.total_tokens

        # FederatedSkill Cost Accounting Consistency Fix TASK1：记录
        # client_execution 这一环节真实 LLM 调用的成本，与
        # experiments/federated.py 中 client_execution 的记录方式完全一致
        # （Trajectory 只有聚合 total_tokens，无法拆分输入/输出，故作为
        # tokens_total_hint 传入，不伪造 tokens_input/tokens_output）。
        if self._cost_accountant is not None:
            self._cost_accountant.record_call(
                component="client_execution",
                usd_cost=trajectory.cost_usd,
                tokens_total_hint=traj_tokens,
                worker_id=wid,
                round_idx=round_idx,
                task_id=task.task_id,
            )

        # Step 2: Patch Distillation  (Section 4.1.2)
        # A3 ablation: 跳过蒸馏，将轨迹摘要直接当作 patch
        if self._disable_patch_distillation:
            patch = WorkerPatch(
                worker_id=wid,
                upserts={},
                deletions=[],
                reward=reward,
                summary=trajectory.final_message[:500] if trajectory.final_message else "",
            )
            logger.debug("[Ablation A3] 跳过蒸馏，使用 trajectory summary as patch")
            if self._execution_trace is not None:
                self._execution_trace.record_distillation(
                    worker_id=wid, llm_called=False, patch_generated=True,
                )
        else:
            # Experiment Integrity Hardening TASK1：LLM 调用失败时
            # client.distill_patch() 会抛出 PatchDistillationFailure（不再
            # 静默返回空 patch）。strict 模式直接重抛终止实验；audit
            # 模式仅在显式提供 output_dir 时可用，用于调试/对比，不得
            # 用于正式论文结果。
            try:
                patch = client.distill_patch(trajectory)
            except PatchDistillationFailure as exc:
                if self._distillation_failure_mode == "strict" or self._output_dir is None:
                    raise
                logger.error("[audit 模式] 蒸馏失败，本轮 worker=%s 库不更新: %s", wid, exc)
                if self._distillation_failure_recorder is not None:
                    self._distillation_failure_recorder.record(
                        setting=self._setting_name, family_id=self._setting_name,
                        round_idx=round_idx, worker_id=wid, reason=str(exc),
                    )
                if self._execution_trace is not None:
                    self._execution_trace.record_distillation(
                        worker_id=wid, llm_called=True, patch_generated=False,
                    )
                patch = WorkerPatch(
                    worker_id=wid, upserts={}, deletions=[], reward=reward,
                    summary="[audit 模式] 蒸馏失败，本轮无 patch",
                )
            else:
                if self._execution_trace is not None:
                    self._execution_trace.record_distillation(
                        worker_id=wid, llm_called=True, patch_generated=True,
                    )
        patch_tokens = sum(len(v) for v in patch.upserts.values()) // 4  # chars→tokens 估算

        # Phase14 新增：基于真实文本计算 SELR（论文 Appendix E Eq.5），与
        # federated.py 保持一致的计算方式。
        trajectory_text = "\n".join(step.content for step in trajectory.steps) + "\n" + (
            trajectory.final_message or ""
        )
        patch_text = "\n".join(patch.upserts.values())
        selr_info = compute_selr_from_texts(trajectory_text, patch_text)

        # Step 3: Apply patch (SE 直接用自己的 patch，无 server merge)
        # SkillLibrary.apply_patch 同时支持 WorkerPatch 和 MergedPatch
        client.library.apply_patch(patch)

        lib_after = client.library.snapshot(round_idx).skill_count

        logger.debug(
            "SE Round %d worker=%s task=%s reward=%.1f lib:%d→%d",
            round_idx, wid, task.task_id, reward, lib_before, lib_after,
        )
        snapshot = TrialSnapshot(
            round_idx=round_idx,
            worker_id=wid,
            task_id=task.task_id,
            reward=reward,
            trajectory_tokens=traj_tokens,
            patch_tokens=patch_tokens,
            library_size_before=lib_before,
            library_size_after=lib_after,
            cost_usd=trajectory.cost_usd,
            selr=selr_info["selr"],
            n_sensitive_entities=selr_info["n_sensitive"],
            n_leaked_entities=selr_info["n_leaked"],
        )
        return snapshot, trajectory, patch
