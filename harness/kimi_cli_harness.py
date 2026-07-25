"""
kimi_cli_harness.py — KimiCLIHarness：真实 `kimi` CLI Harness

官方 FederatedSkill 仓库未给出 kimi-cli 的具体 flag 语法；当前命令名和参数
已按本机安装的官方 `@moonshot-ai/kimi-code@0.28.1` 的 `kimi --help` 校验：
可执行入口为 `kimi`，非交互 prompt 参数为 `-p/--prompt`。
官方 FederatedSkill 仓库的旧版成功标记取自
skillfl/skillflow_adapter/runner.py：

    ("kimi-cli.txt", '"id":"1","result":{"status":"finished"')

Kimi CLI 的输出格式会随版本/配置变化：既可能输出 `stream-json` 事件，
也可能只输出普通文本（例如 `To resume this session: kimi -r ...`）。
成功判定不能依赖任一固定输出 marker，而应以进程正常退出为主，并排除
CLI 明确报告的失败文本。

鉴权：与 qwen-code 相同，官方仓库注释明确 kimi-cli 走 OpenAI 协议，读取
OPENAI_API_KEY/OPENAI_BASE_URL；Moonshot/Kimi 额外要求 temperature>=1.0
（见 core.datatypes.WorkerProfile.is_moonshot，本类不重复实现该判断，
只在 agent_kwargs 里如实传递，不改变 WorkerProfile 本身)。

Kimi CLI Harness Fix（2026-07-21，只读分析 + 修复 stdin 输入通道）
------------------------------------------------------------------
Phase 1 只读分析（`scripts/probe_kimi_cli.py` 诊断 + 本机 `kimi --help`
逐项核对）确认的真实 invocation contract：

    Usage: kimi [options] [command]
      -p, --prompt <prompt>     Run one prompt non-interactively and print
                                 the response.
      --output-format <format>  Output format for prompt mode. Defaults to
                                 text. (choices: "text", "stream-json")
      -y, --yolo                Auto-approve regular tool calls; the agent
                                 may still ask questions. (default: false)

**kimi CLI 完全没有任何"从 stdin 读取 prompt"的机制**——`-p/--prompt`
参数值本身才是唯一的非交互 prompt 通道。旧版实现把占位字符串
"Follow the task instructions provided on stdin." 写死在 `--prompt`
里，真正的任务内容（含技能检索结果/工作区文件列表）另外通过
subprocess `input=` 管道喂给 stdin——但 `scripts/probe_kimi_cli.py` 的
真实探测证据显示：CLI 从未读取这个 stdin 内容，agent 自己尝试
`Read /dev/stdin`（报错 "does not exist"）或 `cat -`（返回空）后，
转而用 Bash 工具满仓库搜索 `.agents`/`AGENTS.md` 任务文件，最终因为
等不到真实 prompt 内容而反问用户——真实任务 prompt 从未真正进入过
Kimi agent。

修复方式：`build_argv()` 的 `--prompt` 参数值改为空字符串占位符（与
`QwenCodeHarness.build_argv()` 的 `--prompt ""` 模式一致）；真正的完整
prompt 通过新增的 `_build_invocation()` 钩子（定义在
`CLIAgentHarnessBase`，默认行为对 claude-code/qwen-code 完全不变）在
调用前原地替换进 `--prompt` 参数值，并让 stdin 保持关闭
（`input_text=None`），不再依赖一条 kimi CLI 根本不支持的通道。

Phase A 异构 harness 验证（2026-07-21）追加修复
------------------------------------------------------------------
Phase A 真实 preflight 检查（干净临时目录下真实 subprocess 调用
`kimi -y --prompt "..." --output-format text`）发现：本机真实
`kimi` CLI（v0.28.1）**不接受 `--prompt` 与 `-y/--yolo` 同时出现**，
会直接以 `returncode=1` 报错退出：`error: Cannot combine --prompt
with --yolo.`——这是上一轮修复新增 `-y` 时未做真实 subprocess 校验、
只做过单元测试（mock subprocess）验证的真实回归，会导致 Setting3/4
里 kimi-cli worker 的每一个任务都必然失败。

已移除 `-y/--yolo`：`--prompt <value>` 本身就是非交互一次性执行模式
（`kimi --help`: "Run one prompt non-interactively and print the
response."），不会像交互式会话那样等待工具调用确认，因此不需要额外的
"跳过交互确认"标志；即使需要将来还要处理某类工具确认提示，也应该在
真实发现"prompt 模式下确实会阻塞等待确认"的证据后再加，而不是预先加上
一个未经真实 CLI 校验、且与 `--prompt` 直接冲突的标志。

Windows cmd.exe 批处理换行截断修复（2026-07-21，真实 non-interactive
agent execution fix）
------------------------------------------------------------------
**真实失败现象**：`kimi --prompt "<完整多行任务 prompt>" --output-format
stream-json` 在 Windows 上 `returncode=0`，但只返回一句通用问候
（"Got it. I'm ready to help you with coding tasks. What would you
like me to do?"），workspace 里从未创建任何文件——看起来像是模型
"完全没收到任务内容"，而不是拒绝执行。

**真实根因（已用临时 subprocess 探针逐层验证，非推测）**：本机安装的
`kimi` 二进制是 npm 生成的 Windows 批处理 shim（`kimi.CMD`），内容为
`... & "%_prog%" "%dp0%\node_modules\@moonshot-ai\kimi-code\dist\
main.mjs" %*`（`%_prog%` 通常解析为 `node.exe`）。Windows 的
`CreateProcess` 对 `.cmd/.bat` 文件有内置关联行为：即使 Python
`subprocess`/`shell=False`，系统仍会隐式经由 `cmd.exe` 解析并执行这条
批处理命令行；而 `cmd.exe` 对命令行里的**字面换行符**——即使这些换行符
位于双引号包裹的参数值内部——仍然当作命令分隔符处理。本 harness 的
`--prompt` 参数值是包含真实换行的多行任务文本，于是 cmd.exe 只把
第一行（`"You are a coding agent."`）当作真正传给 kimi-code 进程的
`--prompt` 值，之后的任务步骤（创建文件/写入内容/`--output-format`
等后续参数）全部被当成同一个 cmd.exe 会话里的后续命令，从未真正到达
kimi-code 进程——这正是模型只收到问候语片段、后续 `--auto` 等参数
也一并丢失的真实原因，不是模型拒绝执行任务，也不是鉴权/网络问题。
Claude Code/Qwen Code 不受影响，因为它们的完整 prompt 走 stdin 管道
（见 `CLIAgentHarnessBase._build_invocation()` 默认实现），从不经过
cmd.exe 的命令行解析。

**已验证的修复**：`node.exe` 是原生 PE 可执行文件，Windows 不会对它
做 `.cmd/.bat` 关联，`CreateProcess` 直接用标准 `CommandLineToArgvW`
规则解析参数，能正确保留参数值内部的字面换行符。用临时 subprocess
探针直接调用 `node.exe "<kimi.CMD 同目录下的 main.mjs>" --prompt
"<完整多行 prompt>" --output-format stream-json`（跳过 `.CMD` shim）
后，kimi-code 正确解析出完整任务、真实调用 `Write` 工具、在 workspace
创建了内容精确为 `CLI_AGENT_SUCCESS` 的文件——证明修复有效。

`_resolve_windows_cmd_shim_bypass()`：仅在 `os.name == "nt"` 且
`argv[0]` 解析后是 `.cmd`/`.bat` 文件时才生效；直接读取该 shim 文件
内容，用正则提取它引用的真实 `node_modules/.../*.mjs`（或 `.js`）
入口路径（不依赖硬编码版本路径，shim 文件本身如何变化就如何解析），
换成 `[node.exe 路径, 入口脚本路径, *原始参数]` 调用，绕开 cmd.exe。
任何一步失败（找不到 shim/正则未命中/入口文件不存在/`node` 不在
PATH）都静默回退到原始 argv——不抛异常、不影响非 Windows 环境、
不影响 Claude/Qwen harness（它们完全不调用这个方法）。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from core.datatypes import WorkerProfile
from core.exceptions import TaskExecutionError
from harness.cli_harness_base import CLIAgentHarnessBase


class KimiCLIHarness(CLIAgentHarnessBase):
    """真实 `kimi` CLI Harness（Setting3/4 里 agent_harness="kimi-cli" 的 worker）。"""

    harness_name = "kimi-cli"
    binary_name = "kimi"
    version_args = ("--version",)
    # 匹配 npm 批处理 shim 里引用的真实 node 入口脚本，例如：
    #   "%dp0%\node_modules\@moonshot-ai\kimi-code\dist\main.mjs"
    # 不依赖硬编码路径，直接从 shim 文件内容里解析，兼容未来包结构调整。
    _SHIM_ENTRY_PATTERN = re.compile(r'"%dp0%\\([^"]+\.(?:mjs|js))"')
    _FAILURE_KEYWORDS = (
        "failed to run prompt",
        "authentication failed",
        "invalid api key",
        "unauthorized",
        "forbidden",
        "rate limit exceeded",
        "no model configured",
    )

    def build_argv(self, profile: WorkerProfile) -> list[str]:
        # "--prompt" 的值只是占位符，真正的完整 prompt 由 _build_invocation()
        # 在调用前原地替换（kimi CLI 唯一的非交互 prompt 通道是 -p/--prompt
        # 参数值本身，不存在 stdin 读取机制，见上方 docstring）。
        # 不加 "-y/--yolo"：Phase A 真实 preflight 发现该 CLI 会直接拒绝
        # "--prompt" 与 "-y/--yolo" 同时出现（returncode=1，"Cannot combine
        # --prompt with --yolo."）；且 "--prompt" 非交互模式本身不会等待
        # 工具调用确认，不需要这个标志。
        return [
            self.binary_name,
            "--prompt", "",
            "--output-format", "stream-json",
        ]

    def _build_invocation(
        self, profile: WorkerProfile, argv: list[str], full_prompt: str
    ) -> tuple[list[str], str | None]:
        """把完整 full_prompt 写入 argv 里 "--prompt" 参数值，不再走 stdin。

        `kimi --help` 校验确认 CLI 没有任何从 stdin 读取 prompt 的机制，
        stdin 管道内容会被完全忽略（真实探测证据见
        `scripts/probe_kimi_cli.py`）。返回 `input_text=None` 让调用方
        用 `subprocess.DEVNULL` 打开 stdin，不再打开一个 kimi 根本不读的
        管道。
        """
        fixed_argv = list(argv)
        try:
            idx = fixed_argv.index("--prompt")
        except ValueError:
            fixed_argv.extend(["--prompt", full_prompt])
        else:
            fixed_argv[idx + 1] = full_prompt
        fixed_argv = self._resolve_windows_cmd_shim_bypass(fixed_argv)
        return fixed_argv, None

    def _resolve_windows_cmd_shim_bypass(self, argv: list[str]) -> list[str]:
        """Windows 上绕开 npm `.cmd` 批处理 shim，直接用 `node.exe` 调用
        真实入口脚本，避免 cmd.exe 将 `--prompt` 参数值里的字面换行符
        当作命令分隔符截断（真实根因/修复验证见本模块 docstring）。

        任何一步失败都静默回退到原始 argv，不抛异常、不影响非 Windows
        环境、不影响 Claude/Qwen harness（它们完全不调用这个方法）。
        """
        if os.name != "nt" or not argv:
            return argv
        shim_path = shutil.which(argv[0]) or argv[0]
        if not shim_path.lower().endswith((".cmd", ".bat")):
            return argv
        try:
            shim_text = Path(shim_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return argv
        match = self._SHIM_ENTRY_PATTERN.search(shim_text)
        if match is None:
            return argv
        entry_path = Path(shim_path).resolve().parent / match.group(1)
        if not entry_path.is_file():
            return argv
        node_exe = shutil.which("node")
        if node_exe is None:
            return argv
        return [node_exe, str(entry_path), *argv[1:]]

    def build_env(self, profile: WorkerProfile) -> dict[str, str]:
        env = os.environ.copy()
        api_key = self._resolve_api_key(profile)
        if api_key:
            env["OPENAI_API_KEY"] = api_key
            env["KIMI_MODEL_API_KEY"] = api_key
        if profile.api_base:
            env["OPENAI_BASE_URL"] = profile.api_base
            env["KIMI_MODEL_BASE_URL"] = profile.api_base
        env["KIMI_MODEL_NAME"] = profile.backbone_model
        env["KIMI_MODEL_PROVIDER_TYPE"] = "openai"
        env["KIMI_MODEL_MAX_CONTEXT_SIZE"] = str(profile.max_context_tokens)
        env["KIMI_MODEL_MAX_OUTPUT_SIZE"] = "16384"
        env["KIMI_MODEL_CAPABILITIES"] = "image_in,thinking,tool_use"
        return env

    def success_marker(self) -> str:
        """Kimi 无固定成功 marker；支持 stream-json 与普通文本输出。"""
        return ""

    def _validate_cli_result(
        self, cli_result: object, effective_timeout: float | None = None
    ) -> None:
        """按退出状态和明确失败文本判定，不依赖特定版本的输出格式。"""
        # 保留共享层对 timeout / exception / non-zero returncode 的严格判定；
        # success_marker() 为空，因此不会触发固定 marker 检查。
        # [Runtime Protocol Alignment Issue1] 透传 effective_timeout，使基类
        # 的 timeout 失败信息里报告的是该 task 实际生效的超时值（可能来自
        # task.toml [agent] timeout_sec），而不是恒为仓库级默认值。
        super()._validate_cli_result(cli_result, effective_timeout=effective_timeout)

        output = f"{getattr(cli_result, 'stdout', '')}\n{getattr(cli_result, 'stderr', '')}"
        output_lower = output.lower()
        matched = next(
            (keyword for keyword in self._FAILURE_KEYWORDS if keyword in output_lower),
            None,
        )
        if matched is not None:
            diagnostic = output.strip() or "（无 CLI 输出）"
            raise TaskExecutionError(
                f"{self.harness_name} CLI 执行失败: failure_keyword={matched!r}; "
                f"output_tail={diagnostic[-4000:]!r}"
            )
