"""
backbone.py — 单模型 LLM 骨干调用器（对应论文中的 m_i）

与原版 skillflow_adapter/llm_client.py 的设计差异：
  原版：  make_llm_call() 返回函数闭包，重试逻辑嵌在闭包内
  本版：  LLMBackbone 类，状态显式，重试委托给 retry 模块

为什么用 litellm（而非直接 openai SDK）？
  论文 Setting 4 需要同时驱动 Qwen-Code(Dashscope) / Claude-Code(Anthropic-via-Dashscope) /
  Kimi-CLI(Moonshot) 三种 backbone，litellm 通过 <provider>/<model> 前缀统一路由，
  无需为每个提供商维护一套 SDK 客户端。
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from core.constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, MOONSHOT_TEMPERATURE
from core.exceptions import LLMCallError, LLMEmptyResponseError
from llm.json_parser import safe_parse_json
from llm.retry import (
    ErrorBucket,
    RetryConfig,
    classify_error,
    compute_backoff_sleep,
    log_retry,
)


# ---------------------------------------------------------------------------
# 调用结果
# ---------------------------------------------------------------------------


@dataclass
class BackboneCallResult:
    """
    一次 LLM 调用的完整元数据（文本 + token 用量 + 费用）。

    PatchDistiller 将其中的 cost_usd 写入 WorkerPatch.metadata 用于实验统计。
    """

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ---------------------------------------------------------------------------
# 跨调用统计
# ---------------------------------------------------------------------------


class BackboneStats:
    """累计 token 用量与费用。线程不安全，每个 worker 独立实例化。"""

    def __init__(self) -> None:
        self.total_calls: int = 0
        self.failed_calls: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_cost_usd: float = 0.0

    def record_success(self, result: BackboneCallResult) -> None:
        self.total_calls += 1
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.total_cost_usd += result.cost_usd

    def record_failure(self) -> None:
        self.failed_calls += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __repr__(self) -> str:
        return (
            f"BackboneStats(calls={self.total_calls}, "
            f"tokens={self.total_tokens:,}, "
            f"cost=${self.total_cost_usd:.4f})"
        )


# ---------------------------------------------------------------------------
# 核心 backbone 类
# ---------------------------------------------------------------------------


class LLMBackbone:
    """
    单个 LLM backbone 模型的调用器，对应论文中的 m_i。

    每个 FederatedWorker 持有一个 LLMBackbone（自己的模型）。
    服务器端 EvolutionAgent 也持有一个（服务器模型，如 glm-5 或 claude-opus）。

    设计要点：
      1. litellm 统一路由：无需为 Qwen / GLM / Kimi / Claude 各自维护 SDK 客户端
      2. 双级重试：限频（RATE_LIMIT, 无界）+ 瞬态（TRANSIENT, 有界）
      3. BackboneCallResult 保留完整元数据，而非返回裸字符串
      4. 每次调用追踪 cost_usd，来自 litellm.completion_cost()
    """

    def __init__(
        self,
        *,
        litellm_model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        retry_config: RetryConfig | None = None,
        extra_headers: dict[str, str] | None = None,
        request_timeout_seconds: float = 300.0,
    ) -> None:
        """
        Args:
            litellm_model:  litellm 格式的模型名，如 "openai/qwen3.6-plus"
                            或 "anthropic/claude-sonnet-4"。
                            建议通过工厂方法 from_worker_profile() 自动推断，
                            而非手动填写。
            api_key:        API 密钥字符串（已从环境变量读取）
            api_base:       覆盖 API 端点（Dashscope / Moonshot 专用端点）
            temperature:    采样温度（Moonshot 强制 ≥ 1.0，在 from_worker_profile 中处理）
            max_tokens:     最大生成 token 数
            retry_config:   重试策略；None → 使用 RetryConfig 默认值
            extra_headers:  附加 HTTP 头（Dashscope Anthropic 兼容端点需要）
            request_timeout_seconds: 单次 HTTP 请求超时秒数，防止底层连接永久阻塞
        """
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds 必须大于 0")
        self._litellm_model = litellm_model
        self._api_key = api_key
        self._api_base = api_base
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._retry_cfg = retry_config or RetryConfig()
        self._extra_headers = dict(extra_headers or {})
        self._request_timeout_seconds = request_timeout_seconds
        self._stats = BackboneStats()

    # ------------------------------------------------------------------
    # 工厂：从 WorkerProfile 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_worker_profile(
        cls,
        profile: "WorkerProfile",  # noqa: F821  (避免循环导入，运行时已有)
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        retry_config: RetryConfig | None = None,
        request_timeout_seconds: float = 300.0,
    ) -> "LLMBackbone":
        """
        从 WorkerProfile ρ_i 自动构造 backbone。

        处理细节：
          - 从 os.environ 读取 profile.api_key_env 对应的 API 密钥
          - Moonshot / Kimi 强制 temperature ≥ 1.0（原版 patcher_bridge.py 中的
            _resolve_patch_temperature 逻辑，这里迁移到 backbone 层）
          - api_base → litellm provider 前缀推断
        """
        import os
        api_key = os.environ.get(profile.api_key_env, "") or None

        # Moonshot 强制温度下限（原版: _resolve_patch_temperature）
        effective_temp = temperature
        if profile.is_moonshot:
            effective_temp = max(temperature, MOONSHOT_TEMPERATURE)

        litellm_model = resolve_litellm_model(profile.backbone_model, profile.api_base)

        return cls(
            litellm_model=litellm_model,
            api_key=api_key,
            api_base=profile.api_base,
            temperature=effective_temp,
            max_tokens=max_tokens,
            retry_config=retry_config,
            request_timeout_seconds=request_timeout_seconds,
        )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def call(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
    ) -> BackboneCallResult:
        """
        发起一次对话调用，返回 BackboneCallResult（含 token / 费用）。

        论文 Section 4.1.2：patcher 用 worker 自己的 backbone 调用此方法。
        """
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        return self._call_with_retry(messages)

    def call_json(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
    ) -> tuple[dict[str, Any], BackboneCallResult]:
        """
        调用 LLM 并从响应中提取 JSON 对象。

        Returns:
            (parsed_dict, BackboneCallResult)

        Raises:
            LLMJSONParseError: 无法从响应提取有效 JSON
        """
        result = self.call(user_prompt, system_prompt)
        data = safe_parse_json(result.text)
        return data, result

    @property
    def stats(self) -> BackboneStats:
        return self._stats

    @property
    def litellm_model(self) -> str:
        return self._litellm_model

    @property
    def temperature(self) -> float:
        """
        运行时实际生效的采样温度（Reproducibility Metadata / TASK1/TASK6
        新增只读属性）。这是 __init__ 时保存下来的真实构造参数（Moonshot
        强制下限已在 from_worker_profile() 里算好），不是重新计算，纯粹
        暴露既有内部状态，供 experiments/run_experiment.py 写入
        experiment_summary.json 的 "workers" 元数据块，不改变任何调用
        LLM 的行为。
        """
        return self._temperature

    @property
    def max_tokens(self) -> int:
        """运行时实际生效的最大生成 token 数，用途同 temperature 属性。"""
        return self._max_tokens

    # ------------------------------------------------------------------
    # 内部：重试循环
    # ------------------------------------------------------------------

    def _call_with_retry(self, messages: list[dict[str, str]]) -> BackboneCallResult:
        """
        双级重试：
          RATE_LIMIT → 指数退避，无界（原版设计：防止丢 worker 贡献）
          TRANSIENT  → 线性退避，有界（防止卡死）
        """
        import litellm  # 延迟导入，避免未安装时影响导入链

        # 关闭 LiteLLM 每次请求重复打印的 Provider List / issue 链接；真正的
        # 异常仍由下方分类、重试日志和最终 LLMCallError 完整输出。
        litellm.suppress_debug_info = True

        kwargs: dict[str, Any] = {
            "model": self._litellm_model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "timeout": self._request_timeout_seconds,
            # 本项目已经在本函数中统一管理重试。禁用 LiteLLM/OpenAI SDK
            # 内层重试，避免一次外层 attempt 被 SDK 悄悄扩成多次长请求，
            # 导致日志长期停在同一个 attempt 且实际超时边界失效。
            "num_retries": 0,
        }
        use_streaming = "anthropic" in (self._api_base or "").lower()
        if use_streaming:
            # DashScope 的 Anthropic-compatible 网关会在非流式长生成期间约
            # 30 秒无首字节时断开。流式传输持续返回 chunk，最后仍在本地
            # 重组为同一个完整响应，不改变 prompt、采样参数或 JSON 语义。
            kwargs["stream"] = True
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._extra_headers:
            kwargs["extra_headers"] = self._extra_headers

        cfg = self._retry_cfg
        rate_attempt = 0
        transient_attempt = 0

        while True:
            attempt = rate_attempt + transient_attempt + 1
            print(
                f"[backbone] request start model={self._litellm_model} "
                f"attempt={attempt} timeout={self._request_timeout_seconds:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            started_at = time.monotonic()
            try:
                request_finished = threading.Event()

                def report_pending() -> None:
                    while not request_finished.wait(15.0):
                        state = "stream active" if use_streaming else "request pending"
                        print(
                            f"[backbone] {state} model={self._litellm_model} "
                            f"attempt={attempt} elapsed={time.monotonic() - started_at:.0f}s",
                            file=sys.stderr,
                            flush=True,
                        )

                heartbeat = threading.Thread(target=report_pending, daemon=True)
                heartbeat.start()
                try:
                    completion = litellm.completion(**kwargs)
                    if use_streaming:
                        chunks = list(completion)
                        resp = litellm.stream_chunk_builder(chunks, messages=messages)
                        if resp is None:
                            raise LLMEmptyResponseError(
                                f"模型 {self._litellm_model} 未能重组流式响应"
                            )
                    else:
                        resp = completion
                finally:
                    request_finished.set()
                    heartbeat.join(timeout=0.1)

                # 空响应检测：只拒绝真正为空或纯空白的内容。短文本（如
                # "OK"/"NO"/"SKIP"）可能是合法决策或连通性响应，不能
                # 仅因长度不足就触发重复付费调用。
                text = _extract_response_text(resp)
                if not text or not text.strip():
                    raise LLMEmptyResponseError(
                        f"模型 {self._litellm_model} 返回空响应: {text!r}"
                    )

                # 费用（litellm 内置定价表，未知 provider 返回 0.0）
                try:
                    cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
                except Exception:
                    cost = 0.0

                usage = getattr(resp, "usage", None)
                result = BackboneCallResult(
                    text=text,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
                    cost_usd=cost,
                )
                self._stats.record_success(result)
                print(
                    f"[backbone] request done model={self._litellm_model} "
                    f"attempt={attempt} elapsed={time.monotonic() - started_at:.1f}s "
                    f"tokens={result.total_tokens}",
                    file=sys.stderr,
                    flush=True,
                )
                return result

            except LLMEmptyResponseError as exc:
                # 空响应视同 TRANSIENT
                transient_attempt += 1
                if transient_attempt > cfg.max_transient_retries:
                    self._stats.record_failure()
                    raise LLMCallError(
                        f"空响应超过 {cfg.max_transient_retries} 次，放弃"
                    ) from exc
                sleep = compute_backoff_sleep(transient_attempt, cfg)
                log_retry("空响应", transient_attempt, cfg.max_transient_retries, sleep, exc)
                time.sleep(sleep)
                continue

            except Exception as exc:
                bucket = classify_error(exc)

                if bucket == ErrorBucket.RATE_LIMIT:
                    rate_attempt += 1
                    sleep = compute_backoff_sleep(rate_attempt, cfg, cap_attempt=5)
                    log_retry("限速", rate_attempt, cfg.max_rate_retries, sleep, exc)
                    time.sleep(sleep)
                    continue

                if bucket == ErrorBucket.TRANSIENT:
                    transient_attempt += 1
                    if transient_attempt > cfg.max_transient_retries:
                        self._stats.record_failure()
                        raise LLMCallError(
                            f"网络抖动超过 {cfg.max_transient_retries} 次"
                        ) from exc
                    sleep = compute_backoff_sleep(transient_attempt, cfg)
                    log_retry(
                        "网络抖动", transient_attempt, cfg.max_transient_retries, sleep, exc
                    )
                    time.sleep(sleep)
                    continue

                # PERMANENT 错误：立即抛出（鉴权失败 / 请求格式错误）
                self._stats.record_failure()
                raise LLMCallError(f"不可重试的 LLM 错误: {exc}") from exc


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def resolve_litellm_model(model_name: str, api_base: str | None) -> str:
    """
    将 model_name 解析为 litellm 的 '<provider>/<model>' 格式。

    推断规则（按优先级）：
    1. 已含 '/'         → 直接使用（用户已手动指定）
    2. api_base 含 'anthropic' 或 model 以 'claude' 开头 → anthropic/
    3. api_base 含 'google'    或 model 以 'gemini' 开头 → gemini/
    4. api_base 含 '/openai'                              → openai/
    5. 其余（dashscope / zhipu / moonshot 兼容端点）     → openai/

    注：dashscope 提供 Anthropic-compatible 和 OpenAI-compatible 两类网关；
        对于 qwen3.6-plus / glm-5 走 OpenAI 兼容端点均用 openai/ 前缀。
    """
    name = (model_name or "").strip()
    if not name:
        return name

    base = (api_base or "").rstrip("/").lower()
    lname = name.lower()

    if "openrouter.ai" in base:
        return name if lname.startswith("openrouter/") else f"openrouter/{name}"
    if "/" in name:
        return name

    # DashScope /apps/anthropic 是官方 Setting1/2 配置使用的 Anthropic-compatible
    # Messages API。路由探针已覆盖中文 prompt，当前 SDK 不再复现旧版 ASCII 问题。
    if "anthropic" in base or lname.startswith("claude"):
        return f"anthropic/{name}"
    if "/google" in base or lname.startswith("gemini"):
        return f"gemini/{name}"
    # 其余 DashScope / Moonshot / OpenAI-compatible endpoints 使用 openai/ 前缀。
    return f"openai/{name}"


def _extract_response_text(resp: Any) -> str:
    """
    从 litellm ModelResponse 中提取纯文本。

    处理两种内容格式：
      - 字符串（大多数 OpenAI-compatible 端点）
      - list-of-TextBlock（Anthropic Messages API，经 litellm 转换后可能保留此格式）
    """
    choices = getattr(resp, "choices", None)
    if not choices:
        return ""
    msg = getattr(choices[0], "message", None)
    if msg is None:
        return ""
    content = getattr(msg, "content", None)

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            t = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if isinstance(t, str):
                parts.append(t)
        return "".join(parts)
    return ""
