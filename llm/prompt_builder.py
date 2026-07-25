"""
prompt_builder.py — 联邦技能演化全链路提示词构建

本文件自行设计提示词结构，不沿用 SkillFlow 的 SkillPatchEvolver 原版 prompt。
差异体现在：
  1. 显式分区（Trial Outcome / Trajectory / Library / Schema），方便消融测试
  2. 隐私约束段独立显示（对应论文 Section 4.1.2 privacy guarantee）
  3. 服务端提示词分 Stage1（规划）/ Stage2（演化）两段，与论文 P^t 结构对齐
  4. 支持 worker backbone 差异化措辞（qwen / glm / kimi 各有不同输出倾向）
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

from core.constants import MAX_LIBRARY_PROMPT_CHARS, MAX_TRAJECTORY_PROMPT_CHARS
from core.datatypes import (
    CompactedTrajectory,
    LibrarySnapshot,
    TrajectoryStep,
    TrialOutcome,
    WorkerProfile,
)
from prompts import load_prompt

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Patch 蒸馏提示词（客户端 PatchDistiller 使用）
# ---------------------------------------------------------------------------


class DistillerPromptBuilder:
    """
    构建 PatchDistiller 使用的 (system_prompt, user_prompt) 二元组。

    对应论文 Section 4.1.2：三项输入（压缩轨迹 + 技能库快照 + 试验结果）
    拼装为单次 LLM 调用的输入，输出 JSON 格式的 patch 4-tuple。
    """

    # JSON 输出的 schema 说明（嵌入 user_prompt 末尾）
    # [Full Reproduction Alignment Audit TASK1] 新增 "rationale" 字段，与
    # "summary" 明确区分：summary 是一句话标签，rationale 是对"为什么失败/
    # 为什么这样改"的详细因果解释；两者都不等价于 reward（reward 是独立的
    # 数值验证信号，只用于评估，不作为 rationale 的替代）。
    _OUTPUT_SCHEMA = textwrap.dedent("""\
    {
      "upsert_files": {
        "<能力目录>/SKILL.md":
          "---\\nname: <驼峰命名>\\ndescription: <一句话描述>\\n---\\n\\n# 工作流\\n...",
        "<能力目录>/scripts/<helper>.py": "# 辅助脚本..."
      },
      "delete_paths": ["<旧技能路径>"],
      "summary": "<一句话说明本次 patch 的意图>",
      "rationale": "<详细解释：为什么这次试验成功/失败，以及为什么这个 patch 能解决根本原因；不要只重复 reward 数值>"
    }""")

    def build(
        self,
        compacted: CompactedTrajectory,
        snapshot: LibrarySnapshot,
        outcome: TrialOutcome,
        profile: WorkerProfile,
    ) -> tuple[str, str]:
        """
        组装 (system_prompt, user_prompt)。

        Returns:
            (system_prompt, user_prompt)
        """
        system = self._build_system(profile)
        user = self._build_user(compacted, snapshot, outcome, profile)
        return system, user

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_system(self, profile: WorkerProfile) -> str:
        # 系统提示词来自 prompts/patch_prompt.txt。
        # 注意：与 Stage1/Stage2 不同，本提示词并非保留自官方文本——
        # 官方 patcher 依赖不可见的外部库 SkillFlow
        # （libs.skill_evolution.patcher.SkillPatchEvolver），其具体提示词
        # 本项目从未接触过，因此此处是完全自主设计（仅为保持与
        # Stage1/Stage2 一致的文件化结构而迁入 prompts/ 目录）。
        template = load_prompt("patch_prompt.txt")
        return template.format(
            client_id=profile.client_id,
            backbone_model=profile.backbone_model,
            agent_harness=profile.agent_harness,
        )

    # ------------------------------------------------------------------
    # User prompt
    # ------------------------------------------------------------------

    def _build_user(
        self,
        compacted: CompactedTrajectory,
        snapshot: LibrarySnapshot,
        outcome: TrialOutcome,
        profile: WorkerProfile,
    ) -> str:
        sections = [
            self._section_trial_outcome(outcome),
            self._section_trajectory(compacted),
            self._section_library(snapshot),
            self._section_instructions(outcome),
            self._section_output_schema(),
        ]
        return "\n\n".join(sections)

    def _section_trial_outcome(self, outcome: TrialOutcome) -> str:
        """
        Paper Section 4.1.2 step 3: Trial Outcome section.
        """
        success_str = "SUCCESS (reward = 1.0)" if outcome.success else f"FAILED (reward = {outcome.reward:.3f})"
        lines = [
            "## Trial Outcome",
            f"Task: {outcome.task_name}",
            f"Reward: {outcome.reward:.4f}  {success_str}",
        ]
        if outcome.exception_types:
            lines.append(f"Exception types: {', '.join(outcome.exception_types)}")
        if outcome.verification_failures:
            lines.append("Failed sub-tests:")
            for f in outcome.verification_failures[:10]:
                lines.append(f"  - {f}")
        # 官方对齐 Part5：failure_reason/verifier_feedback 是严格从 verifier
        # 输出/异常信号派生的诊断信号（core.datatypes.Trajectory._derive_failure_reason），
        # 独立于下面的 "Agent final output"（agent 自己的 chat 陈述）单独展示，
        # 且在 prompt 顺序上排在 chat 文本之前，引导 LLM 优先采信这里而不是
        # agent 的自我陈述（agent 完全可能声称成功但 verifier 并不认可）。
        if not outcome.success:
            if outcome.failure_reason:
                lines.append(f"Failure reason (derived strictly from verifier/exception signals, NOT from chat):\n{outcome.failure_reason}")
            if outcome.verifier_feedback:
                lines.append(f"Raw verifier feedback:\n{outcome.verifier_feedback}")
        if outcome.final_agent_message:
            trimmed = outcome.final_agent_message[:600]
            if len(outcome.final_agent_message) > 600:
                trimmed += " ...[truncated]"
            lines.append(
                "Agent final chat message (agent's own claim — may be WRONG, "
                f"e.g. agent may claim success while the verifier disagrees; "
                f"do NOT use this as the basis for diagnosing failure cause):\n{trimmed}"
            )
        return "\n".join(lines)

    def _section_trajectory(self, compacted: CompactedTrajectory) -> str:
        """
        Paper Section 4.1.2 step 1: Compacted Trajectory section.
        """
        header = (
            f"## Compacted Trajectory\n"
            f"{len(compacted.steps)} steps (original {compacted.original_step_count} steps, "
            f"K_step={compacted.k_step}, K_obs={compacted.k_obs})"
        )
        step_lines: list[str] = []
        for step in compacted.steps:
            step_lines.append(self._format_step(step))
        trajectory_text = "\n".join(step_lines)

        # 整体截断（防止 context 溢出）
        if len(trajectory_text) > MAX_TRAJECTORY_PROMPT_CHARS:
            trajectory_text = (
                trajectory_text[:MAX_TRAJECTORY_PROMPT_CHARS]
                + "\n...[trajectory truncated due to length]"
            )
        return f"{header}\n\n{trajectory_text}"

    def _section_library(self, snapshot: LibrarySnapshot) -> str:
        """
        Paper Section 4.1.2 step 2: Library Snapshot section.
        """
        header = (
            f"## Current Library Snapshot\n"
            f"Worker: {snapshot.worker_id}  |  Round: {snapshot.round_idx}  |  "
            f"Skills: {snapshot.skill_count}  |  Size: {snapshot.total_size_bytes:,} bytes"
        )
        if not snapshot.files:
            return header + "\n\n(Library is empty)"

        library_json = self._format_library_json(snapshot)
        return f"{header}\n\n```json\n{library_json}\n```"

    def _section_instructions(self, outcome: TrialOutcome) -> str:
        if outcome.success:
            strategy = (
                "Trial succeeded (reward=1.0). Extract the successful experience:\n"
                "  - Capture effective tool-call sequences and decision procedures\n"
                "  - If a similar skill already exists, consider supplementing or merging details\n"
                "  - Return an empty patch with a reason in summary if no update is needed"
            )
        else:
            strategy = (
                f"Trial failed (reward={outcome.reward:.3f}). Update the skill library to address the root cause:\n"
                "  - Base your diagnosis STRICTLY on the 'Failure reason' / 'Raw verifier feedback' lines above "
                "(derived from the real verifier output), NOT on the agent's own chat claim — the agent may "
                "claim success even when the verifier disagrees.\n"
                "  - If a relevant skill exists but uses a wrong approach, fix or replace it\n"
                "  - If no skill covers this scenario, add a new one\n"
                "  - If failure is due to environment issues (not model logic), return an empty patch\n"
                "  - The updated/new SKILL.md content MUST include three explicit sections addressing this "
                "failure: '## Failure Cause' (what verifiably went wrong, quoting the verifier feedback), "
                "'## Future Prevention Rule' (a concrete rule the agent must follow next time to avoid this "
                "failure class), and '## Verification Procedure' (a concrete, checkable step the agent must "
                "perform before declaring the task done, e.g. re-reading the required output file to confirm "
                "it exists and matches the expected schema — never rely on self-reported success)."
            )
        return f"## Instructions\n{strategy}"

    def _section_output_schema(self) -> str:
        return (
            "## Output Format (strict JSON, no extra text)\n\n"
            "```json\n"
            + self._OUTPUT_SCHEMA
            + "\n```\n\n"
            "Path rules:\n"
            "  - All paths relative to the skill library root (no leading '/', no '..')\n"
            "  - Skill directory names use English capability descriptions, no task IDs or serial numbers\n"
            "  - Empty patch is valid: {\"upsert_files\":{}, \"delete_paths\":[], \"summary\":\"...\", \"rationale\":\"...\"}\n"
            "  - `rationale` must explain causation (why this happened / why this fix works); "
            "do not just restate the reward number or duplicate `summary` verbatim"
        )

    # ------------------------------------------------------------------
    # 格式化辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _format_step(step: TrajectoryStep) -> str:
        """Format a TrajectoryStep as readable text."""
        parts = [f"[Step {step.step_index}] <{step.role.upper()}>"]
        if step.content:
            trimmed = step.content[:500]
            if len(step.content) > 500:
                trimmed += " ...[truncated]"
            parts.append(trimmed)
        if step.tool_calls:
            names = [
                tc.get("function", {}).get("name", "?")
                if isinstance(tc, dict) else "?"
                for tc in step.tool_calls[:3]
            ]
            parts.append(f"Tool calls: {', '.join(names)}")
        if step.observation:
            obs = step.observation
            parts.append(f"Observation: {obs}")
        return "\n".join(parts)

    @staticmethod
    def _format_library_json(snapshot: LibrarySnapshot) -> str:
        """
        将技能库格式化为 JSON。
        截断策略：优先保留所有 SKILL.md，其余文件按大小截断。
        """
        files_dict = snapshot.to_path_content_dict()
        skill_mds = {k: v for k, v in files_dict.items() if k.endswith("SKILL.md")}
        others = {k: v for k, v in files_dict.items() if not k.endswith("SKILL.md")}

        # 先放入所有 SKILL.md
        result: dict[str, str] = dict(skill_mds)
        current_size = len(json.dumps(result, ensure_ascii=False))

        # 再按剩余空间填入其他文件（截断超长内容）
        for path, content in sorted(others.items()):
            if current_size >= MAX_LIBRARY_PROMPT_CHARS:
                break
            truncated = content
            if len(content) > 400:
                truncated = content[:400] + " ...[截断]"
            result[path] = truncated
            current_size += len(path) + len(truncated) + 10

        return json.dumps(result, ensure_ascii=False, indent=2)
