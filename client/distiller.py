"""
distiller.py — 客户端 Patch 蒸馏器（PatchDistiller）

实现论文 Section 4.1.2 的核心公式：

    δ_i^t = g_i(L_i^t, B_i^t, ρ_i)

其中：
    g_i   = PatchDistiller.distill()
    L_i^t = SkillLibrary（当前技能库）
    B_i^t = Trajectory  （原始执行轨迹，MUST NOT 离开客户端）
    ρ_i   = WorkerProfile（worker 静态配置）
    δ_i^t = WorkerPatch  （上传给服务端的四元组）

与原版的区别：
  原版（patcher_bridge.PatcherBridge）：
    - 包装 SkillFlow 的 SkillPatchEvolver（外部库，不透明）
    - patch = evolver.generate_patch(compacted_traj, snapshot_dict)
  本版（PatchDistiller）：
    - 完全自主的 7 步流水线，每步可单独测试
    - BackboneRouter 解耦「调哪个模型」和「怎么调」
    - 独立的 _validate_patch() 步骤，安全检查更显式
    - 隐私审计 _audit_privacy() 主动检测任务特定数据泄露风险
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.constants import K_OBS, K_STEP
from core.datatypes import (
    CompactedTrajectory,
    LibrarySnapshot,
    Trajectory,
    TrialOutcome,
    WorkerPatch,
    WorkerProfile,
    validate_safe_rel_path,
)
from core.exceptions import (
    LLMCallError,
    PatchDistillationError,
    PatchDistillationFailure,
    PatchValidationError,
)
from client.library import SkillLibrary
from client.trajectory import TrajectoryCompressor
from evaluation.cost_accounting import CostAccountant
from llm.backbone import BackboneCallResult, LLMBackbone
from llm.prompt_builder import DistillerPromptBuilder
from llm.router import BackboneRouter

logger = logging.getLogger(__name__)


class PatchDistiller:
    """
    本地 Patch 蒸馏器，将 (B_i^t, L_i^t, ρ_i) → δ_i^t。

    论文 Section 4.1.2：
        'A local patcher, which shares the same backbone LLM as the execution
         LLM, converts the raw trial artifacts into the skill patch δ_i^t.'

    隐私保证：
        原始轨迹 B_i^t 永远不会出现在返回的 WorkerPatch 中。
        所有路径经过 validate_safe_rel_path() 检验。
        _audit_privacy() 对 upserts 内容做启发式泄漏检测。

    使用方式（单 worker）：
        distiller = PatchDistiller.from_profile(profile)
        patch = distiller.distill(trajectory, library)

    使用方式（多 worker，共用路由表）：
        router = BackboneRouter.from_profiles(profiles)
        distiller = PatchDistiller(router=router)
        patch = distiller.distill(trajectory, library, profile)
    """

    def __init__(
        self,
        router: BackboneRouter,
        prompt_builder: DistillerPromptBuilder | None = None,
        k_step: int = K_STEP,
        k_obs: int = K_OBS,
        cost_recorder: CostAccountant | None = None,
    ) -> None:
        """
        Args:
            router:         BackboneRouter，提供每个 worker 的 m_i backbone
            prompt_builder: DistillerPromptBuilder；None → 使用默认实例
            k_step:         轨迹压缩的最大步数（论文 K_step，默认 20）
            k_obs:          观察字符截断上限（论文 K_obs，默认 3000）
            cost_recorder:  可选，`evaluation/cost_accounting.py::CostAccountant`
                （Appendix C 成本复现审计新增）。提供时，Step5 每次调用自己
                backbone 蒸馏 patch 完成后，会记一条
                `component="patch_distiller"` 的 LLMCallCostRecord（此前该次
                调用的 cost/tokens 算出来后直接丢弃，从未被任何统计结构读取）。
                为 None（默认）时零行为变化，向后兼容旧调用方/已有测试。也可
                以之后用 `set_cost_recorder()` 补设。
        """
        self._router = router
        self._prompt_builder = prompt_builder or DistillerPromptBuilder()
        self._compressor = TrajectoryCompressor(k_step=k_step, k_obs=k_obs)
        self._cost_recorder = cost_recorder

    def set_cost_recorder(self, cost_recorder: CostAccountant | None) -> None:
        """设置/替换成本审计器（FederatedClient.set_cost_recorder 转发到此）。"""
        self._cost_recorder = cost_recorder

    # ------------------------------------------------------------------
    # 工厂：单 worker 快速构造
    # ------------------------------------------------------------------

    @classmethod
    def from_profile(
        cls,
        profile: WorkerProfile,
        k_step: int = K_STEP,
        k_obs: int = K_OBS,
    ) -> "PatchDistiller":
        """
        为单个 worker 快速构造 distiller（Setting 1 Self-Evolve baseline）。

        示例：
            profile = WorkerProfile(client_id="w0", backbone_model="qwen3.6-plus", ...)
            distiller = PatchDistiller.from_profile(profile)
            patch = distiller.distill(traj, library)
        """
        from llm.router import make_single_worker_router
        router = make_single_worker_router(profile.client_id, profile)
        return cls(router=router, k_step=k_step, k_obs=k_obs)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def distill(
        self,
        trajectory: Trajectory,
        library: SkillLibrary,
        profile: WorkerProfile | None = None,
    ) -> WorkerPatch:
        """
        完整蒸馏流水线：B_i^t + L_i^t + ρ_i → δ_i^t

        ⚠️  Privacy guarantee:
            trajectory（原始轨迹 B_i^t）的内容不会出现在返回的 WorkerPatch 中。
            只有蒸馏出的抽象化操作规程（skills）才会进入 upserts。

        Args:
            trajectory: 原始执行轨迹 B_i^t（本地保留，不上传）
            library:    当前技能库 L_i^t
            profile:    WorkerProfile ρ_i；None → 尝试从 router 按 trajectory.worker_id
                        反查已注册的 profile（见 BackboneRouter.register_profile()）

        Returns:
            WorkerPatch δ_i^t = (U_i^t, D_i^t, R_{i,x}(τ), s_i^t)

        Raises:
            ValueError: profile 未传入，且 router 中也没有为 trajectory.worker_id
                        注册过 profile（无法确定 patch 归属的 worker_id / 风格提示）。
        """
        # 未显式传入 profile 时，尝试从 router 按 trajectory.worker_id 反查
        # （router.register_profile() 由 BackboneRouter.from_profiles() 自动调用）
        if profile is None:
            profile = self._router.get_profile(trajectory.worker_id)
        if profile is None:
            raise ValueError(
                f"distill() 未收到 profile 参数，且 router 中也没有为 "
                f"worker_id={trajectory.worker_id!r} 注册过 WorkerProfile。"
                f"请显式传入 profile，或先调用 "
                f"router.register_profile(worker_id, profile)。"
            )

        worker_id = profile.client_id
        logger.info(
            "开始蒸馏: worker=%s task=%s round=%d",
            worker_id, trajectory.task_name, trajectory.round_idx,
        )

        # ── Step 1: 压缩轨迹 ──────────────────────────────────────────
        compacted = self._step1_compress(trajectory)

        # ── Step 2: 构建库快照 ────────────────────────────────────────
        snapshot = self._step2_snapshot(library, trajectory.round_idx)

        # ── Step 3: 提取试验结果 ──────────────────────────────────────
        outcome = self._step3_outcome(trajectory)

        # ── Step 4: 构建提示词 ────────────────────────────────────────
        system_prompt, user_prompt = self._step4_prompt(compacted, snapshot, outcome, profile)

        # ── Step 5: 调用 backbone LLM ────────────────────────────────
        raw_dict, llm_result = self._step5_call_llm(
            worker_id, system_prompt, user_prompt,
            round_idx=trajectory.round_idx, task_id=trajectory.task_name,
        )
        # Appendix C 成本复现审计（TASK4）：此次调用已经真实发生、结果已经拿到，
        # 这里只是追加一次只读记录，不影响 llm_result 本身或后续任何步骤。
        if self._cost_recorder is not None:
            self._cost_recorder.record_call(
                component="patch_distiller",
                usd_cost=llm_result.cost_usd,
                tokens_input=llm_result.prompt_tokens,
                tokens_output=llm_result.completion_tokens,
                worker_id=worker_id,
                round_idx=trajectory.round_idx,
            )

        # ── Step 6: 验证与净化 ────────────────────────────────────────
        validated = self._step6_validate(raw_dict, profile, trajectory.round_idx)

        # ── Step 7: 组装 WorkerPatch 四元组 ───────────────────────────
        patch = self._step7_build_patch(
            validated, outcome, profile, llm_result,
            round_idx=trajectory.round_idx
        )

        logger.info(
            "蒸馏完成: worker=%s upserts=%d deletions=%d reward=%.3f",
            worker_id, len(patch.upserts), len(patch.deletions), patch.reward,
        )
        return patch

    # ------------------------------------------------------------------
    # 七步流水线（每步独立可测试）
    # ------------------------------------------------------------------

    def _step1_compress(self, trajectory: Trajectory) -> CompactedTrajectory:
        """
        Step 1 — 论文 Section 4.1.2 ❶ Compacted Trajectory
        将原始轨迹压缩至 K_step 步，观察截断至 K_obs 字符。
        """
        logger.debug(
            "Step1 压缩轨迹: %d 步 → 最多 %d 步",
            len(trajectory.steps), self._compressor.k_step,
        )
        return self._compressor.compress(trajectory)

    def _step2_snapshot(
        self, library: SkillLibrary, round_idx: int
    ) -> LibrarySnapshot:
        """
        Step 2 — 论文 Section 4.1.2 ❷ Library Snapshot
        读取当前技能库所有文件，构建 JSON 快照。
        """
        snapshot = library.snapshot(round_idx=round_idx)
        logger.debug(
            "Step2 库快照: %d 个技能 / %d bytes",
            snapshot.skill_count, snapshot.total_size_bytes,
        )
        return snapshot

    def _step3_outcome(self, trajectory: Trajectory) -> TrialOutcome:
        """
        Step 3 — 论文 Section 4.1.2 ❸ Trial Outcome
        从 trajectory 提取：任务名、奖励信号、异常类型、最终消息、验证失败列表。
        """
        reward = float(trajectory.reward) if trajectory.reward is not None else 0.0
        exception_types: list[str] = []
        if trajectory.exception_info:
            et = trajectory.exception_info.get("exception_type")
            if et:
                exception_types.append(str(et))

        return TrialOutcome(
            task_name=trajectory.task_name,
            reward=reward,
            success=reward >= 1.0,
            exception_types=exception_types,
            final_agent_message=trajectory.final_message or "",
            verification_failures=list(trajectory.verifier_subtest_failures or []),
            runtime_seconds=trajectory.runtime_seconds,
            token_usage=trajectory.total_tokens,
            cost_usd=trajectory.cost_usd,
            verifier_feedback=(trajectory.verifier_output or "")[:1500],
            failure_reason=trajectory.failure_reason or "",
        )

    def _step4_prompt(
        self,
        compacted: CompactedTrajectory,
        snapshot: LibrarySnapshot,
        outcome: TrialOutcome,
        profile: WorkerProfile,
    ) -> tuple[str, str]:
        """
        Step 4 — 组装 (system_prompt, user_prompt)。
        """
        return self._prompt_builder.build(compacted, snapshot, outcome, profile)

    def _step5_call_llm(
        self,
        worker_id: str,
        system_prompt: str,
        user_prompt: str,
        round_idx: int,
        task_id: str,
    ) -> tuple[dict[str, Any], BackboneCallResult]:
        """
        Step 5 — 用 worker 自己的 backbone（m_i）调用 LLM。

        论文：
            'The patcher executes via a single LLM call utilizing the
             client's native backbone — the same model used for task execution.'

        Experiment Integrity Hardening TASK1（Patch Distillation Fail-Loud）：
        原实现在 LLMCallError 时返回空 patch dict、静默继续实验——这会让真实
        实验结果混入"未经蒸馏的空更新"而不报错。现改为直接抛出
        `PatchDistillationFailure`（携带 worker_id/round_idx/task_id/原始异常），
        不在本层做任何降级决定；是否停止实验（strict）还是跳过该 client 本轮
        并记录（audit）由 runner 层（`experiments/baseline.py`/
        `experiments/federated.py`）决定。
        """
        backbone: LLMBackbone = self._router.get(worker_id)
        logger.debug("Step5 调用 backbone: %s", backbone.litellm_model)

        try:
            raw_dict, llm_result = backbone.call_json(user_prompt, system_prompt)
            logger.debug(
                "Step5 LLM 返回: prompt_tokens=%d, completion_tokens=%d, cost=%.4f",
                llm_result.prompt_tokens, llm_result.completion_tokens, llm_result.cost_usd,
            )
            return raw_dict, llm_result
        except LLMCallError as exc:
            logger.error(
                "Step5 LLM 调用失败: worker=%s round=%d task=%s: %s",
                worker_id, round_idx, task_id, exc,
            )
            raise PatchDistillationFailure(
                worker_id=worker_id, round_idx=round_idx, task_id=task_id,
                original_error=exc,
            ) from exc

    def _step6_validate(
        self,
        raw: dict[str, Any],
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> dict[str, Any]:
        """
        Step 6 — 验证并净化 LLM 输出。

        安全检查（论文 Section 4.1.2）：
          - 统一字段名（兼容 upsert_files / upserts 两种 key 名）
          - 路径安全：拒绝绝对路径、目录穿越
          - 内容安全：过滤空内容、二进制占位符
          - 隐私审计：启发式检测任务特定数据泄露风险

        Returns:
            净化后的 dict，固定包含 upsert_files / delete_paths / summary
        """
        # -- 字段名归一化（兼容 SkillFlow 原版 upsert_files 和自定义 upserts）
        upserts_raw: dict = raw.get("upsert_files") or raw.get("upserts") or {}
        deletes_raw: list = raw.get("delete_paths") or raw.get("deletions") or []
        summary: str = str(raw.get("summary", ""))[:1000]
        # [Full Reproduction Alignment Audit TASK1] rationale 与 summary 是两个
        # 独立字段：rationale 是 LLM 对失败原因/修改理由的详细解释，summary 仍是
        # 一句话标签。LLM 未返回 rationale 时留空（向后兼容，不回退到 summary，
        # 避免把两者悄悄合并成同一份文本）。
        rationale: str = str(raw.get("rationale", ""))[:4000]

        if not isinstance(upserts_raw, dict):
            logger.warning("upsert_files 字段类型错误: %s，清空", type(upserts_raw).__name__)
            upserts_raw = {}
        if not isinstance(deletes_raw, list):
            logger.warning("delete_paths 字段类型错误: %s，清空", type(deletes_raw).__name__)
            deletes_raw = []

        # -- 路径安全验证
        safe_upserts: dict[str, str] = {}
        for path, content in upserts_raw.items():
            safe = validate_safe_rel_path(str(path))
            if safe is None:
                logger.warning("不安全的 upsert 路径已拒绝: %r", path)
                continue
            if not isinstance(content, str) or not content.strip() or content == "<binary>":
                logger.warning("空内容或二进制占位符已跳过: %r", path)
                continue
            safe_upserts[safe] = content

        safe_deletes: list[str] = []
        for path in deletes_raw:
            safe = validate_safe_rel_path(str(path))
            if safe is None:
                logger.warning("不安全的 delete 路径已拒绝: %r", path)
                continue
            safe_deletes.append(safe)

        # -- 隐私审计（启发式，只记录警告，不拒绝）
        self._audit_privacy(safe_upserts, profile)

        return {
            "upsert_files": safe_upserts,
            "delete_paths": safe_deletes,
            "summary": summary,
            "rationale": rationale,
            "_round_idx": round_idx,  # 内部传递，不写入 patch
        }

    def _step7_build_patch(
        self,
        validated: dict[str, Any],
        outcome: TrialOutcome,
        profile: WorkerProfile,
        llm_result: BackboneCallResult,
        round_idx: int = 0,
    ) -> WorkerPatch:
        """
        Step 7 — 组装最终的 WorkerPatch：worker_id + 四元组 (U_i^t, D_i^t, R_{i,x}(τ), s_i^t)。

        对应论文 Equation (4)：δ_i^t = (U_i^t, D_i^t, R_{i,x}(τ), s_i^t)；
        worker_id 字段对应 Appendix B.2 实际 patch manifest 中的 "worker_id" 字段。
        不携带 round_idx/timestamp/profile_hash 等未被论文作品证实的额外字段。
        """
        return WorkerPatch(
            worker_id=profile.client_id,
            upserts=validated["upsert_files"],   # U_i^t
            deletions=validated["delete_paths"], # D_i^t
            reward=outcome.reward,               # R_{i,x}(τ)
            summary=validated["summary"],        # s_i^t
            rationale=validated.get("rationale", ""),  # [TASK1] 独立于 reward/summary
        )

    # ------------------------------------------------------------------
    # 隐私审计（启发式）
    # ------------------------------------------------------------------

    # 可能暴露任务特定数据的正则模式
    _LEAK_PATTERNS: list[tuple[str, str]] = [
        (r"\b[A-Z]{2,6}-\d{4,}\b", "任务 ID 格式（如 TASK-12345）"),
        (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "IP 地址"),
        (r"(?i)\b(password|passwd|secret|token|api_key)\s*[:=]\s*\S+", "凭据/密钥"),
        (r"(?i)\b(confidential|internal only|top secret)\b", "敏感标记词"),
        (r"/(?:home|tmp|var|etc)/\S+", "绝对文件路径"),
    ]

    def _audit_privacy(
        self,
        upserts: dict[str, str],
        profile: WorkerProfile,
    ) -> None:
        """
        对 upserts 内容执行启发式隐私审计。
        只记录 WARNING 日志，不拒绝（防止误判正常代码中的正则匹配）。
        如需强制执行，可将 logger.warning 改为 raise PatchValidationError。
        """
        for path, content in upserts.items():
            for pattern, desc in self._LEAK_PATTERNS:
                if re.search(pattern, content):
                    logger.warning(
                        "隐私审计警告: %s/%s 中检测到可能的任务特定数据（%s），"
                        "请检查此内容是否为可泛化规程",
                        profile.client_id, path, desc,
                    )
                    break  # 每个文件只报告第一个匹配，避免刷屏
