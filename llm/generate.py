"""
generate.py — 统一 LLM 调用接口 generate(model, prompt, json_mode)

Phase13 任务2 要求：
    response = llm.generate(model, prompt, json_mode)
    统一支持 Qwen / GLM / Kimi / Claude 四种 backbone；
    记录 input_tokens / output_tokens / cost / latency。

设计原则（不重构已测试模块）：
    本文件不修改 llm/backbone.py、llm/router.py、llm/providers.py 中任何
    已测试的类/方法，只是在其上包一层「按 model 名称路由 + 统一命名字段」
    的薄封装：
      - backbone 路由复用 llm.providers.resolve_provider_for_model()
        （已支持 qwen/glm/kimi/claude 关键字识别）
      - 实际调用复用 llm.backbone.LLMBackbone.call() / call_json()
        （litellm 统一路由 + 已有重试逻辑）
      - GenerateResult 是全新的返回类型（不修改 BackboneCallResult），
        用 input_tokens/output_tokens 的论文常见命名包装
        BackboneCallResult 里已有的 prompt_tokens/completion_tokens，
        并在本模块内部用 time.monotonic() 测量 latency_seconds。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from core.constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
from llm.backbone import LLMBackbone, resolve_litellm_model
from llm.providers import resolve_provider_for_model


@dataclass
class GenerateResult:
    """
    统一 LLM 调用结果。

    字段命名对齐 Phase13 任务2 要求（input_tokens/output_tokens/cost/latency），
    而不是 llm.backbone.BackboneCallResult 内部使用的
    prompt_tokens/completion_tokens/cost_usd 命名——两套命名并存，
    互不冲突，本类型只在 generate() 的返回值里使用。
    """

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    latency: float = 0.0
    json_data: dict[str, Any] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _build_backbone(
    model: str,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> LLMBackbone:
    """
    按模型名称自动路由到 Qwen / GLM / Kimi / Claude 对应 provider，
    构造一个 LLMBackbone（不缓存/不注册到任何 router，调用方按需自行复用）。
    """
    provider = resolve_provider_for_model(model)
    resolved_api_base = api_base or provider.api_base
    effective_temperature = max(temperature, provider.min_temperature)
    litellm_model = resolve_litellm_model(model, resolved_api_base)

    return LLMBackbone(
        litellm_model=litellm_model,
        api_key=api_key or provider.api_key,
        api_base=resolved_api_base,
        temperature=effective_temperature,
        max_tokens=max_tokens,
    )


def generate(
    model: str,
    prompt: str,
    json_mode: bool = False,
    *,
    system_prompt: str | None = None,
    backbone: LLMBackbone | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> GenerateResult:
    """
    统一 LLM 调用入口：generate(model, prompt, json_mode).

    Args:
        model:         模型名，如 "qwen3.6-plus" / "glm-5" / "kimi-k2.5" / "claude-opus"。
                       通过 llm.providers.resolve_provider_for_model() 自动识别 provider。
        prompt:        用户 prompt。
        json_mode:     True 时调用 backbone.call_json()，返回值的 json_data 字段
                       填充解析后的 dict；False 时只返回纯文本。
        system_prompt: 可选 system prompt。
        backbone:      若已持有一个 LLMBackbone（如来自 BackboneRouter.get(worker_id)），
                       可直接传入以复用其路由/重试配置，此时忽略 model 的 provider 推断
                       （但仍用于日志/兼容签名）。
        api_key/api_base/temperature/max_tokens: 覆盖 provider 默认值。

    Returns:
        GenerateResult（text/input_tokens/output_tokens/cost/latency[/json_data]）

    Raises:
        core.exceptions.LLMCallError 及其子类（透传自 LLMBackbone）。
    """
    llm_backbone = backbone or _build_backbone(
        model,
        api_key=api_key,
        api_base=api_base,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    t_start = time.monotonic()
    if json_mode:
        data, call_result = llm_backbone.call_json(prompt, system_prompt)
        latency = time.monotonic() - t_start
        return GenerateResult(
            text=call_result.text,
            input_tokens=call_result.prompt_tokens,
            output_tokens=call_result.completion_tokens,
            cost=call_result.cost_usd,
            latency=latency,
            json_data=data,
        )

    call_result = llm_backbone.call(prompt, system_prompt)
    latency = time.monotonic() - t_start
    return GenerateResult(
        text=call_result.text,
        input_tokens=call_result.prompt_tokens,
        output_tokens=call_result.completion_tokens,
        cost=call_result.cost_usd,
        latency=latency,
    )
