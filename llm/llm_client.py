"""
llm_client.py — 统一 LLM 调用层

支持 OpenAI 兼容 API 的三类提供商：
  - 通义千问  (provider="dashscope",  base_url=阿里云兼容端点)
  - 智谱 GLM  (provider="zhipu",      base_url=开放平台 v4 端点)
  - Moonshot  (provider="moonshot",   base_url=月之暗面 API)

设计原则：
  1. 所有提供商走 openai.OpenAI(base_url=...) ，不引入额外 SDK
  2. 超频限速 → 指数退避重试；网络抖动 → 线性重试
  3. Moonshot 不允许 temperature<1.0，单独归一化
  4. 全程追踪 token 消耗和估算费用
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Type

from openai import (
    OpenAI,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
)
from pydantic import BaseModel

from core.constants import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_RETRY_ATTEMPTS,
    MOONSHOT_TEMPERATURE,
    RETRY_BASE_SLEEP,
    RETRY_MAX_SLEEP,
    TRANSIENT_MAX_RETRIES,
)
from core.datatypes import WorkerProfile
from core.exceptions import (
    LLMCallError,
    LLMEmptyResponseError,
    LLMJSONParseError,
    LLMRateLimitError,
)
from llm.json_parser import safe_parse_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 使用统计（不可变快照通过 snapshot() 获取）
# ---------------------------------------------------------------------------


class UsageStats:
    """轻量级 token / 费用追踪器，线程不安全（单进程单客户端场景）。"""

    def __init__(self) -> None:
        self.total_calls: int = 0
        self.failed_calls: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        # 粗略估算：按输入 $0.5/M + 输出 $1.5/M
        self._input_price_per_m: float = 0.5
        self._output_price_per_m: float = 1.5

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self.prompt_tokens / 1_000_000 * self._input_price_per_m
            + self.completion_tokens / 1_000_000 * self._output_price_per_m
        )

    def record(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.total_calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"UsageStats(calls={self.total_calls}, "
            f"tokens={self.total_tokens:,}, "
            f"~${self.estimated_cost_usd:.4f})"
        )


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class LLMClient:
    """
    统一 LLM 客户端。

    使用方式：
        client = LLMClient.from_profile(profile)
        text   = client.chat("写一首诗")
        data   = client.chat_json("输出 JSON：...")
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        api_base: str,
        provider: str,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._model = model
        self._provider = provider.lower()
        self._max_tokens = max_tokens
        self._stats = UsageStats()

        # Moonshot 不接受 temperature < 1.0
        self._temperature = (
            max(temperature, MOONSHOT_TEMPERATURE)
            if self._is_moonshot()
            else temperature
        )

        self._client = OpenAI(api_key=api_key, base_url=api_base)
        logger.info(
            "LLMClient ready: model=%s provider=%s temperature=%.2f",
            model, provider, self._temperature,
        )

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_profile(
        cls,
        profile: WorkerProfile,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> "LLMClient":
        """从 WorkerProfile ρ_i 构造客户端。"""
        api_key = os.environ.get(profile.api_key_env, "")
        if not api_key:
            logger.warning(
                "环境变量 %s 未设置，API 调用可能失败 (worker=%s)",
                profile.api_key_env,
                profile.client_id,
            )
        return cls(
            model=profile.backbone_model,
            api_key=api_key,
            api_base=profile.api_base,
            provider=profile.model_provider,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def chat(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
    ) -> str:
        """纯文本对话，返回原始字符串。"""
        messages = self._build_messages(user_prompt, system_prompt)
        return self._call_with_retry(messages)

    def chat_json(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
        validate_schema: Type[BaseModel] | None = None,
    ) -> dict[str, Any]:
        """
        结构化 JSON 对话。

        自动从 LLM 响应中提取 JSON（支持 markdown 代码块）。
        如果提供 validate_schema，会用 Pydantic 校验输出结构。
        """
        raw = self.chat(user_prompt, system_prompt)
        data = self._extract_json(raw)

        if validate_schema is not None:
            try:
                validate_schema.model_validate(data)
            except Exception as exc:
                raise LLMCallError(
                    f"LLM 输出不符合 schema {validate_schema.__name__}: {exc}\n"
                    f"原始响应前 400 字: {raw[:400]!r}"
                ) from exc

        return data

    @property
    def stats(self) -> UsageStats:
        return self._stats

    @property
    def model(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    # 内部：重试逻辑
    # ------------------------------------------------------------------

    def _call_with_retry(self, messages: list[dict[str, str]]) -> str:
        """
        两级重试策略：
          Level-1  RateLimitError          → 指数退避，最多 MAX_RETRY_ATTEMPTS 次
          Level-2  网络抖动 (Connection/Timeout) → 线性退避，最多 TRANSIENT_MAX_RETRIES 次
        """
        rate_attempt = 0
        transient_attempt = 0

        while True:
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
                content = (resp.choices[0].message.content or "").strip()

                # 记录 token 用量
                if resp.usage:
                    self._stats.record(
                        resp.usage.prompt_tokens,
                        resp.usage.completion_tokens,
                    )

                if not content:
                    raise LLMEmptyResponseError(
                        f"模型 {self._model} 返回空内容（可能被内容过滤拦截）"
                    )
                return content

            except RateLimitError as exc:
                rate_attempt += 1
                if rate_attempt > MAX_RETRY_ATTEMPTS:
                    self._stats.failed_calls += 1
                    raise LLMRateLimitError(
                        f"超过最大重试次数 {MAX_RETRY_ATTEMPTS}，放弃"
                    ) from exc
                sleep = min(
                    RETRY_BASE_SLEEP * (2 ** (rate_attempt - 1)),
                    RETRY_MAX_SLEEP,
                )
                logger.warning(
                    "限速 (attempt %d/%d)，%.0fs 后重试: %s",
                    rate_attempt, MAX_RETRY_ATTEMPTS, sleep, exc,
                )
                time.sleep(sleep)

            except (APIConnectionError, APITimeoutError) as exc:
                transient_attempt += 1
                if transient_attempt > TRANSIENT_MAX_RETRIES:
                    self._stats.failed_calls += 1
                    raise LLMCallError(
                        f"网络抖动超过 {TRANSIENT_MAX_RETRIES} 次，放弃"
                    ) from exc
                sleep = RETRY_BASE_SLEEP * transient_attempt
                logger.warning(
                    "网络抖动 (attempt %d/%d)，%.0fs 后重试: %s",
                    transient_attempt, TRANSIENT_MAX_RETRIES, sleep, exc,
                )
                time.sleep(sleep)

            except (LLMEmptyResponseError, LLMCallError):
                raise
            except Exception as exc:
                self._stats.failed_calls += 1
                raise LLMCallError(f"不可重试的 LLM 错误: {exc}") from exc

    # ------------------------------------------------------------------
    # 内部：消息构造 / JSON 提取
    # ------------------------------------------------------------------

    @staticmethod
    def _build_messages(
        user_prompt: str,
        system_prompt: str | None,
    ) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": user_prompt})
        return msgs

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """委托给 json_parser，统一抛出 LLMJSONParseError。"""
        try:
            return safe_parse_json(text)
        except LLMJSONParseError:
            raise
