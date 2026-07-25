"""
factory.py — get_harness()：按 agent_harness 名称 + 模式统一分派

用户 Part6 要求：
    AgentHarness
    |
    |-- CLIHarness
    |
    |-- APIWorkspaceHarness

    默认：
        strict reproduction mode: CLI
        debug mode: API

本模块是唯一的分派入口，executor/harness_executor.py::HarnessAwareExecutor
只调用 get_harness()，不直接 import 任何具体 Harness 类，避免执行器与
具体 CLI 实现耦合（用户 Part2 要求"不要让 executor 直接绑定某个 CLI"）。
"""

from __future__ import annotations

from typing import Any, Literal

from harness.api_workspace_harness import APIWorkspaceHarness
from harness.base_harness import BaseAgentHarness
from harness.claude_code_harness import ClaudeCodeHarness
from harness.kimi_cli_harness import KimiCLIHarness
from harness.qwen_code_harness import QwenCodeHarness

HarnessMode = Literal["strict", "debug"]

#: agent_harness 字符串（core.datatypes.WorkerProfile.agent_harness 的真实取值）
#: -> 具体 CLI Harness 类。与用户 Part5 example 完全一致：
#:   claude-code -> ClaudeCodeHarness, qwen-code -> QwenCodeHarness,
#:   kimi-cli -> KimiCLIHarness。
_CLI_HARNESS_MAP: dict[str, type[BaseAgentHarness]] = {
    "claude-code": ClaudeCodeHarness,
    "qwen-code": QwenCodeHarness,
    "kimi-cli": KimiCLIHarness,
}


def get_harness(agent_harness: str, mode: HarnessMode, router: Any, top_k_skills: int = 3) -> BaseAgentHarness:
    """
    按 (agent_harness 字符串, mode) 构造具体 Harness 实例。

    Args:
        agent_harness: WorkerProfile.agent_harness 的取值，如 "claude-code"。
        mode:          "strict"（默认，真实 CLI subprocess）或
                       "debug"（API 回退，委托 AgentWorkspaceExecutor）。
        router:        BackboneRouter，传给 Harness 构造函数（CLI Harness
                       仍需要它来做技能检索复用 client.executor.TaskExecutor
                       的 helper；debug 模式用它构造真实 LLM 调用）。

    Raises:
        ValueError: agent_harness 不在已知三种取值之一，且 mode="strict"
            （debug 模式下任何 agent_harness 名称都合法，因为不实际使用它
            来选择 CLI 二进制）。
        harness.cli_utils.CLIBinaryNotFoundError: mode="strict" 且对应
            CLI 二进制未安装——由具体 Harness.initialize() 触发，本函数
            本身不做二进制检测（延迟到真正 run() 时才检测，避免仅仅
            "构造对象"这个动作就要求本机装好所有 CLI）。
    """
    if mode == "debug":
        return APIWorkspaceHarness(router=router, top_k_skills=top_k_skills)

    if mode != "strict":
        raise ValueError(f"未知 harness mode={mode!r}，仅支持 'strict'/'debug'")

    harness_cls = _CLI_HARNESS_MAP.get(agent_harness)
    if harness_cls is None:
        raise ValueError(
            f"未知 agent_harness={agent_harness!r}，strict 模式仅支持 "
            f"{sorted(_CLI_HARNESS_MAP)}（debug 模式无此限制）"
        )
    return harness_cls(router=router, top_k_skills=top_k_skills)
