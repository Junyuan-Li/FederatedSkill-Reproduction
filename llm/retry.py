"""
retry.py — 错误分类与退避重试工具

把重试策略从 backbone 中解耦出来，作为独立可测试模块。
原版（skillflow_adapter/llm_client.py）将重试逻辑嵌在函数闭包里；
本版将其显式化为 ErrorBucket + RetryConfig，便于单测和参数调整。
"""

from __future__ import annotations

import random
import sys
import time
from dataclasses import dataclass
from enum import Enum, auto

from core.constants import (
    MAX_RETRY_ATTEMPTS,
    RETRY_BASE_SLEEP,
    RETRY_MAX_SLEEP,
    TRANSIENT_MAX_RETRIES,
)


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


class ErrorBucket(Enum):
    """
    LLM 调用错误的三条处理路径。

    RATE_LIMIT  — 限频 / 过载：指数退避，重试次数由 RetryConfig.max_rate_retries
                  控制（默认 MAX_RETRY_ATTEMPTS，实验中通常设置很大以防丢 worker）
    TRANSIENT   — 网络抖动 / 5xx：有界重试，超限后抛出 LLMCallError
    EMPTY       — 200 OK 但内容为空：与 TRANSIENT 同等处理
    PERMANENT   — 鉴权 / 请求格式错误：立即抛出，不重试
    """

    RATE_LIMIT = auto()
    TRANSIENT = auto()
    EMPTY = auto()
    PERMANENT = auto()


# 限频相关的异常类名
_RATE_LIMIT_TYPE_NAMES: frozenset[str] = frozenset(
    {"RateLimitError", "Throttled", "OverloadedError"}
)

# 限频相关的消息片段（dashscope / zhipu / moonshot 各有不同措辞）
_RATE_LIMIT_PHRASES: tuple[str, ...] = (
    "rate limit", "ratelimit", "too many requests",
    "overloaded", "overload", "throttle", "rate exceeded",
    "concurrent", "qps limit", "tokens per minute", "tpm",
    "requests per minute", "rpm exceeded",
    "quota exceeded for the moment",
)

# 瞬态错误的异常类名
_TRANSIENT_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "APIConnectionError", "APITimeoutError", "Timeout",
        "ServiceUnavailableError", "InternalServerError",
        "ConnectionError", "ReadTimeout", "ConnectTimeout",
        "RemoteProtocolError", "ProtocolError",
    }
)

# 瞬态错误的消息片段
_TRANSIENT_PHRASES: tuple[str, ...] = (
    "timeout", "connection reset", "temporarily unavailable",
    "connection refused", "broken pipe", "eof occurred",
    "server disconnected", "incomplete read",
)

# 瞬态/限频的 HTTP 状态码
_RATE_LIMIT_CODES: frozenset = frozenset({429, "429"})
_TRANSIENT_CODES: frozenset = frozenset({500, 502, 503, 504, "500", "502", "503", "504"})


def classify_error(exc: BaseException) -> ErrorBucket:
    """
    将一个异常归入 ErrorBucket，决定重试路径。

    设计偏保守：宁可误判为 RATE_LIMIT 多等一会儿，也不误判为 PERMANENT 丢掉
    一个 worker 的贡献（论文中每 round 丢 patch 无法弥补）。
    """
    type_name = type(exc).__name__

    if type_name in _RATE_LIMIT_TYPE_NAMES:
        return ErrorBucket.RATE_LIMIT

    msg = str(exc).lower()
    if any(p in msg for p in _RATE_LIMIT_PHRASES):
        return ErrorBucket.RATE_LIMIT

    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in _RATE_LIMIT_CODES:
        return ErrorBucket.RATE_LIMIT

    if type_name in _TRANSIENT_TYPE_NAMES:
        return ErrorBucket.TRANSIENT
    if status in _TRANSIENT_CODES:
        return ErrorBucket.TRANSIENT
    if any(p in msg for p in _TRANSIENT_PHRASES):
        return ErrorBucket.TRANSIENT

    return ErrorBucket.PERMANENT


# ---------------------------------------------------------------------------
# 退避配置与计算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryConfig:
    """
    重试策略配置——作为一等值对象，便于测试时替换。

    Attributes:
        base_sleep:            指数退避的基础等待秒数
        max_sleep:             等待上限秒数
        max_rate_retries:      限频重试上限（通常设很大，不轻易放弃）
        max_transient_retries: 瞬态重试上限（有界，防止卡死）
        jitter:                是否加随机抖动（避免多 worker 同步惊群）
    """

    base_sleep: float = RETRY_BASE_SLEEP
    max_sleep: float = RETRY_MAX_SLEEP
    max_rate_retries: int = MAX_RETRY_ATTEMPTS
    max_transient_retries: int = TRANSIENT_MAX_RETRIES
    jitter: bool = True

    @classmethod
    def aggressive(cls) -> "RetryConfig":
        """高并发联邦实验配置：更长退避、更多重试次数。"""
        return cls(base_sleep=10.0, max_sleep=600.0, max_rate_retries=9999, jitter=True)

    @classmethod
    def fast_fail(cls) -> "RetryConfig":
        """单测 / 调试用：快速失败，不等待。"""
        return cls(base_sleep=0.01, max_sleep=0.1, max_rate_retries=2, max_transient_retries=1)


def compute_backoff_sleep(attempt: int, cfg: RetryConfig, cap_attempt: int = 6) -> float:
    """
    计算第 *attempt* 次重试应等待的秒数。

    公式: sleep = min(base_sleep × 2^(min(attempt-1, cap)), max_sleep) × jitter
    """
    exp = min(attempt - 1, cap_attempt)
    raw = cfg.base_sleep * (2 ** exp)
    capped = min(raw, cfg.max_sleep)
    if cfg.jitter:
        # 乘以 [0.5, 1.5) 的随机因子
        return capped * (0.5 + random.random())
    return capped


def log_retry(
    reason: str,
    attempt: int,
    max_attempts: int,
    sleep: float,
    exc: BaseException | None,
) -> None:
    """输出重试日志到 stderr（与原版格式一致，便于 paper_logs 分析脚本复用）。"""
    exc_part = f": {type(exc).__name__}: {exc}" if exc else ""
    print(
        f"[backbone] {reason} (attempt {attempt}/{max_attempts}), "
        f"sleep {sleep:.1f}s{exc_part}",
        file=sys.stderr,
        flush=True,
    )
