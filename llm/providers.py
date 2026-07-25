"""
providers.py — LLM provider registry（对应官方 .env.example + configs/*.yaml 的端点配置）

论文 Section 5.1 实验使用的三个 provider：
  - DashScope (Anthropic-compatible)：qwen3.6-plus, glm-5
  - Moonshot (Anthropic-compatible)：kimi-k2.5
  - Anthropic (native)：claude-* (可选)

API Provider 密钥隔离配置（Provider Key Isolation Fix）：
  Qwen3.6-Plus 与 GLM-5 虽然同用 DashScope 平台，但可能是两个不同的
  DashScope 账户/App Key，不应共用同一个环境变量——否则无法独立轮换/限流/
  审计两个模型的调用。因此本注册表为每个 model 单独分配一个环境变量名：

      model            -> provider                  -> environment variable
      qwen3.6-plus     -> DashScope (compatible)     -> QWEN_DASHSCOPE_API_KEY
      glm-5            -> DashScope (compatible)      -> GLM_DASHSCOPE_API_KEY
      kimi-k2.5        -> Moonshot                    -> MOONSHOT_API_KEY
      claude-code CLI  -> Anthropic Gateway (CLI 直连) -> ANTHROPIC_BASE_URL /
                          ANTHROPIC_AUTH_TOKEN
                          （不经过本文件，不经过 Python LLM API，由
                          harness/claude_code_harness.py 直接把这两个变量
                          传给真实 claude CLI 子进程，见 Part4）。

  为不打断既有流程/旧测试/旧 configs，PROVIDERS 里旧的共享环境变量名
  （DASHSCOPE_KEY / MOONSHOT_KEY）保留为 fallback：优先读新的拆分名，
  新名未设时再回退到旧的共享名（ProviderConfig.api_key 属性）。

环境变量读取优先级：
  QWEN_DASHSCOPE_API_KEY -> （fallback）DASHSCOPE_KEY
  GLM_DASHSCOPE_API_KEY  -> （fallback）DASHSCOPE_KEY
  MOONSHOT_API_KEY       -> （fallback）MOONSHOT_KEY
  ANTHROPIC_API_KEY      → Anthropic native key（可选，未在论文 Setting1-4 中使用）
  OPENAI_API_KEY         → OpenAI key（Setting 4 backup）

工厂函数 make_backbone_for_experiment() 直接对应论文四种实验设置的 worker 配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

# 自动加载 .env 文件（python-dotenv 未安装时静默跳过）
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Provider 常量
# ---------------------------------------------------------------------------

# DashScope — Anthropic-compatible endpoint（论文 qwen3.6-plus / glm-5）
DASHSCOPE_ANTHROPIC_BASE = "https://dashscope.aliyuncs.com/apps/anthropic"
# DashScope — OpenAI-compatible endpoint（qwen-code native 模式）
DASHSCOPE_OPENAI_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# Moonshot — Anthropic-compatible endpoint（论文 kimi-k2.5）
MOONSHOT_ANTHROPIC_BASE = "https://api.moonshot.ai/anthropic"
# Moonshot — OpenAI-compatible endpoint
MOONSHOT_OPENAI_BASE = "https://api.moonshot.ai/v1"
# Anthropic native
ANTHROPIC_NATIVE_BASE = "https://api.anthropic.com"

# 论文中使用的三个 backbone 模型名称（与官方 configs/*.yaml 保持一致）
MODEL_QWEN = "qwen3.6-plus"
MODEL_GLM = "glm-5"
MODEL_KIMI = "kimi-k2.5"

# GLM-5 作为服务器端 evolution agent（论文 Section 6 Implementation 段）
MODEL_SERVER = "glm-5"

# Moonshot 温度下限（kimi 拒绝 temperature < 1.0，与官方 patcher_bridge.py 一致）
MOONSHOT_MIN_TEMPERATURE = 1.0

# --- API Provider 密钥隔离配置：每个 model 专属的环境变量名 ------------------
# 新的主名（优先读取）
QWEN_DASHSCOPE_API_KEY_ENV = "QWEN_DASHSCOPE_API_KEY"
GLM_DASHSCOPE_API_KEY_ENV = "GLM_DASHSCOPE_API_KEY"
MOONSHOT_API_KEY_ENV = "MOONSHOT_API_KEY"
# 旧的共享名（fallback，不再作为默认推荐值，仅为向后兼容保留）
_LEGACY_DASHSCOPE_KEY_ENV = "DASHSCOPE_KEY"
_LEGACY_MOONSHOT_KEY_ENV = "MOONSHOT_KEY"


# ---------------------------------------------------------------------------
# ProviderConfig：描述一个 provider 的端点 + 密钥来源
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderConfig:
    """
    一个 LLM provider 的运行时配置。

    Attributes:
        api_base:       API 端点 URL（传给 litellm api_base）
        api_key_env:    环境变量名，运行时从 os.environ 优先读取
        fallback_key_envs: 当 api_key_env 未设时依次尝试的旧环境变量名
                        （向后兼容，不需要则留空元组）。仅用于过渡，新代码/
                        新 config 不要依赖它。
        litellm_prefix: litellm 路由前缀（"openai" / "anthropic"）
        min_temperature: 该 provider 的温度下限（Moonshot=1.0，其余=0.0）
    """
    api_base: str
    api_key_env: str
    fallback_key_envs: tuple[str, ...] = ()
    litellm_prefix: str = "openai"
    min_temperature: float = 0.0

    @property
    def api_key(self) -> str | None:
        for env_name in (self.api_key_env, *self.fallback_key_envs):
            value = os.environ.get(env_name)
            if value:
                return value
        return None

    @property
    def available(self) -> bool:
        return bool(self.api_key)


# ---------------------------------------------------------------------------
# 内置 provider 注册表
# ---------------------------------------------------------------------------

#: 全部支持的 provider 配置。Qwen 与 GLM 同属 DashScope 平台，但使用
#: 独立的环境变量名（QWEN_DASHSCOPE_API_KEY / GLM_DASHSCOPE_API_KEY），
#: 不共用同一个 key。
PROVIDERS: dict[str, ProviderConfig] = {
    # DashScope（Qwen3.6-Plus 专用）
    "dashscope_qwen_anthropic": ProviderConfig(
        api_base=DASHSCOPE_ANTHROPIC_BASE,
        api_key_env=QWEN_DASHSCOPE_API_KEY_ENV,
        fallback_key_envs=(_LEGACY_DASHSCOPE_KEY_ENV,),
        litellm_prefix="anthropic",
    ),
    "dashscope_qwen_openai": ProviderConfig(
        api_base=DASHSCOPE_OPENAI_BASE,
        api_key_env=QWEN_DASHSCOPE_API_KEY_ENV,
        fallback_key_envs=(_LEGACY_DASHSCOPE_KEY_ENV,),
        litellm_prefix="openai",
    ),
    # DashScope（GLM-5 专用，仍是 DashScope compatible routing，不是 Zhipu 原生端点）
    "dashscope_glm_anthropic": ProviderConfig(
        api_base=DASHSCOPE_ANTHROPIC_BASE,
        api_key_env=GLM_DASHSCOPE_API_KEY_ENV,
        fallback_key_envs=(_LEGACY_DASHSCOPE_KEY_ENV,),
        litellm_prefix="anthropic",
    ),
    "dashscope_glm_openai": ProviderConfig(
        api_base=DASHSCOPE_OPENAI_BASE,
        api_key_env=GLM_DASHSCOPE_API_KEY_ENV,
        fallback_key_envs=(_LEGACY_DASHSCOPE_KEY_ENV,),
        litellm_prefix="openai",
    ),
    # Moonshot via Anthropic-compatible endpoint（kimi workers）
    "moonshot_anthropic": ProviderConfig(
        api_base=MOONSHOT_ANTHROPIC_BASE,
        api_key_env=MOONSHOT_API_KEY_ENV,
        fallback_key_envs=(_LEGACY_MOONSHOT_KEY_ENV,),
        litellm_prefix="anthropic",
        min_temperature=MOONSHOT_MIN_TEMPERATURE,
    ),
    # Moonshot via OpenAI-compatible endpoint（kimi-cli native）
    "moonshot_openai": ProviderConfig(
        api_base=MOONSHOT_OPENAI_BASE,
        api_key_env=MOONSHOT_API_KEY_ENV,
        fallback_key_envs=(_LEGACY_MOONSHOT_KEY_ENV,),
        litellm_prefix="openai",
        min_temperature=MOONSHOT_MIN_TEMPERATURE,
    ),
    # Anthropic native（可选备用，不在论文 Setting1-4 范围内）
    "anthropic_native": ProviderConfig(
        api_base=ANTHROPIC_NATIVE_BASE,
        api_key_env="ANTHROPIC_API_KEY",
        litellm_prefix="anthropic",
    ),
}

# 向后兼容别名（旧代码/旧测试可能仍引用这两个旧 key；指向 GLM/Qwen 目前共用的
# DashScope compatible 端点，行为与 Provider Key Isolation Fix 之前完全一致，
# 仅用于过渡，新代码请直接用上面按模型区分的 key）。
PROVIDERS["dashscope_openai"] = PROVIDERS["dashscope_qwen_openai"]
PROVIDERS["dashscope_anthropic"] = PROVIDERS["dashscope_qwen_anthropic"]


def get_provider(name: str) -> ProviderConfig:
    if name not in PROVIDERS:
        raise KeyError(f"未知 provider: {name!r}，可用: {list(PROVIDERS)}")
    return PROVIDERS[name]


def available_providers() -> list[str]:
    """返回当前环境中已配置 API key 的 provider 列表。"""
    return [name for name, cfg in PROVIDERS.items() if cfg.available]


# ---------------------------------------------------------------------------
# 模型 → provider 映射（对应论文实验设置）
# ---------------------------------------------------------------------------

#: 每个模型名 → 推荐 provider key。Qwen 与 GLM 现已拆分为各自的 provider key
#: （对应各自独立的环境变量），不再共用同一个 provider 条目。
MODEL_TO_PROVIDER: dict[str, str] = {
    MODEL_QWEN: "dashscope_qwen_openai",  # openai-compatible endpoint avoids ASCII encoding bug
    MODEL_GLM:  "dashscope_glm_openai",   # same: openai-compatible is safer for UTF-8 content
    MODEL_KIMI: "moonshot_openai",
    # glm-4 与 glm-5 同属 GLM 系列，共用 GLM_DASHSCOPE_API_KEY（不是 Qwen 的 key）：
    "glm-4":    "dashscope_glm_openai",
    # Fallback for OpenAI models
    "gpt-4.1":   "openai_native",
    "gpt-4o":    "openai_native",
}

# 默认 server backbone（论文使用 GLM-5 作为 merger agent）
# 使用 dashscope_glm_openai 而非 dashscope_anthropic：避免非 ASCII prompt/patch 内容触发
# litellm Anthropic SDK 路径下的 UnicodeEncodeError（与 client 侧修正 #14 保持一致），
# 并使用 GLM 专属的 GLM_DASHSCOPE_API_KEY（不是 Qwen 的 key）。
DEFAULT_SERVER_PROVIDER = "dashscope_glm_openai"
DEFAULT_SERVER_MODEL = MODEL_SERVER


def resolve_provider_for_model(model_name: str) -> ProviderConfig:
    """
    推断 model_name 对应的 provider 配置。

    策略：
      1. 精确匹配 MODEL_TO_PROVIDER
    2. 关键词匹配（kimi → moonshot_openai，qwen → dashscope_qwen_openai，
         glm → dashscope_glm_openai——Provider Key Isolation Fix 后不再合并成
         同一个分支，避免 Qwen/GLM 共用同一个环境变量）
      3. 无法识别的模型名不再静默兜底到 dashscope_qwen_openai（Experiment Integrity
         Hardening TASK5：消除隐藏 fallback，避免用未预期的 provider 悄悄发起
         真实 API 调用而不被发现），直接抛出 ValueError。
    """
    if model_name in MODEL_TO_PROVIDER:
        key = MODEL_TO_PROVIDER[model_name]
        if key in PROVIDERS:
            return PROVIDERS[key]
    lower = model_name.lower()
    if "kimi" in lower or "moonshot" in lower:
        return PROVIDERS["moonshot_openai"]
    if "glm" in lower:
        return PROVIDERS["dashscope_glm_openai"]
    if "qwen" in lower:
        return PROVIDERS["dashscope_qwen_openai"]
    if "claude" in lower:
        return PROVIDERS["anthropic_native"]
    # Experiment Integrity Hardening TASK5：无法识别的模型名必须显式报错，
    # 不能再静默默认为某个 provider（此前会导致未登记的模型名悄悄
    # 路由到错误的 provider，污染真实 API 实验结果且不留痕迹）。
    raise ValueError(
        f"未知模型名: {model_name!r}，无法自动推断 provider。"
        f"请在 MODEL_TO_PROVIDER 中登记该模型，或显式传入 provider_override。"
    )


# ---------------------------------------------------------------------------
# WorkerProfile 预设（对应论文四种实验设置）
# ---------------------------------------------------------------------------

def make_worker_profile(
    worker_id: str,
    model: str,
    harness: Literal["claude-code", "qwen-code", "kimi-cli"] = "claude-code",
    provider_override: str | None = None,
) -> dict:
    """
    构造一个 WorkerProfile 的 dict（供 experiments/configs/*.yaml 解析）。

    Args:
        worker_id:         e.g. "u0"
        model:             e.g. "qwen3.6-plus"
        harness:           agent CLI（论文使用的三种）
        provider_override: 强制指定 provider key；None 则自动推断

    Returns:
        可传给 WorkerProfile(**d) 的字典
    """
    from core.datatypes import WorkerProfile  # 避免循环导入

    provider_key = provider_override or MODEL_TO_PROVIDER.get(model, "dashscope_qwen_openai")
    provider = PROVIDERS.get(provider_key, PROVIDERS["dashscope_qwen_openai"])

    return dict(
        client_id=worker_id,
        backbone_model=model,
        agent_harness=harness,
        model_provider=provider_key.split("_")[0],  # e.g. "dashscope"
        api_base=provider.api_base,
        api_key_env=provider.api_key_env,
    )


# ---------------------------------------------------------------------------
# 四种实验设置的 worker profile 预设（对应论文 Table 1）
# ---------------------------------------------------------------------------

def setting1_self_evolve_workers() -> list[dict]:
    """Setting 1: Self-Evolve，单 worker，Qwen3.6-Plus（CC harness）。"""
    return [make_worker_profile("u0", MODEL_QWEN, "claude-code")]


def setting2_homo_fed_workers() -> list[dict]:
    """Setting 2: Homogeneous，3× GLM-5（CC harness）——论文 Figure 2。"""
    return [
        make_worker_profile("u0", MODEL_GLM, "claude-code"),
        make_worker_profile("u1", MODEL_GLM, "claude-code"),
        make_worker_profile("u2", MODEL_GLM, "claude-code"),
    ]


def setting3_hetero_backbone_workers() -> list[dict]:
    """Setting 3: Heterogeneous backbone（3 模型均用 CC harness）——论文 Table 1 左。"""
    return [
        make_worker_profile("u0", MODEL_QWEN, "claude-code"),
        make_worker_profile("u1", MODEL_GLM,  "claude-code"),
        make_worker_profile("u2", MODEL_KIMI, "claude-code",
                            provider_override="moonshot_anthropic"),
    ]


def setting4_full_hetero_workers() -> list[dict]:
    """Setting 4: Full heterogeneous（3 模型 + 3 CLI）——论文 Table 1 右。"""
    return [
        make_worker_profile("u0", MODEL_QWEN, "qwen-code",
                            provider_override="dashscope_qwen_openai"),
        make_worker_profile("u1", MODEL_GLM,  "claude-code"),
        make_worker_profile("u2", MODEL_KIMI, "kimi-cli",
                            provider_override="moonshot_openai"),
    ]
