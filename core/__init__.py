"""core package — public re-exports for convenience."""

from core.constants import (
    K_STEP,
    K_OBS,
    TRUNCATION_MARKER,
    SKILL_FILENAME,
    ALLOWED_SKILL_SUBDIRS,
    DEFAULT_TEMPERATURE,
    DEFAULT_MAX_TOKENS,
)
from core.datatypes import (
    # 身份标识
    WorkerProfile,
    # 轨迹 (τ_i / B_i^t)
    TrajectoryStep,
    Trajectory,
    TrajectoryBuffer,
    CompactedTrajectory,
    # 试验结果
    TrialOutcome,
    # 技能库
    LibraryFileEntry,
    LibraryDigest,
    LibrarySnapshot,
    # 补丁 δ_i^t
    WorkerPatch,
    # 服务端
    CapabilityState,
    PaperMergeAction,
    SkipUpdate,
    CapabilityMatrixCell,
    CapabilityMatrix,
    HighLevelMemory,
    LowLevelMemory,
    Directive,
    EvolutionPlan,
    MergedPatch,
    DecisionLog,
    RoundRecord,
    # 工具函数
    validate_safe_rel_path,
)
from core.exceptions import (
    FederatedSkillError,
    PatchDistillationError,
    PatchValidationError,
    LibraryError,
    LibraryValidationError,
    LLMCallError,
    LLMRateLimitError,
    LLMEmptyResponseError,
    LLMJSONParseError,
    TrajectoryError,
    ServerPlanningError,
    ServerEvolutionError,
    BenchmarkError,
    TaskLoadError,
    VerificationError,
    TaskExecutionError,
)

__all__ = [
    # constants
    "K_STEP", "K_OBS", "TRUNCATION_MARKER", "SKILL_FILENAME",
    "ALLOWED_SKILL_SUBDIRS", "DEFAULT_TEMPERATURE", "DEFAULT_MAX_TOKENS",
    # datatypes
    "WorkerProfile",
    "TrajectoryStep", "Trajectory", "CompactedTrajectory",
    "TrialOutcome",
    "LibraryFileEntry", "LibraryDigest", "LibrarySnapshot",
    "WorkerPatch",
    "CapabilityState", "PaperMergeAction", "SkipUpdate",
    "CapabilityMatrixCell", "CapabilityMatrix",
    "HighLevelMemory", "LowLevelMemory",
    "Directive", "EvolutionPlan",
    "MergedPatch", "DecisionLog", "RoundRecord",
    "validate_safe_rel_path",
    # exceptions
    "FederatedSkillError",
    "PatchDistillationError", "PatchValidationError",
    "LibraryError", "LibraryValidationError",
    "LLMCallError", "LLMRateLimitError", "LLMEmptyResponseError", "LLMJSONParseError",
    "TrajectoryError",
    "ServerPlanningError", "ServerEvolutionError",
    "BenchmarkError", "TaskLoadError", "VerificationError",
    "TaskExecutionError",
]
