"""llm package — 公开接口重导出"""

from llm.backbone import LLMBackbone, BackboneCallResult, BackboneStats, resolve_litellm_model
from llm.retry import ErrorBucket, RetryConfig, classify_error, compute_backoff_sleep
from llm.router import BackboneRouter, make_single_worker_router
from llm.json_parser import safe_parse_json, extract_json_or_none
from llm.prompt_builder import DistillerPromptBuilder
from llm.generate import generate, GenerateResult
from llm.providers import (
    ProviderConfig, PROVIDERS, get_provider, available_providers,
    resolve_provider_for_model, MODEL_QWEN, MODEL_GLM, MODEL_KIMI, MODEL_SERVER,
    DASHSCOPE_ANTHROPIC_BASE, MOONSHOT_ANTHROPIC_BASE,
    setting1_self_evolve_workers, setting2_homo_fed_workers,
    setting3_hetero_backbone_workers, setting4_full_hetero_workers,
)

__all__ = [
    "LLMBackbone", "BackboneCallResult", "BackboneStats", "resolve_litellm_model",
    "ErrorBucket", "RetryConfig", "classify_error", "compute_backoff_sleep",
    "BackboneRouter", "make_single_worker_router",
    "safe_parse_json", "extract_json_or_none",
    "DistillerPromptBuilder",
    # Providers
    "ProviderConfig", "PROVIDERS", "get_provider", "available_providers",
    "resolve_provider_for_model",
    "MODEL_QWEN", "MODEL_GLM", "MODEL_KIMI", "MODEL_SERVER",
    "DASHSCOPE_ANTHROPIC_BASE", "MOONSHOT_ANTHROPIC_BASE",
    "setting1_self_evolve_workers", "setting2_homo_fed_workers",
    "setting3_hetero_backbone_workers", "setting4_full_hetero_workers",
    # Phase13: unified generate(model, prompt, json_mode) interface
    "generate", "GenerateResult",
]
