"""
constants.py — All hard-coded hyper-parameters for FederatedSkill reproduction.

Every constant here traces back to either:
  - A paper equation / footnote  (e.g. K_step, K_obs, Eq.4)
  - The official reference code   (e.g. max_tokens, patcher temperature)
  - Standard engineering defaults  (e.g. retry counts)
"""

# ---------------------------------------------------------------------------
# Paper Section 4.1.2 – Patch Distillation compaction parameters
# ---------------------------------------------------------------------------

#: Maximum agentic steps kept in the compacted trajectory:
#: initial step + (K_STEP − 1) most-recent steps.
K_STEP: int = 20

#: Maximum characters per observation before appending TRUNCATION_MARKER.
K_OBS: int = 3_000

#: Marker appended to truncated observations (matches official patcher).
TRUNCATION_MARKER: str = "<truncated>"

# ---------------------------------------------------------------------------
# Paper Section 4.1.2 – Patch schema constraints
# ---------------------------------------------------------------------------

#: Soft cap on SKILL.md line count (from merge_skill/scripts/validate_skill_md.py).
MAX_SKILL_MD_LINES: int = 500

#: Hard cap on skills per task family (from SKILL.md merger rules).
MAX_SKILLS_PER_FAMILY: int = 4

#: Allowed sub-directories inside a skill directory.
ALLOWED_SKILL_SUBDIRS: frozenset[str] = frozenset({"scripts", "references", "assets"})

#: Required file name for each skill directory.
SKILL_FILENAME: str = "SKILL.md"

# ---------------------------------------------------------------------------
# LLM call parameters (match official patcher_bridge.py defaults)
# ---------------------------------------------------------------------------

#: Default sampling temperature for the patcher LLM call.
DEFAULT_TEMPERATURE: float = 0.2

#: Kimi/Moonshot rejects temperature < 1.0 – override for that provider.
MOONSHOT_TEMPERATURE: float = 1.0

#: Default max generation tokens for the patcher (matches upstream constant).
DEFAULT_MAX_TOKENS: int = 8_192

#: Default max tokens for the server-side evolution agent (Stage 1 + Stage 2).
MERGER_MAX_TOKENS: int = 16_384

# ---------------------------------------------------------------------------
# Retry / resilience
# ---------------------------------------------------------------------------

#: Total attempts before giving up on a single LLM call.
MAX_RETRY_ATTEMPTS: int = 20

#: Base sleep seconds for exponential backoff (rate-limit retries).
RETRY_BASE_SLEEP: float = 5.0

#: Upper bound on sleep seconds.
RETRY_MAX_SLEEP: float = 300.0

#: Bounded retries for transient network errors (not rate limits).
#: Matches official llm_client.py default: SKILLFL_LLM_TRANSIENT_MAX_RETRIES=20.
TRANSIENT_MAX_RETRIES: int = 20

# ---------------------------------------------------------------------------
# Prompt / context budget
# ---------------------------------------------------------------------------

#: Maximum characters allowed for the library snapshot JSON in the distiller prompt.
#: Prevents context overflow. SKILL.md files are prioritised over scripts.
MAX_LIBRARY_PROMPT_CHARS: int = 20_000

#: Maximum characters allowed for the compacted trajectory in the prompt.
MAX_TRAJECTORY_PROMPT_CHARS: int = 12_000
