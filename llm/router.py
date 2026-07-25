"""
router.py — 联邦 worker 的 backbone 路由表

解决论文核心异构性问题：每个 worker ρ_i 使用**自己的** backbone m_i 蒸馏 patch，
而非共用一个 server 端模型。

对应论文 Section 4.1.2：
    'The patcher executes via a single LLM call utilizing the client's
     native backbone — the same model used for task execution.'

原版在 patcher_bridge.py 的 PatcherBridge.__init__ 中逐 worker 构造 evolvers 字典；
本版将路由逻辑独立为 BackboneRouter，可被 distiller / server evolution agent 复用。
"""

from __future__ import annotations

import os
from typing import Sequence

from core.constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
from core.datatypes import WorkerProfile
from llm.backbone import LLMBackbone, RetryConfig


class BackboneRouter:
    """
    Worker ID → LLMBackbone 的路由表。

    职责：
      1. 为每个 worker 注册对应的 LLMBackbone（骨干模型 m_i）
      2. 路由 get(worker_id) → backbone（distiller 调用时使用）
      3. 工厂方法 from_profiles() 批量从 WorkerProfile 列表构建

    设计亮点（与原版不同）：
      - 路由表和 backbone 创建分离，便于单测时注入 mock backbone
      - fallback_backbone 处理没有注册的 worker（如 server 侧调用）
      - 支持动态 register()，适应 round-by-round 动态加入的 worker
    """

    def __init__(
        self,
        fallback_backbone: LLMBackbone | None = None,
    ) -> None:
        """
        Args:
            fallback_backbone: 当 worker_id 未注册时使用的默认 backbone。
                               通常是 server 端模型（如 glm-5 / claude-opus）。
                               None → 未注册的 worker 调用 get() 时抛出 KeyError。
        """
        self._routes: dict[str, LLMBackbone] = {}
        self._profiles: dict[str, WorkerProfile] = {}
        self._fallback = fallback_backbone

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def from_profiles(
        cls,
        profiles: Sequence[WorkerProfile],
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        retry_config: RetryConfig | None = None,
        fallback_backbone: LLMBackbone | None = None,
    ) -> "BackboneRouter":
        """
        从 WorkerProfile 列表批量构建路由表。

        对应论文实验配置：每个 worker 在 configs/*.yaml 中声明自己的
        model_name / api_base / api_key_env，这里统一读取并创建 backbone。

        Args:
            profiles:          所有 worker 的 ρ_i 列表
            temperature:       默认采样温度（Moonshot 会被自动提升到 ≥ 1.0）
            max_tokens:        最大生成 token 数
            retry_config:      重试策略（None → 所有 worker 共用默认值）
            fallback_backbone: 未注册 worker 的兜底 backbone
        """
        router = cls(fallback_backbone=fallback_backbone)
        for profile in profiles:
            backbone = LLMBackbone.from_worker_profile(
                profile,
                temperature=temperature,
                max_tokens=max_tokens,
                retry_config=retry_config,
            )
            router.register(profile.client_id, backbone)
            router.register_profile(profile.client_id, profile)
        return router

    @classmethod
    def from_config_dicts(
        cls,
        worker_configs: dict[str, dict],
        retry_config: RetryConfig | None = None,
    ) -> "BackboneRouter":
        """
        从 YAML 配置字典（worker_id → {model_name, api_base, api_key_env, ...}）
        批量构建路由表。

        兼容原版 configs/*.yaml 中 patcher 块的格式：
        ```yaml
        workers:
          - id: u0
            model_name: qwen3.6-plus
            api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
            api_key_env: DASHSCOPE_KEY
        ```
        """
        from llm.backbone import resolve_litellm_model

        router = cls()
        for wid, cfg in worker_configs.items():
            model_name = cfg.get("model_name") or cfg.get("model", "")
            if not model_name:
                raise ValueError(
                    f"worker_configs[{wid!r}] 缺少 'model_name' 字段"
                )
            api_base = cfg.get("api_base")
            api_key_env = cfg.get("api_key_env", "")
            api_key = cfg.get("api_key") or (
                os.environ.get(api_key_env, "") if api_key_env else ""
            ) or None
            temperature = float(cfg.get("temperature", DEFAULT_TEMPERATURE))
            max_tokens = int(cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
            provider = cfg.get("provider", "")

            litellm_model = resolve_litellm_model(model_name, api_base)
            backbone = LLMBackbone(
                litellm_model=litellm_model,
                api_key=api_key,
                api_base=api_base,
                temperature=temperature,
                max_tokens=max_tokens,
                retry_config=retry_config,
            )
            router.register(wid, backbone)
        return router

    # ------------------------------------------------------------------
    # 路由操作
    # ------------------------------------------------------------------

    def register(self, worker_id: str, backbone: LLMBackbone) -> None:
        """注册一个 worker 的 backbone。支持覆盖（round 中途换模型场景）。"""
        self._routes[worker_id] = backbone

    def register_profile(self, worker_id: str, profile: WorkerProfile) -> None:
        """
        注册一个 worker 的 WorkerProfile（ρ_i），供仅持有 worker_id 的调用方
        （如 PatchDistiller.distill(profile=None)）反查完整 profile 使用。

        `from_profiles()` 会自动调用本方法；`from_config_dicts()` 和
        手动 `register()` 不会自动注册 profile，需要时可显式调用。
        """
        self._profiles[worker_id] = profile

    def get_profile(self, worker_id: str) -> WorkerProfile | None:
        """
        按 worker_id 查询已注册的 WorkerProfile。

        Returns:
            对应的 WorkerProfile；未注册时返回 None（调用方需自行处理缺省逻辑）。
        """
        return self._profiles.get(worker_id)

    def get(self, worker_id: str) -> LLMBackbone:
        """
        获取 worker_id 对应的 backbone。

        Raises:
            KeyError: worker_id 未注册且没有 fallback
        """
        backbone = self._routes.get(worker_id)
        if backbone is not None:
            return backbone
        if self._fallback is not None:
            return self._fallback
        registered = list(self._routes.keys())
        raise KeyError(
            f"worker {worker_id!r} 未在 BackboneRouter 中注册。"
            f"已注册的 worker: {registered}"
        )

    def list_workers(self) -> list[str]:
        """返回所有已注册的 worker ID。"""
        return list(self._routes.keys())

    def has_worker(self, worker_id: str) -> bool:
        return worker_id in self._routes

    def __len__(self) -> int:
        return len(self._routes)

    def __repr__(self) -> str:
        workers = list(self._routes.keys())
        return f"BackboneRouter(workers={workers}, has_fallback={self._fallback is not None})"


# ---------------------------------------------------------------------------
# 便捷函数：单 worker 快速构造
# ---------------------------------------------------------------------------


def make_single_worker_router(
    worker_id: str,
    profile: WorkerProfile,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> BackboneRouter:
    """
    为单个 worker 快速构造 router，适合 Setting 1（Self-Evolve baseline）。
    """
    return BackboneRouter.from_profiles(
        profiles=[profile],
        temperature=temperature,
        max_tokens=max_tokens,
    )
