"""
planner.py — Stage 1 演化规划器（EvolutionPlanner）

对应论文 Section 4.2.1: Stage 1 — Evolution Planning。

论文公式：
    P^t = (C^t, M^t, D^t)

其中：
    C^t  = 能力矩阵（CapabilityMatrix）
    M^t  = 两级记忆（HighLevelMemory + {LowLevelMemory}）
    D^t  = 演化指令集（{Directive}）

输入（论文原文）：
    'full patch set {(ρ_i, δ_i^t)} + description-level digest of every
     client's pre-task library'

注意：Stage1 只接收 LibraryDigest（技能名+描述），
     不接收完整 SKILL 文件内容（信息最小化原则）。

本类只负责三件事：拼装/调用 Stage1PromptBuilder 生成的提示词、调用服务器
backbone LLM、解析 LLM 返回的 JSON 为 EvolutionPlan。能力矩阵维护、记忆更新
的具体规则见 server/capability.py、server/memory.py，均为本项目独立实现，
不包含从官方源码复制的算法逻辑。
"""

from __future__ import annotations

import logging
from typing import Any

from core.datatypes import (
    CapabilityMatrix,
    Directive,
    EvolutionPlan,
    HighLevelMemory,
    LibraryDigest,
    LowLevelMemory,
    parse_merge_action,
    WorkerPatch,
    WorkerProfile,
)
from core.exceptions import LLMCallError, ServerPlanningError
from evaluation.cost_accounting import CostAccountant
from evaluation.integrity_logs import ExecutionTraceRecorder, InvalidActionRecorder
from evaluation.memory_trace import MemoryTraceRecorder
from llm.backbone import LLMBackbone
from server.capability import CapabilityTracker
from server.memory import EvolutionMemoryStore
from server.prompt_builder import Stage1PromptBuilder

logger = logging.getLogger(__name__)


class EvolutionPlanner:
    """
    Stage 1 演化规划器，使用服务器 backbone 执行一次 LLM 调用，
    产出 EvolutionPlan P^t。

    Args:
        server_backbone: 服务器端 LLM（如 glm-5 或 claude-opus），
                         不同于任何 worker 的 backbone
        prompt_builder:  Stage1PromptBuilder；None → 使用默认实例
    """

    def __init__(
        self,
        server_backbone: LLMBackbone,
        prompt_builder: Stage1PromptBuilder | None = None,
        cost_recorder: CostAccountant | None = None,
        invalid_action_recorder: InvalidActionRecorder | None = None,
        trace_recorder: ExecutionTraceRecorder | None = None,
        memory_trace_recorder: MemoryTraceRecorder | None = None,
    ) -> None:
        self._backbone = server_backbone
        self._prompt_builder = prompt_builder or Stage1PromptBuilder()
        self._cost_recorder = cost_recorder
        # Experiment Integrity Hardening TASK2：未知 action 不再静默默认为
        # ABSORB，而是拒绝该 directive 并记录到 invalid_action.log。None（默认）
        # 时仅记 warning 日志，不落盘。
        self._invalid_action_recorder = invalid_action_recorder
        # Experiment Integrity Hardening TASK4：experiment_execution_trace.jsonl
        # 的旁路记录器，None（默认）时零行为变化。
        self._trace_recorder = trace_recorder
        # Full Reproduction Alignment Audit TASK5（Two-Level Memory
        # Alignment）：旁路记录 Stage1 对 high-level memory 的读取+更新。
        # None（默认）时零行为变化。
        self._memory_trace_recorder = memory_trace_recorder

    def set_cost_recorder(self, cost_recorder: CostAccountant | None) -> None:
        """设置/替换成本审计器（Appendix C 成本复现审计新增，TASK4）。
        None（默认）时零行为变化。"""
        self._cost_recorder = cost_recorder

    def set_invalid_action_recorder(self, recorder: InvalidActionRecorder | None) -> None:
        """设置/替换非法 action 记录器（Experiment Integrity Hardening TASK2）。"""
        self._invalid_action_recorder = recorder

    def set_trace_recorder(self, recorder: ExecutionTraceRecorder | None) -> None:
        """设置/替换执行轨迹记录器（Experiment Integrity Hardening TASK4）。"""
        self._trace_recorder = recorder

    def set_memory_trace_recorder(self, recorder: MemoryTraceRecorder | None) -> None:
        """设置/替换两级记忆访问追踪器（TASK5：FederatedServer.set_memory_trace_recorder 转发到此）。"""
        self._memory_trace_recorder = recorder

    def plan(
        self,
        round_idx: int,
        family_name: str,
        patches: dict[str, WorkerPatch],
        library_digests: dict[str, list[LibraryDigest]],
        capability_tracker: CapabilityTracker,
        memory_store: EvolutionMemoryStore,
        worker_profiles: dict[str, WorkerProfile],
    ) -> EvolutionPlan:
        """
        执行 Stage1 规划，返回 EvolutionPlan P^t。

        Args:
            round_idx:         当前 round 序号
            family_name:       任务族名称
            patches:           {worker_id: WorkerPatch}，本轮所有 worker 的 patch
            library_digests:   {worker_id: [LibraryDigest]}（描述级摘要）
            capability_tracker: 当前能力矩阵追踪器
            memory_store:      演化记忆存储器
            worker_profiles:   {worker_id: WorkerProfile}

        Returns:
            EvolutionPlan P^t

        Raises:
            ServerPlanningError: LLM 调用失败且无法降级处理
        """
        logger.info(
            "Stage1 规划开始: round=%d family=%s workers=%d patches=%d",
            round_idx, family_name, len(worker_profiles), len(patches),
        )

        # 构建提示词
        # Full Reproduction Alignment Audit TASK5（Two-Level Memory
        # Alignment）：记录一次 high-level memory 读取事件——证明该段文本
        # 确实被传入了本次 Stage1 prompt（_section_memory()）。只取已有数据，
        # 不影响任何决策。
        if self._memory_trace_recorder is not None:
            self._memory_trace_recorder.record_read(
                round_idx=round_idx, family_id=family_name, stage="stage1_planning",
                memory_level="high_level", worker_id=None,
                content=memory_store.high_level.content,
            )
        system_prompt, user_prompt = self._prompt_builder.build(
            round_idx=round_idx,
            family_name=family_name,
            patches=patches,
            library_digests=library_digests,
            capability_tracker=capability_tracker,
            memory_store=memory_store,
            worker_profiles=worker_profiles,
        )

        # 调用服务器 backbone
        try:
            raw_dict, call_result = self._backbone.call_json(user_prompt, system_prompt)
            logger.info(
                "Stage1 LLM 返回: tokens=%d cost=%.4f",
                call_result.total_tokens, call_result.cost_usd,
            )
            # Appendix C 成本复现审计（TASK4）：此前 call_result.cost_usd 只写进
            # info log 就丢弃了（EvolutionPlan 无 cost 字段承载），这里补一条
            # 只读记录，不影响规划结果。
            if self._cost_recorder is not None:
                self._cost_recorder.record_call(
                    component="stage1_planner",
                    usd_cost=call_result.cost_usd,
                    tokens_input=call_result.prompt_tokens,
                    tokens_output=call_result.completion_tokens,
                    round_idx=round_idx,
                    family_id=family_name,
                )
        except LLMCallError as exc:
            logger.error("Stage1 LLM 调用失败: %s", exc)
            # 降级：返回空规划（矩阵和记忆不变，无新指令）
            if self._trace_recorder is not None:
                self._trace_recorder.record_stage1(
                    llm_called=True, plan_generated=False, fallback_used=True,
                )
            return self._fallback_plan(
                round_idx, family_name, capability_tracker, memory_store, worker_profiles
            )

        # 解析并验证结果
        try:
            plan = self._parse_plan(
                raw_dict, round_idx, family_name, capability_tracker, memory_store,
                worker_profiles, call_result.cost_usd,
            )
        except Exception as exc:
            logger.error("Stage1 解析失败: %s\n原始输出: %s", exc, str(raw_dict)[:500])
            if self._trace_recorder is not None:
                self._trace_recorder.record_stage1(
                    llm_called=True, plan_generated=False, fallback_used=True,
                )
            return self._fallback_plan(
                round_idx, family_name, capability_tracker, memory_store, worker_profiles
            )

        logger.info(
            "Stage1 完成: directives=%d workflows=%d",
            len(plan.directives),
            len(plan.capability_matrix.cells),
        )
        if self._trace_recorder is not None:
            self._trace_recorder.record_stage1(
                llm_called=True, plan_generated=True, fallback_used=False,
            )
        return plan

    # ------------------------------------------------------------------
    # 解析 LLM 输出
    # ------------------------------------------------------------------

    def _parse_plan(
        self,
        raw: dict[str, Any],
        round_idx: int,
        family_name: str,
        capability_tracker: CapabilityTracker,
        memory_store: EvolutionMemoryStore,
        worker_profiles: dict[str, WorkerProfile],
        cost_usd: float,
    ) -> EvolutionPlan:
        """将 Stage1 LLM 的 JSON 输出解析为 EvolutionPlan。"""

        # 1. 能力矩阵
        matrix_dict: dict = raw.get("capability_matrix") or {}
        if isinstance(matrix_dict, dict):
            capability_tracker.update_from_plan_dict(matrix_dict, round_idx)
        capability_matrix = capability_tracker.to_capability_matrix(round_idx)

        # 2. 记忆更新
        high_content = str(raw.get("high_level_memory") or memory_store.high_level.content)
        low_updates: dict[str, str] = {}
        raw_low = raw.get("low_level_memories") or {}
        if isinstance(raw_low, dict):
            for wid, content in raw_low.items():
                if isinstance(content, str):
                    low_updates[wid] = content
        memory_store.apply_plan_memory_update(high_content, low_updates, round_idx)
        # Full Reproduction Alignment Audit TASK5：上面这行确实完成了
        # high-level（以及 Stage1 也可写的 low-level）记忆更新，这里补一条
        # 只读审计记录（与 plan() 里的 record_read() 配对，形成"读 -> 用于
        # 本轮决策 -> 更新"的完整可审计闭环）。
        if self._memory_trace_recorder is not None:
            self._memory_trace_recorder.record_update(
                round_idx=round_idx, family_id=family_name, stage="stage1_planning",
                memory_level="high_level", worker_id=None, new_content=high_content,
            )

        high_mem = HighLevelMemory(
            family_name=family_name,
            content=high_content,
            last_updated_round=round_idx,
        )
        low_mems: dict[str, LowLevelMemory] = {}
        for wid, profile in worker_profiles.items():
            mem = memory_store.get_worker_memory(wid)
            low_mems[wid] = mem or LowLevelMemory(
                worker_id=wid,
                backbone_model=profile.backbone_model,
                agent_harness=profile.agent_harness,
                content="",
            )

        # 3. 指令列表
        directives = self._parse_directives(raw.get("directives") or [], round_idx, family_name)

        return EvolutionPlan(
            round_idx=round_idx,
            family_name=family_name,
            capability_matrix=capability_matrix,
            high_level_memory=high_mem,
            low_level_memories=low_mems,
            directives=directives,
        )

    def _parse_directives(
        self, raw_directives: list, round_idx: int, family_name: str,
    ) -> list[Directive]:
        """将 JSON 列表解析为 Directive 列表，跳过格式错误项。

        Experiment Integrity Hardening TASK2：无法识别的 action 字符串不再
        静默默认为 ABSORB——直接拒绝该条 directive（不加入返回列表），并
        记录到 invalid_action.log（不改变合法动作的解析行为）。
        """
        directives: list[Directive] = []
        for item in raw_directives:
            if not isinstance(item, dict):
                continue
            action_str = str(item.get("action", "keep")).lower()
            try:
                action = parse_merge_action(action_str)
            except ValueError as exc:
                target = item.get("target_worker_id")
                if self._invalid_action_recorder is not None:
                    self._invalid_action_recorder.record(
                        round_idx=round_idx, family_name=family_name,
                        target_worker_id=target, raw_action=item.get("action"),
                        error=exc,
                    )
                else:
                    logger.warning(
                        "Stage1 directive 包含无法识别的 action=%r（target=%r），"
                        "已拒绝该 directive（不再静默默认为 ABSORB）",
                        item.get("action"), target,
                    )
                continue  # 拒绝该 directive，不再默认为 ABSORB

            try:
                directives.append(Directive(
                    target_worker_id=str(item.get("target_worker_id", "")),
                    workflow_name=str(item.get("workflow_name", "")),
                    action=action,
                    priority=int(item.get("priority", 1)),
                    reason=str(item.get("reason", "")),
                    source_worker_id=item.get("source_worker_id") or None,
                    source_reward=float(item["source_reward"])
                    if item.get("source_reward") is not None else None,
                ))
            except Exception:
                continue
        return directives

    def _fallback_plan(
        self,
        round_idx: int,
        family_name: str,
        capability_tracker: CapabilityTracker,
        memory_store: EvolutionMemoryStore,
        worker_profiles: dict[str, WorkerProfile],
    ) -> EvolutionPlan:
        """LLM 失败时返回空规划（保持现状，无指令）。"""
        capability_matrix = capability_tracker.to_capability_matrix(round_idx)
        low_mems: dict[str, LowLevelMemory] = {}
        for wid, profile in worker_profiles.items():
            mem = memory_store.get_worker_memory(wid)
            low_mems[wid] = mem or LowLevelMemory(
                worker_id=wid,
                backbone_model=profile.backbone_model,
                agent_harness=profile.agent_harness,
                content="",
            )
        return EvolutionPlan(
            round_idx=round_idx,
            family_name=family_name,
            capability_matrix=capability_matrix,
            high_level_memory=memory_store.high_level,
            low_level_memories=low_mems,
            directives=[],  # 无指令：Stage2 维持现状
        )
