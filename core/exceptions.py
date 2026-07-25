"""
exceptions.py — Custom exception hierarchy for FederatedSkill reproduction.

Hierarchy:
  FederatedSkillError
  ├── PatchDistillationError
  │     └── PatchValidationError
  ├── LibraryError
  │     └── LibraryValidationError
  ├── LLMCallError
  │     ├── LLMRateLimitError
  │     ├── LLMEmptyResponseError
  │     └── LLMJSONParseError
  ├── TrajectoryError
  ├── ServerPlanningError
  ├── ServerEvolutionError
  ├── BenchmarkError
  │     ├── TaskLoadError
  │     └── VerificationError
  ├── TaskExecutionError
  └── ArtifactRecordingError
"""


class FederatedSkillError(Exception):
    """Base exception for all FederatedSkill errors."""


# ---------------------------------------------------------------------------
# Client-side: distillation
# ---------------------------------------------------------------------------


class PatchDistillationError(FederatedSkillError):
    """Raised when patch distillation fails (e.g., LLM produces unusable output)."""


class PatchValidationError(PatchDistillationError):
    """
    Raised when a WorkerPatch or MergedPatch fails structural validation.

    This includes:
    - Absolute paths or directory traversals in upserts/deletions
    - Malformed SKILL.md frontmatter
    - Schema validation failures from Pydantic
    """


class PatchDistillationFailure(PatchDistillationError):
    """
    Experiment Integrity Hardening TASK1：Patch Distillation Fail-Loud。

    """

    def __init__(
        self,
        worker_id: str,
        round_idx: int,
        task_id: str,
        original_error: Exception,
    ) -> None:
        self.worker_id = worker_id
        self.round_idx = round_idx
        self.task_id = task_id
        self.original_error = original_error
        super().__init__(
            f"Patch distillation 失败: worker_id={worker_id!r} round={round_idx} "
            f"task_id={task_id!r} 原因={type(original_error).__name__}: {original_error}"
        )


# ---------------------------------------------------------------------------
# Skill library
# ---------------------------------------------------------------------------


class LibraryError(FederatedSkillError):
    """Base exception for skill library operations."""


class LibraryValidationError(LibraryError):
    """Raised when library validation finds structural issues."""


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------


class LLMCallError(FederatedSkillError):
    """Raised when an LLM API call fails after all retries."""


class LLMRateLimitError(LLMCallError):
    """Rate limit exhausted after all backoff retries."""


class LLMEmptyResponseError(LLMCallError):
    """Provider returned 200 but the message content is empty or malformed."""


class LLMJSONParseError(LLMCallError):
    """Cannot extract valid JSON from the LLM response."""


# ---------------------------------------------------------------------------
# Trajectory processing
# ---------------------------------------------------------------------------


class TrajectoryError(FederatedSkillError):
    """Raised when trajectory processing fails (missing fields, corrupt data)."""


# ---------------------------------------------------------------------------
# Server-side planning / evolution
# ---------------------------------------------------------------------------


class ServerPlanningError(FederatedSkillError):
    """Raised when Stage-1 evolution planning fails."""


class ServerEvolutionError(FederatedSkillError):
    """Raised when Stage-2 per-client library evolution fails."""


# ---------------------------------------------------------------------------
# Benchmark layer  (Section 5 实验系统)
# ---------------------------------------------------------------------------


class BenchmarkError(FederatedSkillError):
    """Base exception for benchmark/task system errors."""


class TaskLoadError(BenchmarkError):
    """Raised when a task file cannot be loaded or parsed (malformed JSON/YAML)."""


class VerificationError(BenchmarkError):
    """
    Raised when the verifier encounters an unexpected error (not a normal test failure).

    Note: A normal test failure returns VerificationResult(reward=0.0, success=False)
    and does NOT raise this exception. This is only raised for verifier infrastructure
    failures (e.g., subprocess crash, missing entry_point, internal JSON parse error).
    """


# ---------------------------------------------------------------------------
# Task execution  (Section 4.1.1 Trial Execution)
# ---------------------------------------------------------------------------


class TaskExecutionError(FederatedSkillError):
    """
    Raised when the TaskExecutor cannot complete the trial pipeline.

    Corresponds to Section 4.1.1:
        'The local agent is conditioned on its current skill library L_i^t
         and its specific LLM backbone... generates an execution trajectory τ.'

    This covers LLM call failures, code extraction failures, and
    unrecoverable subprocess errors during execution.
    """


# ---------------------------------------------------------------------------
# Evaluation layer: artifact recording  [EXTENSION, engineering-only]
# ---------------------------------------------------------------------------


class ArtifactRecordingError(FederatedSkillError):
    """
    FederatedSkill Artifact Fidelity Hardening TASK2。

    核心审计 artifact（例如 capability_matrix.jsonl）记录/落盘失败时抛出。
    取代此前 `experiments/federated.py::FederatedRunner._run_round()` 里
    "记 debug 日志、静默吞掉异常、继续实验"的行为——真实实验
    （`strict_artifact_mode=True`，默认）下该失败必须让实验中止，避免论文
    结果被静默污染成"实验正常跑完，但某个 artifact 其实从未被记录"的假象。
    `strict_artifact_mode=False`（仅供 mock/调试场景使用）时改为记一条
    warning 并继续，不抛出本异常。

    不影响 Evolution Agent 决策逻辑、Capability Matrix 状态转移、Merge
    Action 本身——只影响"记录这些已产出结果的旁路审计代码"的错误处理方式。
    """

    def __init__(self, artifact_name: str, round_idx: int, original_error: Exception) -> None:
        self.artifact_name = artifact_name
        self.round_idx = round_idx
        self.original_error = original_error
        super().__init__(
            f"核心 artifact 记录失败: artifact={artifact_name!r} round={round_idx} "
            f"原因={type(original_error).__name__}: {original_error}"
        )
