"""
datatypes.py — All typed data structures for FederatedSkill reproduction.

Every class here corresponds exactly to a variable, equation, or concept in
the paper.  The mapping is documented in each class docstring.

Paper: FederatedSkill: Federated Learning for Agentic Skill Evolution

Notation quick-reference:
  ρ_i           → WorkerProfile
  τ_i           → Trajectory   (raw single-trial trajectory; MUST NOT leave the client)
  B_i^t         → TrajectoryBuffer  (per-round history batch used for distillation)
  L_i^t         → SkillLibrary  (managed by client/library.py)
  δ_i^t         → WorkerPatch   (the ONLY artifact uploaded to the server; 4-tuple)
  g_i(·)        → PatchDistiller.distill()
  P^t           → EvolutionPlan
  Δ_i^t         → MergedPatch
  C^t           → CapabilityMatrix
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Helper – path safety
# ---------------------------------------------------------------------------


def validate_safe_rel_path(path: str) -> str | None:
    """
    Validate that *path* is a safe relative path for use inside a skill library.

    Returns the cleaned path (forward-slash normalised) or ``None`` if unsafe.

    Security:  Paper Section 4.1.2 – 'all paths in U_i^t ∪ D_i^t are
    rigorously validated to reject absolute paths or directory traversals.'
    Implementation mirrors the official ``safe_rel_path()`` in merge.py.
    """
    if not isinstance(path, str):
        return None
    s = path.strip()
    if not s:
        return None
    # Reject POSIX / Windows absolute prefixes before any normalisation
    if s.startswith(("/", "\\")):
        return None
    # Normalise separators
    s = s.replace("\\", "/")
    # Strip a single leading "./"
    if s.startswith("./"):
        s = s[2:]
    if not s:
        return None
    p = PurePosixPath(s)
    if p.is_absolute():
        return None
    for part in p.parts:
        if part in ("", "..", "."):
            return None
        # Windows drive letter (e.g. "C:" or "C:foo")
        if len(part) >= 2 and part[1] == ":":
            return None
    return str(p)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class CapabilityState(str, Enum):
    """
    States for each (workflow, worker) cell in the capability matrix C^t.

    Paper Section 4.2.1:
        'one of four states: covered, absorbing, broken, or gap'
    """

    COVERED = "covered"      # Client reliably solves this workflow
    ABSORBING = "absorbing"  # This round's patch supplies the missing skill
    BROKEN = "broken"        # Skill exists but fails; requires repair
    GAP = "gap"              # No working skill exists for this workflow


class PaperMergeAction(str, Enum):
    """
    [PAPER] Actions the server-side evolution agent may apply per directive.

    Paper Section 4.2.2: 'absorbing transferable patches, repairing broken
    skills in place, or rewriting peer skills to align with ρ_i'

    This enum contains EXACTLY the three actions the paper describes — no
    more, no less. Renamed from the historical `MergeAction` (which also
    carried a 4th project-only member, `DROP`) so that both the class name
    and its member set honestly say "this is the paper's action space, and
    nothing else." The engineering-only "no evolution needed" skip path is
    now a *separate* type, `SkipUpdate` (see below), not a member here.
    """

    ABSORB = "absorb"     # 直接吸收同伴的高分 patch（reward=1.0）
    REPAIR = "repair"     # 就地修复损坏技能（不依赖 peer）
    REFACTOR = "refactor" # 将同伴技能重写为适配本 worker ρ_i 的版本


class SkipUpdate(str, Enum):
    """
    [EXTENSION] Engineering-only "no evolution needed" marker.

    NOT part of `PaperMergeAction` / the paper's action space. Used when
    Stage1 determines a directive requires no update for the target worker
    this round, letting Stage2 (`server.merge.EvolutionExecutor`) skip the
    LLM call entirely — a cost-saving fast path, not an algorithm step.

  
    """

    NO_UPDATE = "no_update"


def parse_merge_action(action_str: str) -> "PaperMergeAction | SkipUpdate":
    """
    把 LLM 输出的原始 action 字符串解析为 `PaperMergeAction` 或 `SkipUpdate`。

    先尝试 `[PAPER]` 的三个动作，再尝试 `[EXTENSION]` 的 `NO_UPDATE`；
    都无法匹配时抛出 `ValueError`，由调用方决定降级策略。
    统一封装，避免 `server/planner.py` 与 `server/merge.py` 各自重复解析逻辑。
    """
    try:
        return PaperMergeAction(action_str)
    except ValueError:
        return SkipUpdate(action_str)


# ---------------------------------------------------------------------------
# Worker identity  (ρ_i)
# ---------------------------------------------------------------------------


class WorkerProfile(BaseModel):
    """
    Corresponds to ρ_i in the paper — the static client profile.

    Paper Section 3:
        'a static profile ρ_i (backbone model and agent harness)'
    Paper Equation (2):
        δ_i^t = g_i(L_i^t, B_i^t, ρ_i)

    Frozen: profile never changes during a federation run.
    Used by:
      - PatchDistiller to route LLM calls to the correct backbone
      - Server-side evolution agent to personalise merges
    """

    model_config = ConfigDict(frozen=True)

    client_id: str = Field(description="Unique identifier for this federated client")
    backbone_model: str = Field(
        description="LLM backbone. E.g. 'qwen3.6-plus', 'glm-5', 'kimi-k2.5'"
    )
    agent_harness: str = Field(
        description="Agent CLI/harness. E.g. 'claude-code', 'qwen-code', 'kimi-cli'"
    )
    model_provider: str = Field(
        description="API provider. E.g. 'dashscope', 'zhipu', 'moonshot', 'anthropic'"
    )
    api_base: str = Field(description="OpenAI-compatible API base URL")
    api_key_env: str = Field(
        description="Name of the environment variable that holds the API key"
    )
    # ---- Stage2 个性化所需：Server 用这两个字段区分 Qwen / GLM / Claude 策略 ----
    system_prompt_name: str = Field(
        default="default",
        description="Prompt 模板名称；不同 harness 有不同的 system prompt 风格",
    )
    # 对应原版 UserSpec.agent_import_path（harness Agent 的 Python 导入路径）
    agent_import_path: str = Field(
        default="",
        description="Agent Harness 类的 Python import 路径，如 "
                    "'libs.harbor_noinstall_agents.agents:NoInstallQwenCode'",
    )
    # 对应原版 UserSpec.agent_kwargs（harness 初始化参数）
    agent_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="传给 agent harness 构造函数的额外关键字参数",
    )
    max_context_tokens: int = Field(default=32_768, gt=0)
    generation_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ---- Full Reproduction Alignment Audit TASK2（ρ_i 描述性增强字段） ----
    # 论文 Section 3 把 ρ_i 字面定义为 'a static profile ρ_i
    # (backbone model and agent harness)'在实践中已经在用「这个 worker 擅长什么任务族、有哪些工具、已知能力
    # 标签」这类更细粒度信息做 capability-aware 决策。
    # Current mismatch: 这些描述性信息此前只能靠 prompt 里零散字符串拼接表达，
    # WorkerProfile 本身不携带结构化字段，无法被审计/序列化/跨模块复用。
    # Code change: 新增三个可选描述性字段，仅作为 prompt 拼装的补充上下文，不参与 profile_hash 计算
    # （原因见下方 profile_hash 的说明）。
    task_family: str = Field(
        default="",
        description="该 worker 当前主要负责的任务族名称（描述性元数据，不参与 profile_hash/记忆路由）",
    )
    available_tools: list[str] = Field(
        default_factory=list,
        description="该 worker 的 agent harness 暴露的工具列表（描述性元数据，不参与 profile_hash/记忆路由）",
    )
    capability_tags: list[str] = Field(
        default_factory=list,
        description="人工/离线标注的能力标签（描述性元数据，不参与 profile_hash/记忆路由）",
    )

    @computed_field  # type: ignore[misc]
    @property
    def profile_hash(self) -> str:
        """
        Stable 12-char hash of (backbone_model, agent_harness) ONLY.

        Full Reproduction Alignment Audit TASK2 — 为什么不把 task_family /
        available_tools / capability_tags 纳入哈希：

        Paper motivation: 论文对 ρ_i 的唯一定义就是 backbone model + agent
        harness 两项（Section 3），两级记忆 M^t 的 low-level 记忆 'keyed by
        ρ_i' 语义（Section 4.2.1）也完全基于这两项构成的等价类。
        Current mismatch（若纳入哈希会引入的新问题）: 若把 task_family 等也
        编码进 profile_hash，会让"同一 (backbone, agent_harness) 但当前任务
        族不同"的两个 worker 被错误分配到不同记忆桶，与论文"同一个 ρ_i 共享
        memory"的字面要求矛盾（论文未说 ρ_i 会随任务族变化，它是 static
        profile）。
        Code change: 保持 profile_hash 公式不变（仍只哈希 backbone_model +
        agent_harness），新增字段仅作为 prompt 拼装的补充描述性上下文，不影响
        EvolutionMemoryStore 的分桶路由（server/memory.py 未改动）。
        """
        raw = f"{self.backbone_model}:{self.agent_harness}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    @property
    def is_moonshot(self) -> bool:
        """True for Kimi/Moonshot — requires temperature >= 1.0."""
        return "moonshot" in self.model_provider.lower() or "kimi" in self.backbone_model.lower()


# ---------------------------------------------------------------------------
# Trajectory  (τ_i)
# ---------------------------------------------------------------------------


class TrajectoryStep(BaseModel):
    """
    One step in the raw execution trajectory τ.

    Metrics (tokens_used) are stripped during compaction per Section 4.1.2.
    """

    step_index: int = Field(ge=0, description="0-based position in trajectory")
    role: str = Field(description="'user' | 'assistant' | 'tool'")
    content: str = Field(default="")
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    observation: str = Field(
        default="",
        description="Environment observation; may be truncated in CompactedTrajectory",
    )
    tokens_used: int = Field(default=0, ge=0, description="Stripped during compaction")


class Trajectory(BaseModel):
    """
    Raw execution trajectory τ_i produced by the client agent for a single trial.

    Paper Section 4.1.1:
        τ_i ~ π_i(·|L_i^t, ρ_i)
        'The local agent is conditioned on its current skill library L_i^t
         and its specific LLM backbone… generates an execution trajectory τ'

    Note: B_i^t (behavior history batch) is represented by TrajectoryBuffer,
    which wraps a list of Trajectory objects across one federation round.

    
    """

    task_name: str
    worker_id: str
    round_idx: int = Field(ge=0, description="Federation round index")
    steps: list[TrajectoryStep] = Field(default_factory=list)
    stdout: str = Field(default="")
    stderr: str = Field(default="")
    final_message: str = Field(default="", description="Last 'assistant' message")
    reward: float | None = Field(
        default=None, description="R_{i,x}(τ) from the verifier (None if not run)"
    )
    soft_reward: float | None = Field(
        default=None,
        description=(
            "Soft reward: inner sub-test pass rate 0.0-1.0. "
            "Matches official patcher_bridge._compute_soft_reward(): "
            "parsed from inner pytest progress bar in verifier_output. "
            "If hard reward = 1.0, soft_reward = 1.0. "
            "If unparseable, falls back to hard reward."
        ),
    )
    verifier_output: str = Field(default="", description="Raw verifier stdout")
    verifier_subtest_failures: list[str] = Field(
        default_factory=list, description="Names of failed sub-tests"
    )
    total_tokens: int = Field(default=0, ge=0)
    runtime_seconds: float = Field(default=0.0, ge=0.0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    exception_info: dict[str, Any] | None = Field(
        default=None,
        description="{'exception_type': str, 'exception_message': str} if trial crashed",
    )

    # ---- Phase12 新增字段：真实 agent workspace 模式所需（向后兼容，均为可选） ----
    # 新版 executor（executor/agent_executor.py::AgentWorkspaceExecutor）会显式填充
    # actions / generated_files；旧版 executor（client/executor.py 等）保持不填，
    # 默认空列表，不影响既有测试。
    actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Agent 在本次 trial 中执行的高层动作序列（write_file / run_command / "
            "verify 等），由支持真实 workspace 模式的 executor 填充。"
            "对应用户 Phase12 需求 'actions'。"
        ),
    )
    generated_files: list[str] = Field(
        default_factory=list,
        description=(
            "本次 trial 在 workspace 内新生成/修改的文件相对路径列表"
            "（由 executor/environment.py::WorkspaceManager 的前后快照 diff 得到）。"
        ),
    )
    exceptions: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "本次 trial 抛出的异常列表（列表形式，兼容多步骤多次异常的 agent workspace "
            "流程）。若调用方未显式填充，_sync_derived_fields() 会从 exception_info "
            "自动派生一条记录，保证旧版 executor 产出的 Trajectory 同样满足该字段。"
        ),
    )
    verification: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "结构化验证结果 {'reward', 'verifier_output', 'subtest_failures'}。"
            "若调用方未显式填充，_sync_derived_fields() 会从 reward/verifier_output/"
            "verifier_subtest_failures 自动派生，保证所有 Trajectory（含旧版 executor "
            "产出）都能满足该字段，无需修改已通过测试的 executor 实现。"
        ),
    )

    # ---- 官方对齐 Part2/Part3 新增字段（均可选/向后兼容，默认空） ----
    execution_logs: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "harness 强制执行生成代码这一步骤的日志（区别于 agent 自己在 CLI "
            "session 内部的 Bash 调用）：每条记录形如 "
            "{'stage','executed','returncode','stdout','stderr','timed_out'}。"
            "由 harness/base_harness.py 的强制执行步骤填充；旧版 executor "
            "（未做强制执行）保持空列表，向后兼容。"
        ),
    )
    failure_reason: str = Field(
        default="",
        description=(
            "reward<1.0 时的失败原因摘要，严格从 verifier_output / "
            "verifier_subtest_failures / exception_info 派生（见 "
            "_sync_derived_fields()），绝不猜测/复述 agent 聊天文本。"
            "供 PatchDistiller 直接消费，取代此前只能从 chat 文本猜测失败原因的做法。"
        ),
    )

    @model_validator(mode="after")
    def _sync_derived_fields(self) -> "Trajectory":
        """
        向后兼容派生：仅当调用方未显式填充 exceptions / verification / 
        failure_reason 时，从既有字段（exception_info / reward / 
        verifier_output / verifier_subtest_failures）自动派生，不改变任何
        既有 executor 的行为。
        """
        if not self.exceptions and self.exception_info:
            self.exceptions = [self.exception_info]
        if not self.verification:
            self.verification = {
                "reward": self.reward,
                "verifier_output": self.verifier_output,
                "subtest_failures": list(self.verifier_subtest_failures),
            }
        if not self.failure_reason and self.reward is not None and self.reward < 1.0:
            self.failure_reason = self._derive_failure_reason()
        return self

    def _derive_failure_reason(self) -> str:
        """严格基于 verifier/exception 信号推导失败原因，不读取 final_message。

        优先级：exception_info（进程级异常）> verifier_subtest_failures
        （命名的子测试失败）> verifier_output（原始 verifier 输出前 500 字符）
        > 兜底提示。与用户 Part5 要求一致：'不要根据聊天文本猜失败原因'。
        """
        if self.exception_info:
            et = self.exception_info.get("exception_type", "")
            em = self.exception_info.get("exception_message", "")
            return f"exception: {et}: {em}".strip()
        if self.verifier_subtest_failures:
            return "verifier subtest failures: " + "; ".join(self.verifier_subtest_failures[:10])
        if self.verifier_output:
            return self.verifier_output[:500]
        return "reward < 1.0 但无可用的 verifier_output/verifier_subtest_failures/exception_info"

    @computed_field  # type: ignore[misc]
    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """
        聚合所有 step 内的 tool_calls（论文 Appendix B Agent Harness 中的
        Tool Calling 层）。只读派生字段，自动适用于所有 Trajectory
        （包括已通过测试的旧版 executor 产出，无需修改其实现）。
        """
        calls: list[dict[str, Any]] = []
        for step in self.steps:
            calls.extend(step.tool_calls)
        return calls

    @computed_field  # type: ignore[misc]
    @property
    def token_usage(self) -> int:
        """token_usage 是 total_tokens 的只读别名，命名对齐用户 Phase12 要求。"""
        return self.total_tokens


class TrajectoryBuffer(BaseModel):
    """
    Per-round trajectory history batch B_i^t.

    Paper Section 4.1.2:
        B_i^t is the full trajectory batch collected during round t.
        The distiller g_i(·) receives B_i^t (via CompactedTrajectory) as one of
        three inputs: δ_i^t = g_i(L_i^t, B_i^t, ρ_i)

    In this implementation B_i^t holds one Trajectory per task assignment;
    a round with multiple tasks per worker would produce multiple entries.
    """

    worker_id: str
    round_idx: int = Field(ge=0)
    trajectories: list[Trajectory] = Field(
        default_factory=list,
        description="τ_i instances collected this round (one per task trial)",
    )

    @property
    def count(self) -> int:
        """Number of trial trajectories in this buffer."""
        return len(self.trajectories)

    @property
    def mean_reward(self) -> float:
        """Average R_{i,x}(τ) across all trials in the buffer."""
        rewards = [t.reward for t in self.trajectories if t.reward is not None]
        return sum(rewards) / len(rewards) if rewards else 0.0

    def latest(self) -> Trajectory | None:
        """Return the most recently appended trajectory."""
        return self.trajectories[-1] if self.trajectories else None


class CompactedTrajectory(BaseModel):
    """
    Compacted representation produced from a raw Trajectory by TrajectoryCompressor.

    Paper Section 4.1.2 ❶ (Compacted Trajectory):
        'A compressed sequence retaining at most K_step agentic steps
         (the initial step plus the K_step − 1 most recent steps).
         Execution metrics are stripped, and environment observations are
         truncated to K_obs characters via an explicit <truncated> marker.'

    This is the only trajectory-derived input the patcher sees.
    """

    task_name: str
    worker_id: str
    steps: list[TrajectoryStep] = Field(
        description="Initial step + last (K_step − 1) steps; metrics stripped"
    )
    final_message: str = Field(default="")
    exception_types: list[str] = Field(default_factory=list)
    original_step_count: int = Field(ge=0, description="Total steps before compaction")
    k_step: int = Field(ge=1, description="Max steps retained (paper K_step)")
    k_obs: int = Field(ge=1, description="Max observation chars (paper K_obs)")

    @property
    def was_truncated(self) -> bool:
        return self.original_step_count > self.k_step


# ---------------------------------------------------------------------------
# Trial outcome  (R_{i,x}(τ) + metadata)
# ---------------------------------------------------------------------------


class TrialOutcome(BaseModel):
    """
    Summary metadata extracted from a Trajectory for patch distillation.

    Paper Section 4.1.2 ❸ (Trial Outcome):
        'Summary metadata including the task name, a quality signal R_{i,x}(τ),
         exception types, the final agent message, and verification subtest
         failures (if available).'
    """

    task_name: str
    reward: float = Field(description="R_{i,x}(τ) — verification reward ∈ [0, 1]")
    success: bool = Field(default=False, description="True iff reward >= 1.0")
    verifier_score: float = Field(
        default=0.0,
        description="Soft sub-test pass rate (0–1). Different from hard reward.",
    )
    exception_types: list[str] = Field(default_factory=list)
    final_agent_message: str = Field(default="")
    verification_failures: list[str] = Field(
        default_factory=list, description="Failed verifier sub-test names"
    )
    runtime_seconds: float = Field(default=0.0, ge=0.0)
    token_usage: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)

    # ---- 官方对齐 Part5 新增字段：让 PatchDistiller 的失败诊断严格基于
    # verifier 反馈，而不是从 final_agent_message（chat 文本）猜测 ----
    verifier_feedback: str = Field(
        default="",
        description=(
            "Trajectory.verifier_output 的截断副本（原始 verifier stdout/stderr，"
            "含具体 AssertionError 等细节），供 prompt 里独立于 chat 文本单独展示。"
        ),
    )
    failure_reason: str = Field(
        default="",
        description=(
            "Trajectory.failure_reason 的直接透传（已在 core.datatypes.Trajectory."
            "_derive_failure_reason() 里严格基于 verifier/exception 信号推导，"
            "不基于 chat 文本猜测）。"
        ),
    )

    @model_validator(mode="after")
    def _compute_success(self) -> "TrialOutcome":
        # Per paper: reward = 1.0 is the ONLY 'task passed' value
        self.success = self.reward >= 1.0
        return self


# ---------------------------------------------------------------------------
# Skill library representations  (L_i^t)
# ---------------------------------------------------------------------------


class LibraryFileEntry(BaseModel):
    """One file entry in a LibrarySnapshot."""

    path: str = Field(description="Relative path within the library root (forward slashes)")
    content: str = Field(description="Full UTF-8 file content")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        safe = validate_safe_rel_path(v)
        if safe is None:
            raise ValueError(f"Unsafe library file path: {v!r}")
        return safe


class LibraryDigest(BaseModel):
    """
    Description-level digest of a single skill.

    Paper Section 4.2.1:
        'description-level digest of every client's pre-task library,
         restricted to skill names and descriptions'

    Used in Stage 1 (server planning) ONLY.
    The server planner MUST NOT receive full library content in Stage 1.
    """

    skill_name: str
    description: str
    directory: str = Field(default="", description="Skill directory name in library")
    tags: list[str] = Field(default_factory=list)
    trigger: str = Field(default="", description="When to invoke this skill")


class LibrarySnapshot(BaseModel):
    """
    Full library snapshot L_i^t — all file paths and their contents.

    Paper Section 4.1.2 ❷ (Library Snapshot):
        'A JSON snapshot of the current library L_i^t detailing all file paths
         and their corresponding contents.'

    Passed to PatchDistiller.  Contains complete skill files.
    """

    worker_id: str
    round_idx: int = Field(ge=0)
    files: list[LibraryFileEntry] = Field(default_factory=list)
    skill_count: int = Field(default=0, description="Number of SKILL.md files")
    total_size_bytes: int = Field(default=0)

    @model_validator(mode="after")
    def _compute_stats(self) -> "LibrarySnapshot":
        skill_count = sum(
            1 for f in self.files
            if f.path.endswith("/SKILL.md") or f.path == "SKILL.md"
        )
        total_bytes = sum(len(f.content.encode("utf-8")) for f in self.files)
        self.skill_count = skill_count
        self.total_size_bytes = total_bytes
        return self

    def to_path_content_dict(self) -> dict[str, str]:
        """Return {rel_path: content} for prompt formatting."""
        return {f.path: f.content for f in self.files}

    def filter_skill_mds(self) -> list[LibraryFileEntry]:
        """Return only SKILL.md entries (for truncation-safe prompts)."""
        return [
            f for f in self.files
            if f.path.endswith("/SKILL.md") or f.path == "SKILL.md"
        ]


# ---------------------------------------------------------------------------
# WorkerPatch  (δ_i^t) — the communication unit
# ---------------------------------------------------------------------------


class WorkerPatch(BaseModel):
    """
    Corresponds to δ_i^t = (U_i^t, D_i^t, R_{i,x}(τ), s_i^t).

    Paper Section 4.1.2 — Patch Schema (Equation 4).
    The communication unit's *semantic* payload is exactly the 4-tuple:

        δ_i^t = (U_i^t, D_i^t, R_{i,x}(τ), s_i^t)

    This is the ONLY artifact that leaves the client:
      'By uploading only δ_i^t instead of B_i^t, the framework preserves
       privacy by construction.'

    Privacy guarantees (Section 4.1.2):
      U_i^t  — reusable playbooks only; no task-specific values, IDs, or outputs
      D_i^t  — structural paths only; no raw interaction text
      All paths validated: no absolute paths, no directory traversals

    ``worker_id`` is included because Appendix B.2's concrete patch-manifest
    artifact shows it inline in the real JSON payload:
        {"worker_id": "u0", "reward": 1.0, "summary": "...", "delete_paths": []}
    No other envelope fields (round_idx / timestamp / task_name / profile_hash)
    appear in that artifact, so they are intentionally NOT added here — those
    stay in the caller's routing envelope (e.g. dict[worker_id, WorkerPatch]),
    not in the payload, to preserve the minimization principle.

    Full Reproduction Alignment Audit TASK1 (Patch Schema Alignment):

    Paper motivation: Eq. 4 defines δ_i^t = (U_i^t, D_i^t, R_{i,x}(τ), s_i^t).
    The distiller g_i(·) (Section 4.1.2) is explicitly asked to "analyze the
    trial trajectory" and explain *why* it changed the library the way it
    did — a rationale distinct from R_{i,x}(τ) (a pure numeric verifier
    signal used only for evaluation/Table metrics).
    Current mismatch: prior to this change, ``summary`` was overloaded to
    carry both roles — a short one-line label AND (implicitly) the only
    place any "reasoning" text could live — and ``reward`` was never at risk
    of being conflated with rationale, but there was no dedicated field for
    the LLM's causal explanation itself, so it either got silently dropped
    or squeezed into the one-sentence ``summary``.
    Code change: added ``rationale`` (below) as a new, independent field.
    ``reward`` is UNCHANGED and still the sole evaluation signal; ``summary``
    is UNCHANGED (still the short s_i^t label). ``rationale`` is additive and
    defaults to "" for backward compatibility with existing callers/tests
    that construct WorkerPatch without it.

    Deliberately NOT added (audited and rejected, not merely omitted):
      - ``source_worker``: a WorkerPatch is always self-produced by the
        uploading worker from its own trajectory (Eq. 4 has no peer-sourced
        term); cross-client provenance is a Stage1/Stage2-only concept and
        already exists on ``Directive.source_worker_id`` /
        ``DecisionLog.source_worker_id``. Adding it here would fabricate a
        field with no corresponding paper variable.
      - ``source_task``: Section 4.1.2 explicitly requires U_i^t to contain
        "no task-specific values, IDs, or outputs" (privacy minimization).
        Adding a task-identifying field to the uploaded artifact would
        directly violate that stated privacy guarantee, so it is
        intentionally NOT added.
    """

    # 路由标识（对应 Appendix B.2 patch manifest 中的 "worker_id" 字段）
    worker_id: str = Field(description="Uploading client's id; mirrors Appendix B.2 manifest")

    # U_i^t  — file upserts
    upserts: dict[str, str] = Field(
        default_factory=dict,
        description="U_i^t: {rel_path: full_content}. Generalizable procedures ONLY.",
    )

    # D_i^t  — paths to delete
    deletions: list[str] = Field(
        default_factory=list,
        description="D_i^t: relative paths to delete from the library.",
    )

    # R_{i,x}(τ)  — quality signal (evaluation-only; NOT to be conflated with rationale)
    reward: float = Field(description="R_{i,x}(τ): trial verification reward, used for evaluation ONLY")

    # s_i^t  — one-sentence summary
    summary: str = Field(
        description="s_i^t: one-sentence label recorded for downstream auditing"
    )

    # [TASK1 新增] LLM 对失败原因/技能修改理由的详细解释 —— 区别于 reward
    # （实验反馈信号）与 summary（一句话标签）。默认空字符串，向后兼容旧调用方。
    rationale: str = Field(
        default="",
        description=(
            "LLM's causal explanation of WHY the trial failed/succeeded and WHY this "
            "specific patch addresses it. Distinct from `reward` (a pure evaluation "
            "signal, never used as a proxy for this) and from `summary` (a one-sentence "
            "label)."
        ),
    )

    @field_validator("upserts")
    @classmethod
    def _validate_upsert_paths(cls, v: dict[str, str]) -> dict[str, str]:
        """
        'all paths in U_i^t ∪ D_i^t are rigorously validated to reject
         absolute paths or directory traversals'
        """
        cleaned: dict[str, str] = {}
        for path, content in v.items():
            safe = validate_safe_rel_path(path)
            if safe is None:
                raise ValueError(f"Unsafe upsert path: {path!r}")
            if not content or content == "<binary>":
                continue  # Skip empty / binary placeholders
            cleaned[safe] = content
        return cleaned

    @field_validator("deletions")
    @classmethod
    def _validate_deletion_paths(cls, v: list[str]) -> list[str]:
        cleaned = []
        for path in v:
            safe = validate_safe_rel_path(str(path))
            if safe is None:
                raise ValueError(f"Unsafe deletion path: {path!r}")
            cleaned.append(safe)
        return cleaned


# ---------------------------------------------------------------------------
# Server-side capability tracking  (C^t, two-level memory)
# ---------------------------------------------------------------------------


class CapabilityMatrixCell(BaseModel):
    """One (workflow_row × worker_col) cell in the capability matrix C^t."""

    task_workflow: str = Field(description="Task workflow name (row)")
    worker_id: str = Field(description="Client identifier (column)")
    state: CapabilityState
    evidence: str = Field(default="", description="Why this state was assigned")
    reward_evidence: float | None = Field(
        default=None, description="Trial reward that produced this state"
    )
    last_updated_round: int = Field(default=0)


class CapabilityMatrix(BaseModel):
    """
    Corresponds to C^t in the paper.

    Paper Section 4.2.1:
        'Each row is a task workflow… Each column is a client.
         Each cell in C^t records how well a client has mastered a specific
         workflow, assigning one of four states: covered, absorbing, broken, or gap.
         A workflow row is retired only when every client's cell becomes covered.'
    """

    round_idx: int
    family_name: str
    cells: list[CapabilityMatrixCell] = Field(default_factory=list)

    def get_state(self, task_workflow: str, worker_id: str) -> CapabilityState | None:
        for cell in self.cells:
            if cell.task_workflow == task_workflow and cell.worker_id == worker_id:
                return cell.state
        return None

    def is_workflow_fully_covered(self, task_workflow: str) -> bool:
        """'A workflow row is retired only when every client's cell becomes covered.'"""
        relevant = [c for c in self.cells if c.task_workflow == task_workflow]
        if not relevant:
            return False
        return all(c.state == CapabilityState.COVERED for c in relevant)

    def get_open_cells_for_worker(self, worker_id: str) -> list[CapabilityMatrixCell]:
        """Return cells where worker has gap, broken, or absorbing state."""
        return [
            c for c in self.cells
            if c.worker_id == worker_id and c.state != CapabilityState.COVERED
        ]


class HighLevelMemory(BaseModel):
    """
    Shared, family-level capability memory.

    Paper Section 4.2.1:
        'The high-level memory is shared across the family: it summarizes
         global observations such as which workflows remain universally unsolved,
         orienting every client toward a common task frontier.'
    """

    family_name: str
    content: str = Field(default="# High-Level Memory\n\nNo observations yet.\n")
    last_updated_round: int = Field(default=-1)


class LowLevelMemory(BaseModel):
    """
    Per-client private capability memory, keyed by profile ρ_i.

    Paper Section 4.2.1:
        'The low-level memory is private to each client and keyed by its
         profile ρ_i. It accumulates model-specific failure modes abstracted
         across rounds, such as a frequent misuse of a specific tool.'

    Paper Fidelity Audit (Phase15 P1-2): "keyed by ρ_i" means workers sharing
    the same (backbone_model, agent_harness) profile share ONE bucket, not
    one bucket per worker_id. ``profile_key`` (= WorkerProfile.profile_hash)
    is the actual storage key used by EvolutionMemoryStore; ``worker_id`` is
    kept as the representative/first worker for audit readability, and
    ``shared_worker_ids`` lists every worker currently routed to this bucket.
    """

    worker_id: str
    profile_key: str = Field(
        default="",
        description="WorkerProfile.profile_hash — the ρ_i equivalence-class key "
                    "this memory bucket is actually stored/looked-up under.",
    )
    shared_worker_ids: list[str] = Field(
        default_factory=list,
        description="All worker_ids currently sharing this bucket (same ρ_i).",
    )
    backbone_model: str
    agent_harness: str
    content: str = Field(default="# Per-Worker Memory\n\nNo observations yet.\n")
    last_updated_round: int = Field(default=-1)


class Directive(BaseModel):
    """
    One actionable directive produced in Stage 1 Evolution Planning.

    Paper Section 4.2.1 (Evolution Directives):
        'A directive targets an open cell (a gap, broken, or absorbing workflow)
         and prescribes how to close it: which skill to add, repair, or refactor,
         and the supporting evidence.'
    """

    target_worker_id: str
    workflow_name: str = Field(description="Which task workflow this directive addresses")
    action: PaperMergeAction | SkipUpdate
    priority: int = Field(default=1, ge=1, le=5, description="1 = lowest, 5 = highest")
    reason: str = Field(description="Evidence-backed justification")
    source_worker_id: str | None = Field(
        default=None,
        description="Peer whose patch this directive draws from (if any)",
    )
    source_reward: float | None = Field(default=None)
    style_warning: str | None = Field(
        default=None,
        description="E.g. 'refactor needed: qwen cannot parse verbose GLM-style prose'",
    )


# ---------------------------------------------------------------------------
# Evolution plan  (P^t)
# ---------------------------------------------------------------------------


class EvolutionPlan(BaseModel):
    """
    Corresponds to P^t in the paper.

    Paper Section 4.2.1:
        'an evolution plan P^t with three components:
         a capability matrix locating each client within a specific kind of tasks,
         a two-level memory recording capability boundaries,
         and directives prescribing next steps.'
    """

    round_idx: int
    family_name: str
    capability_matrix: CapabilityMatrix
    high_level_memory: HighLevelMemory
    low_level_memories: dict[str, LowLevelMemory] = Field(
        default_factory=dict, description="{worker_id: LowLevelMemory}"
    )
    directives: list[Directive] = Field(default_factory=list)

    def get_directives_for(self, worker_id: str) -> list[Directive]:
        return sorted(
            [d for d in self.directives if d.target_worker_id == worker_id],
            key=lambda d: -d.priority,
        )


# ---------------------------------------------------------------------------
# Merged patch  (Δ_i^t) — server output
# ---------------------------------------------------------------------------


class MergedPatch(BaseModel):
    """
    Corresponds to Δ_i^t in the paper.

    Paper Section 4.2.2:
        'generates the personalized update Δ_i^t for each client,
         yielding L_i^{t+1} = Apply(L_i^t, Δ_i^t)'
    """

    worker_id: str
    round_idx: int = Field(ge=0)
    upserts: dict[str, str] = Field(default_factory=dict)
    deletions: list[str] = Field(default_factory=list)
    summary: str = Field(default="")
    cost_usd: float = Field(default=0.0, ge=0.0)

    @field_validator("upserts")
    @classmethod
    def _validate_upserts(cls, v: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for path, content in v.items():
            safe = validate_safe_rel_path(path)
            if safe is None:
                raise ValueError(f"Unsafe path in MergedPatch.upserts: {path!r}")
            if content and content != "<binary>":
                cleaned[safe] = content
        return cleaned

    @field_validator("deletions")
    @classmethod
    def _validate_deletions(cls, v: list[str]) -> list[str]:
        cleaned = []
        for path in v:
            safe = validate_safe_rel_path(str(path))
            if safe is None:
                raise ValueError(f"Unsafe path in MergedPatch.deletions: {path!r}")
            cleaned.append(safe)
        return cleaned


# ---------------------------------------------------------------------------
# Audit log  (Decision Log)
# ---------------------------------------------------------------------------


class DecisionLog(BaseModel):
    """
    Auditable record produced after Stage 2 per-client library evolution.

    Paper Section 4.2.2:
        'auditable decision log that records the source patch, reward,
         and justification for every modified path'

    """

    worker_id: str
    round_idx: int
    action: PaperMergeAction | SkipUpdate
    source_worker_id: str | None = Field(default=None)
    affected_files: list[str] = Field(default_factory=list)
    reward: float = Field(default=0.0)
    reason: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    before_content_preview: str | None = Field(
        default=None, description="First 200 chars of the file before the change"
    )
    after_content_preview: str | None = Field(
        default=None, description="First 200 chars of the file after the change"
    )
    family_id: str | None = Field(
        default=None, description="[EXTENSION] Task family this decision belongs to (audit-only)"
    )
    task_id: str | None = Field(
        default=None, description="[EXTENSION] Task id executed this round by worker_id (audit-only)"
    )
    directive_id: str | None = Field(
        default=None,
        description=(
            "[EXTENSION] Stable id distinguishing multiple directives targeting the "
            "same (family_id, round_idx, worker_id) this round, e.g. "
            "'round_2_worker_u0_directive_0' (audit-only, not used in any decision)."
        ),
    )


# ---------------------------------------------------------------------------
# Round-level summary
# ---------------------------------------------------------------------------


class RoundRecord(BaseModel):
    """Aggregated record of one complete federation round."""

    round_idx: int
    family_name: str
    assignments: list[tuple[str, str]] = Field(
        default_factory=list,
        description="[(worker_id, task_name), …] — populated by caller from trajectory metadata",
    )
    worker_patches: dict[str, WorkerPatch] = Field(
        default_factory=dict,
        description="{worker_id: WorkerPatch} submitted this round",
    )
    evolution_plan: EvolutionPlan | None = Field(default=None)
    merged_patches: dict[str, MergedPatch] = Field(
        default_factory=dict, description="{worker_id: MergedPatch}"
    )
    decision_logs: list[DecisionLog] = Field(default_factory=list)
    rewards: dict[str, float] = Field(
        default_factory=dict, description="{worker_id: reward}"
    )
    elapsed_seconds: float = Field(default=0.0)

    @property
    def mean_reward(self) -> float | None:
        if not self.rewards:
            return None
        return sum(self.rewards.values()) / len(self.rewards)
