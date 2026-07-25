"""
prompt_builder.py — 服务端演化两阶段提示词构建器

两个类对应论文 Section 4.2 的两个阶段：
  Stage1Prompts → EvolutionPlanner （Stage 1: Evolution Planning）
  Stage2Prompts → EvolutionExecutor（Stage 2: Per-Client Personalized Evolution）

设计原则（与客户端 DistillerPromptBuilder 同一理念）：
  - 每段输入显式分区，对应论文公式中的变量
  - JSON Schema 明确定义，方便消融测试时替换单个段落
  - Stage2 的提示词区分不同 backbone（Qwen/GLM/Kimi 有不同风格偏好）

关于系统提示词文本的来源（重要）：
  Stage1/Stage2 的系统提示词核心文本保留自官方 SkillFlow 实验中使用的
  task_update_skill/SKILL.md、merge_skill/SKILL.md ——
  **官方实验性 Prompt 作为实验配置予以保留（retained as configuration），
  算法实现（本文件的分段拼装逻辑、server/planner.py 的规划流程、
  server/merge.py 的合并执行流程）由本项目独立实现（independently reproduced），
  不复制官方任何 .py 源码。**
  文本本身存放于 prompts/stage1_prompt.txt、prompts/stage2_prompt.txt，
  不再内嵌于本文件，便于单独审计「哪些是保留的实验配置」。
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

from core.datatypes import (
    Directive,
    LibraryDigest,
    LibrarySnapshot,
    PaperMergeAction,
    SkipUpdate,
    WorkerPatch,
    WorkerProfile,
)
from prompts import load_prompt
from server.capability import CapabilityTracker
from server.memory import EvolutionMemoryStore


# ---------------------------------------------------------------------------
# Stage 1 提示词
# ---------------------------------------------------------------------------


class Stage1PromptBuilder:
    """
    构建 Stage1（Evolution Planning）的提示词。

    论文 Section 4.2.1 输入：
        'full patch set {(ρ_i, δ_i^t)} + description-level digest of every client's
         pre-task library'
    注意：Stage1 只看摘要（LibraryDigest），不看完整 SKILL 文件。
    """

    # Stage1 输出 JSON Schema
    _STAGE1_SCHEMA = textwrap.dedent("""\
    {
      "capability_matrix": {
        "<workflow_name>": {
          "<worker_id>": "covered | absorbing | broken | gap"
        }
      },
      "high_level_memory": "<updated shared high-level memory text>",
      "low_level_memories": {
        "<worker_id>": "<updated private low-level memory text for this worker>"
      },
      "directives": [
        {
          "target_worker_id": "<target worker_id>",
          "workflow_name":    "<workflow_name>",
          "action":           "absorb | repair | refactor | no_update",
          "priority":         1,
          "reason":           "<evidence-backed reason>",
          "source_worker_id": "<peer worker_id that provided the skill, optional>",
          "source_reward":    0.0
        }
      ]
    }""")

    def build(
        self,
        round_idx: int,
        family_name: str,
        patches: dict[str, WorkerPatch],
        library_digests: dict[str, list[LibraryDigest]],
        capability_tracker: CapabilityTracker,
        memory_store: EvolutionMemoryStore,
        worker_profiles: dict[str, WorkerProfile],
    ) -> tuple[str, str]:
        """
        组装 (system_prompt, user_prompt)。
        """
        system = self._build_system(family_name, round_idx)
        user = "\n\n".join([
            self._section_patches(patches, worker_profiles),
            self._section_digests(library_digests),
            self._section_capability(capability_tracker),
            self._section_memory(memory_store, worker_profiles),
            self._section_instructions(round_idx),
            self._section_schema(),
        ])
        return system, user

    def _build_system(self, family_name: str, round_idx: int) -> str:
        # 系统提示词核心文本来自 prompts/stage1_prompt.txt（官方实验性 Prompt，
        # 作为实验配置保留；original experimental prompt retained as configuration).
        # 本方法只负责 format 占位符，不包含任何算法逻辑。
        template = load_prompt("stage1_prompt.txt")
        return template.format(family_name=family_name, round_idx=round_idx)

    def _section_patches(
        self,
        patches: dict[str, WorkerPatch],
        worker_profiles: dict[str, WorkerProfile],
    ) -> str:
        lines = ["## This Round's Worker Patches"]
        for wid, patch in patches.items():
            profile = worker_profiles.get(wid)
            model_info = f"({profile.backbone_model}/{profile.agent_harness})" if profile else ""
            lines.append(
                f"\n### Worker {wid} {model_info}\n"
                f"Reward: {patch.reward:.4f}  "
                f"Upserts: {len(patch.upserts)} files  "
                f"Deletions: {len(patch.deletions)} files\n"
                f"Summary: {patch.summary}\n"
                f"New skill directories: {list({p.split('/')[0] for p in patch.upserts if '/' in p})}"
            )
        return "\n".join(lines)

    def _section_digests(
        self, library_digests: dict[str, list[LibraryDigest]]
    ) -> str:
        lines = ["## Per-Worker Skill Library Digest (Stage1 sees only name + description)"]
        for wid, digests in sorted(library_digests.items()):
            lines.append(f"\n### Worker {wid} Skill Digest")
            if not digests:
                lines.append("  (library is empty)")
            for d in digests:
                lines.append(f"  - [{d.skill_name}] {d.description}")
        return "\n".join(lines)

    def _section_capability(self, tracker: CapabilityTracker) -> str:
        return f"## Current Capability Matrix C^{{t-1}}\n\n{tracker.summary_str()}"

    def _section_memory(
        self,
        memory_store: EvolutionMemoryStore,
        worker_profiles: dict[str, WorkerProfile],
    ) -> str:
        lines = [
            "## Evolution Memory",
            "\n### High-Level Shared Memory (Whole Family)",
            memory_store.high_level.content,
            "\n### Low-Level Private Memory (per Worker)",
        ]
        for wid in sorted(worker_profiles.keys()):
            mem_text = memory_store.get_worker_memory_text(wid)
            lines.append(f"\n**Worker {wid}**:\n{mem_text}")
        return "\n".join(lines)

    def _section_instructions(self, round_idx: int) -> str:
        return textwrap.dedent(f"""\
        ## Planning Guide (Round {round_idx})
        1. For each (workflow, worker) cell, assess its state from this round's reward and memory.
        2. If a worker's reward this round = 1.0, update the corresponding workflow state to covered.
        3. If a peer has reward = 1.0, you may issue an absorb/refactor directive to gap/broken workers.
        4. If a skill exists but keeps failing, issue a repair directive (no peer needed).
        5. Directive priority: 5 = urgent (workflow has no covered worker at all), 1 = low priority.
        6. High-level memory should record "workflows that consistently fail across workers" and
           "transfer patterns that worked".
        7. Low-level memory should record this worker's "model-specific failure modes" (e.g. Qwen's
           tendency to mishandle tabular data).""")

    def _section_schema(self) -> str:
        return (
            "## Output Format (strict JSON, no explanatory text)\n\n"
            "```json\n" + self._STAGE1_SCHEMA + "\n```"
        )


# ---------------------------------------------------------------------------
# Stage 2 提示词
# ---------------------------------------------------------------------------


class Stage2PromptBuilder:
    """
    构建 Stage2（Per-Client Personalized Evolution）的提示词。

    论文 Section 4.2.2：
        'The server then generates the personalized update Δ_i^t for each client,
         yielding L_i^{t+1} = Apply(L_i^t, Δ_i^t)'

    关键：Stage2 对每个 worker 独立运行，提示词包含该 worker 的 profile ρ_i，
    使 LLM 能够感知不同 backbone/harness 的风格差异。
    """

    # Stage2 输出 JSON Schema
    _STAGE2_SCHEMA = textwrap.dedent("""\
    {
      "upsert_files": {
        "<skill_dir>/SKILL.md":
          "---\\nname: <name>\\ndescription: <description>\\n---\\n\\n# Workflow\\n...",
        "<skill_dir>/scripts/<helper>.py": "# script..."
      },
      "delete_paths": ["<relative path>"],
      "summary": "<one-sentence description of this evolution>",
      "decision_log": {
        "action":           "absorb | repair | refactor | no_update",
        "source_worker_id": "<source worker_id, if any>",
        "affected_files":   ["<affected paths>"],
        "reason":           "<detailed reason (auditable record required by the paper)>"
      },
      "updated_low_level_memory": "<updated private low-level memory text for this worker>"
    }""")

    # Per-harness style hints
    _HARNESS_STYLE_HINTS: dict[str, str] = {
        "claude-code": "Target worker uses Claude Code — prefers step-by-step Markdown workflows with concise tool calls.",
        "qwen-code":   "Target worker uses Qwen Code — prefers structured Python scripts.",
        "kimi-cli":    "Target worker uses Kimi CLI — prefers direct command-line invocation patterns, avoid complex nesting.",
    }

    def build(
        self,
        round_idx: int,
        target_profile: WorkerProfile,
        directive: Directive,
        current_snapshot: LibrarySnapshot,
        peer_patches: dict[str, WorkerPatch],
        peer_profiles: dict[str, WorkerProfile],
        low_level_memory_text: str,
        peer_library_digests: dict[str, list[LibraryDigest]] | None = None,
        capability_tracker: CapabilityTracker | None = None,
    ) -> tuple[str, str]:
        """
        组装 Stage2 (system_prompt, user_prompt)。

        peer_library_digests: 官方 merge_skill/SKILL.md Inputs 清单中的
            `peer_libraries/<peer>/` 只读快照——每个同伴完整技能库的
            name+description 摘要（不含正文），供"跨 worker 命名对齐"
            "伞形结构共识"使用。区别于 peer_patches（只反映本轮增量提案），
            这里反映同伴库的当前整体结构。为 None 时保持旧行为（不渲染该
            小节，向后兼容未传该参数的调用方/旧测试）。
        capability_tracker: 官方 merge_skill/SKILL.md Inputs 清单中的
            `task_memory.md`（Stage1 产出的全体 worker×workflow 覆盖矩阵）。
            官方要求 "Read task_memory.md early — before deciding on
            patches"——此前 Stage2 只拿到 Stage1 已经"降维"成单条 directive
            的结论（action/reason/source_worker_id），看不到矩阵全貌，无法
            判断"这个 gap 是不是全体 worker 都覆盖不了的持续性 gap"这类需要
            横向对比的信息，也就没法真正执行"gap 优先补、broken 优先修、
            covered 只做轻量集成、持续 gap 才做重构"的优先级判断。为 None
            时保持旧行为（不渲染该小节，向后兼容未传该参数的调用方/旧测试）。
        """
        peer_library_digests = peer_library_digests or {}
        system = self._build_system(target_profile)
        sections = [
            self._section_directive(directive),
        ]
        if capability_tracker is not None:
            sections.append(self._section_task_memory(capability_tracker))
        sections.extend([
            self._section_current_library(current_snapshot),
            self._section_peer_patches(peer_patches, peer_profiles, directive),
            self._section_peer_libraries(peer_library_digests, peer_profiles),
            self._section_memory(low_level_memory_text, target_profile.client_id),
            self._section_instructions(directive, target_profile),
            self._section_schema(),
        ])
        user = "\n\n".join(sections)
        return system, user

    def _build_system(self, profile: WorkerProfile) -> str:
        # 系统提示词核心内容来自 prompts/stage2_prompt.txt（官方实验性 Prompt，
        # 作为实验配置保留；original experimental prompt retained as configuration).
        # 本方法只负责 format 占位符，不包含任何算法逻辑。
        harness_hint = self._HARNESS_STYLE_HINTS.get(profile.agent_harness, "")
        template = load_prompt("stage2_prompt.txt")
        return template.format(
            client_id=profile.client_id,
            backbone_model=profile.backbone_model,
            agent_harness=profile.agent_harness,
            harness_hint=harness_hint,
        )

    def _section_directive(self, directive: Directive) -> str:
        source_info = ""
        if directive.source_worker_id:
            source_info = (
                f"  Source Worker: {directive.source_worker_id}  "
                f"Source Reward: {directive.source_reward or 'N/A'}"
            )
        return (
            f"## Stage1 Evolution Directive\n"
            f"Action: **{directive.action.value.upper()}**  "
            f"Priority: {directive.priority}/5\n"
            f"Target workflow: {directive.workflow_name}\n"
            f"Reason: {directive.reason}\n"
            f"{source_info}"
        )

    def _section_task_memory(self, capability_tracker: CapabilityTracker) -> str:
        """
        官方 merge_skill/SKILL.md Inputs 清单中的 `task_memory.md`——Stage1
        产出的全体 worker×workflow 覆盖矩阵原文（与 Stage1PromptBuilder.
        `_section_capability()` 复用同一份 `CapabilityTracker.summary_str()`
        渲染，保证 Stage1/Stage2 看到的矩阵文本逐字一致，不重新计算）。

        官方原文要求 "Read task_memory.md early — before deciding on
        patches"：本节的 directive 只是 Stage1 已经对**这一个** workflow
        做出的降维结论，矩阵全貌能让 Stage2 判断这个 gap/broken 是不是
        跨全体 worker 的持续性问题（"持续 gap 才做重构"这类需要横向对比
        才能下的判断），而不仅仅依赖 directive 自带的单条 reason 文本。
        """
        return (
            f"## Task Memory (task_memory.md — family-shared coverage matrix from Stage1)\n\n"
            f"{capability_tracker.summary_str()}"
        )

    def _section_current_library(self, snapshot: LibrarySnapshot) -> str:
        if not snapshot.files:
            return "## Current Skill Library (Target Worker)\n\n(library is empty)"
        files_dict = {f.path: f.content for f in snapshot.files}
        # Show SKILL.md in full; truncate other files
        skill_mds = {k: v for k, v in files_dict.items() if k.endswith("SKILL.md")}
        others = {k: v[:300] + " ...[truncated]" if len(v) > 300 else v
                  for k, v in files_dict.items() if not k.endswith("SKILL.md")}
        preview = json.dumps({**skill_mds, **others}, ensure_ascii=False, indent=2)
        return f"## Current Skill Library (Target Worker, {snapshot.skill_count} skills)\n\n```json\n{preview}\n```"

    def _section_peer_patches(
        self,
        peer_patches: dict[str, WorkerPatch],
        peer_profiles: dict[str, WorkerProfile],
        directive: Directive,
    ) -> str:
        if not peer_patches:
            return "## Peer Patches (for reference)\n\n(no peer patches)"

        lines = ["## Peer Patches (for absorb/refactor reference)"]
        for wid, patch in peer_patches.items():
            profile = peer_profiles.get(wid)
            model_info = f"({profile.backbone_model})" if profile else ""
            is_source = "⭐ " if wid == directive.source_worker_id else ""
            lines.append(
                f"\n### {is_source}Worker {wid} {model_info} — "
                f"reward={patch.reward:.3f}\n"
                f"Summary: {patch.summary}"
            )
            # Show full upsert content (Stage2 needs the full text to absorb/refactor)
            for path, content in patch.upserts.items():
                lines.append(f"\n**{path}**:\n```\n{content[:1500]}\n```")
        return "\n".join(lines)

    def _section_peer_libraries(
        self,
        peer_library_digests: dict[str, list[LibraryDigest]],
        peer_profiles: dict[str, WorkerProfile],
    ) -> str:
        """
        官方 merge_skill/SKILL.md Inputs 清单中的 `peer_libraries/<peer>/`
        只读快照（对应 [Priority-1] Stage2 输入结构对齐修复）：每个同伴
        **完整技能库**的 name+description 摘要（不含正文，避免上下文爆炸），
        供 "## Cross-worker library consistency" 一节描述的两条规则使用：
          - 跨 worker 命名对齐：同一 workflow 在 ≥2 个同伴库里叫什么名字。
          - 伞形结构共识：同伴是否已经把若干窄技能合并成了一个 umbrella。
        与 `_section_peer_patches()` 的区别：那里只反映"本轮同伴新提交的
        增量提案"，而这里反映"同伴库当前的整体结构"——即便同伴本轮完全
        没有触碰某个技能，其名字依然会出现在这里，这正是命名对齐判断
        "peer majority" 所必需的稳定信号。
        """
        if not peer_library_digests:
            return "## Peer Libraries (full skill-graph digest, for naming/umbrella consensus)\n\n(no peer library data provided)"

        lines = ["## Peer Libraries (full skill-graph digest, for naming/umbrella consensus)"]
        for wid, digests in sorted(peer_library_digests.items()):
            profile = peer_profiles.get(wid)
            model_info = f"({profile.backbone_model}/{profile.agent_harness})" if profile else ""
            lines.append(f"\n### Worker {wid} {model_info} — current library ({len(digests)} skills)")
            if not digests:
                lines.append("  (library is empty)")
            for d in digests:
                lines.append(f"  - [{d.skill_name}] {d.description}")
        return "\n".join(lines)

    def _section_memory(self, memory_text: str, worker_id: str) -> str:
        return f"## Worker {worker_id} Private Historical Memory\n\n{memory_text}"

    def _section_instructions(
        self, directive: Directive, profile: WorkerProfile
    ) -> str:
        action_guide = {
            PaperMergeAction.ABSORB: (
                f"Absorb the skill from {directive.source_worker_id}, "
                f"adapting it to {profile.agent_harness}'s invocation style."
            ),
            PaperMergeAction.REPAIR: "Repair the broken skill in the current library, keeping the SKILL.md structure unchanged and only updating the workflow content.",
            PaperMergeAction.REFACTOR: (
                f"Rewrite the skill from {directive.source_worker_id} "
                f"using {profile.backbone_model}'s preferences into a version better suited for this worker."
            ),
            SkipUpdate.NO_UPDATE: "Return an empty patch (no update needed) and explain why in the summary.",
        }
        guide = action_guide.get(directive.action, "Execute the action according to the directive.")
        return f"## Execution Guide\n{guide}"

    def _section_schema(self) -> str:
        return (
            "## Output Format (strict JSON, no explanatory text)\n\n"
            "```json\n" + self._STAGE2_SCHEMA + "\n```\n\n"
            "Path rule: all paths are relative to the skill library root and must not contain '/' or '..'"
        )
