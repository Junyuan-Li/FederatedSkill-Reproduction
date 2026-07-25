"""
qwen_code_harness.py — QwenCodeHarness：真实 `qwen` CLI Harness

⚠ 诚实性披露（与仓库 docs/SIMPLIFICATIONS.md 的披露风格一致）：
    官方仓库 FederatedSkill-main/ 中**没有找到** qwen-code CLI 的具体 flag
    语法（`NoInstallQwenCode` 类的真实实现在外部依赖
    `libs.harbor_noinstall_agents.agents`，未包含在已发布仓库里）。
    唯一找到的真实证据（skillfl/skillflow_adapter/cli.py 注释）是：
        "qwen-code / kimi-cli workers run native OpenAI-protocol and only
         set OPENAI_API_KEY/OPENAI_BASE_URL"
    ——即鉴权走标准 OPENAI_API_KEY/OPENAI_BASE_URL 环境变量，这一点是真实的、
    有代码依据的。当前命令名和参数已按本机安装的官方
    `@qwen-code/qwen-code@0.19.11` 的 `qwen --help` 校验：可执行入口为
    `qwen`，非交互 prompt 参数为 `-p/--prompt`，模型参数为 `--model`。
"""

from __future__ import annotations

from core.datatypes import WorkerProfile
from harness.cli_harness_base import CLIAgentHarnessBase


class QwenCodeHarness(CLIAgentHarnessBase):
    """真实 `qwen` CLI Harness（配置中的 harness 标识仍为 "qwen-code"）。"""

    harness_name = "qwen-code"
    binary_name = "qwen"
    version_args = ("--version",)

    def build_argv(self, profile: WorkerProfile) -> list[str]:
        return [
            self.binary_name,
            "--auth-type=openai",
            "-y",
            "--prompt", "",
            "--model", profile.backbone_model,
        ]

    def build_env(self, profile: WorkerProfile) -> dict[str, str]:
        import os

        env = os.environ.copy()
        api_key = self._resolve_api_key(profile)
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        if profile.api_base:
            env["OPENAI_BASE_URL"] = profile.api_base
        return env

    def success_marker(self) -> str:
        # 官方仓库注释明确指出 qwen-code 输出是自由格式文本，没有可靠的
        # 结构化成功标记（"qwen-code.txt has no reliable structured success
        # marker"）——因此不检测，审计事件里 success_marker_found 恒为 False，
        # 如实反映"未知"而不是编造一个假的检测结果。
        return ""
