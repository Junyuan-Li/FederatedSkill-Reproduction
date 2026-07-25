"""
trajectory_adapter.py — CLI event -> 统一 trajectory record（Part4 要求）

论文定义（Section 4.1.1）：
    τ_i = (a1, o1, ..., aT, oT)

用户 Part4 要求的最小字段集合：
    {task_id, agent_id, timestamp, action, observation, tool, command,
     result, skill_used, reward}

设计原则（"确保 skill extraction 模块无需修改"）：
    - 本模块只新增一种"CLI 事件 -> dict"的适配函数，产出的记录是**追加性**
      的审计/调试视图，不替换、不进入 core.datatypes.Trajectory 的必需字段。
    - 真正被 client/distiller.py::PatchDistiller（skill extraction 的
      唯一消费者）读取的仍然是 core.datatypes.Trajectory 本身
      （steps/actions/generated_files/exceptions 等既有字段），这些字段由
      executor/trajectory.py::TrajectoryCollector 产出——CLI Harness 复用
      同一个 TrajectoryCollector（见 base_harness.py），因此
      PatchDistiller **不需要**认识本模块产出的 record 格式。
    - 本模块产出的 record 列表额外挂在
      Trajectory.metadata["cli_events"]（Trajectory 本身没有 metadata 字段，
      改用 actions 列表里追加一条 type="cli_event_record" 的 dict，不新增
      core.datatypes.Trajectory 的任何字段）。
"""

from __future__ import annotations

import json
import time
from typing import Any

# 用户 Part4 要求的最小字段集合（保持字段名/顺序与需求原文一致）
CLI_EVENT_FIELDS: tuple[str, ...] = (
    "task_id",
    "agent_id",
    "timestamp",
    "action",
    "observation",
    "tool",
    "command",
    "result",
    "skill_used",
    "reward",
)


def cli_event_to_record(
    *,
    task_id: str,
    agent_id: str,
    action: str,
    observation: str = "",
    tool: str = "",
    command: str = "",
    result: Any = None,
    skill_used: list[str] | None = None,
    reward: float | None = None,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """把一次 CLI 事件（一次工具调用/一次输出行）转换成统一 schema 的 dict。

    字段严格按 CLI_EVENT_FIELDS 顺序返回，缺省值均为空/None，不编造数据。
    """
    return {
        "task_id": task_id,
        "agent_id": agent_id,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "action": action,
        "observation": observation,
        "tool": tool,
        "command": command,
        "result": result,
        "skill_used": list(skill_used or []),
        "reward": reward,
    }


def parse_stream_json_lines(stdout: str) -> list[dict[str, Any]]:
    """
    尝试把 CLI 的 stream-json 输出（每行一个 JSON 对象，claude-code
    `--output-format stream-json` 的真实格式，见官方仓库 merge.py 文档字符串）
    解析成事件列表；无法解析的行会被忽略（不是所有 CLI 都支持
    stream-json——qwen-code/kimi-cli 官方仓库里没有强证据支持该格式，此时
    退化为返回空列表，调用方应回退到"整段 stdout 作为一个 final_answer 事件"
    的处理方式）。
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return events


def stream_json_events_to_steps(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """把 Claude 风格 stream-json 事件还原为可供蒸馏的 agentic steps。"""
    steps: list[dict[str, Any]] = []
    final_message = ""

    for event in events:
        event_type = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []

        if event_type == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use" and block.get("name"):
                    tool_calls.append({
                        "type": "function",
                        "id": block.get("id"),
                        "function": {
                            "name": str(block["name"]),
                            "arguments": block.get("input", {}),
                        },
                    })
            text = "\n".join(text_parts)
            if text or tool_calls:
                steps.append({
                    "role": "assistant",
                    "content": text,
                    "tool_calls": tool_calls,
                    "tool_results": [],
                    "observation": "",
                })
                if text:
                    final_message = text

        elif event_type == "user":
            text_parts = []
            tool_results: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_result":
                    raw_content = block.get("content", "")
                    normalized_content = (
                        raw_content
                        if isinstance(raw_content, str)
                        else json.dumps(raw_content, ensure_ascii=False)
                    )
                    tool_results.append({
                        "tool_use_id": block.get("tool_use_id"),
                        "content": normalized_content,
                        "is_error": bool(block.get("is_error", False)),
                    })
            text = "\n".join(text_parts)
            if text or tool_results:
                observation = "\n".join(
                    str(result.get("content", "")) for result in tool_results
                )
                steps.append({
                    "role": "tool" if tool_results else "user",
                    "content": text,
                    "tool_calls": [],
                    "tool_results": tool_results,
                    "observation": observation,
                })

        elif event_type == "result" and isinstance(event.get("result"), str):
            final_message = event["result"]

    return steps, final_message
