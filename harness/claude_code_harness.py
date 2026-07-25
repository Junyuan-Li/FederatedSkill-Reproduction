"""
claude_code_harness.py — ClaudeCodeHarness：真实 `claude` CLI Harness

命令语法**取自官方仓库真实代码**（非猜测）：
    FederatedSkill-main/skillfl/skillflow_adapter/merge.py
    ::make_claude_code_subprocess_runner / make_podman_claude_runner

        cmd = [claude_bin, "--print", "--model", model_name,
               "--dangerously-skip-permissions", "--verbose",
               "--output-format", "stream-json"]
        subprocess.run(cmd, cwd=sandbox_dir, input=prompt, text=True,
                        capture_output=True, timeout=wall_clock_sec)

    成功标记字符串 '"type":"result"' 取自官方仓库
    skillfl/skillflow_adapter/runner.py 里对 claude-code.txt 的检测逻辑。

鉴权：claude-code CLI 在本仓库里是**跨 backbone 共用的 CLI 工具**——Setting1
的 u0（qwen3.6-plus）、Setting2 的全部 glm-5 worker 等，都用同一个 `claude`
二进制作为 harness，但要分别路由到各自真正的 backbone provider（DashScope /
Moonshot 等 Anthropic-compatible 端点），而不是固定打到某一个 Anthropic
Gateway。因此 build_env() 必须按 **per-worker** 的 profile.api_base /
profile.api_key_env 设置 ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY（与
qwen_code_harness.py 用 OPENAI_BASE_URL/OPENAI_API_KEY、kimi_cli_harness.py
同理的模式完全一致），不能只依赖进程里继承的全局 ANTHROPIC_BASE_URL/
ANTHROPIC_AUTH_TOKEN——那一组变量只在"某个 worker 的 backbone 真的是走
第三方 Claude 网关"时才会被用到（当前 Setting1-4 配置里没有这种 worker，
但架构上仍支持：只要 profile.api_base 指向该网关、profile.api_key_env 指向
对应 token 的环境变量名，同一套 per-worker 覆盖逻辑就会生效）。
"""

from __future__ import annotations

from core.datatypes import WorkerProfile
from harness.cli_harness_base import CLIAgentHarnessBase
from llm.providers import DASHSCOPE_ANTHROPIC_BASE


class ClaudeCodeHarness(CLIAgentHarnessBase):
    """真实 `claude` CLI Harness（Setting1/2/3/4 里 agent_harness="claude-code" 的 worker）。"""

    harness_name = "claude-code"
    binary_name = "claude"
    version_args = ("--version",)

    def build_argv(self, profile: WorkerProfile) -> list[str]:
        return [
            self.binary_name,
            "--print",
            "--model", profile.backbone_model,
            "--dangerously-skip-permissions",
            "--verbose",
            "--output-format", "stream-json",
        ]

    def build_env(self, profile: WorkerProfile) -> dict[str, str]:
        import os

        env = os.environ.copy()
        # Per-worker 路由覆盖（与 qwen_code_harness.py/kimi_cli_harness.py 同一模式）：
        # profile.api_key_env 现在指向 Provider Key Isolation Fix 后拆分的
        # QWEN_DASHSCOPE_API_KEY / GLM_DASHSCOPE_API_KEY / MOONSHOT_API_KEY 等
        # 专属变量名，这里把解析出的真实值写入 ANTHROPIC_API_KEY（claude CLI
        # 在 ANTHROPIC_API_KEY 存在时优先使用它，覆盖继承来的
        # ANTHROPIC_AUTH_TOKEN），profile.api_base 覆盖继承来的全局
        # ANTHROPIC_BASE_URL，确保真正打到该 worker 的 backbone provider。
        api_key = self._resolve_api_key(profile)
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        if profile.api_base:
            # Setting1 的同一个 profile 同时供两条调用路径使用：Python
            # LiteLLM/distiller 需要 OpenAI-compatible /compatible-mode/v1，
            # claude CLI 则必须使用 Anthropic-compatible /apps/anthropic。
            # 只在 harness 环境里转换，不修改 profile，避免破坏 distiller。
            if "dashscope.aliyuncs.com/compatible-mode" in profile.api_base:
                env["ANTHROPIC_BASE_URL"] = DASHSCOPE_ANTHROPIC_BASE
            else:
                env["ANTHROPIC_BASE_URL"] = profile.api_base
        # --dangerously-skip-permissions 在非容器环境下要求显式声明沙箱状态，
        # 与官方仓库 make_podman_claude_runner 文档字符串一致。
        env["IS_SANDBOX"] = "1"
        # Part3 要求的两个附加变量：关闭非必要遥测流量 + 关闭 attribution header。
        # 用 setdefault 而不是强制覆盖：如果调用方已经在进程环境里显式设置了
        # 不同的值，尊重该显式设置，不悄悄改写。
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        env.setdefault("CLAUDE_CODE_ATTRIBUTION_HEADER", "0")
        # 官方 1_se_qwen.local.yaml 等配置里 claude-code worker 的 env 显式设置
        # ANTHROPIC_MAX_RETRIES: "999"（claude CLI 自身请求级重试上限，独立于
        # 本仓库 llm/retry.py 的应用层重试）。之前本仓库未设置这个变量，
        # claude CLI 会用其内置的较小默认值，在限流高峰下可能比官方更快放弃
        # 单次请求。用 setdefault 保留调用方显式覆盖的可能。
        env.setdefault("ANTHROPIC_MAX_RETRIES", "999")
        return env

    def success_marker(self) -> str:
        return '"type":"result"'
