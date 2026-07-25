"""
json_parser.py — JSON extraction utilities for LLM responses.

LLMs often wrap JSON in markdown code blocks or add explanation text.
These helpers robustly extract the JSON payload from the raw response.
"""

import json
import re
from typing import Any

from core.exceptions import LLMJSONParseError


# ---------------------------------------------------------------------------
# 正则模式：从 LLM 响应中提取 JSON
# ---------------------------------------------------------------------------

# 匹配 ```json ... ``` 或 ``` ... ``` 代码块
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

# 匹配最外层的 { ... } 花括号块（贪婪）
_BARE_OBJECT_RE = re.compile(r"(\{.*\})", re.DOTALL)


def safe_parse_json(text: str) -> dict[str, Any]:
    """
    Try every extraction strategy in order:
    1. Direct ``json.loads`` on the full text
    2. Extract from the first ```json ... ``` or ``` ... ``` block
    3. Extract the first ``{…}`` span

    Returns:
        Parsed dict.

    Raises:
        LLMJSONParseError: If no strategy succeeds.
    """
    text = text.strip()

    # 策略 1：直接解析整个响应
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 策略 2：代码块
    for match in _FENCED_JSON_RE.finditer(text):
        candidate = match.group(1).strip()
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    # 策略 3：裸花括号块（取最后一个最长匹配，模型可能先输出解释再输出 JSON）
    bare_matches = list(_BARE_OBJECT_RE.finditer(text))
    for m in reversed(bare_matches):
        candidate = m.group(1).strip()
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    raise LLMJSONParseError(
        f"Could not extract a JSON object from LLM response. "
        f"First 300 chars: {text[:300]!r}"
    )


def extract_json_or_none(text: str) -> dict[str, Any] | None:
    """Same as ``safe_parse_json`` but returns ``None`` instead of raising."""
    try:
        return safe_parse_json(text)
    except LLMJSONParseError:
        return None


def ensure_string_values(d: dict) -> dict[str, str]:
    """
    Recursively coerce all values in *d* to strings.
    Used when validating ``upsert_files`` where LLM may return non-str values.
    """
    return {k: str(v) if not isinstance(v, str) else v for k, v in d.items()}
