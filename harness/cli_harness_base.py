"""
cli_harness_base.py — CLIAgentHarnessBase：三个真实 CLI Harness 的公共基类

不是用户 Part2 要求的抽象接口本身（那是 base_harness.py::BaseAgentHarness），
而是三个具体实现（claude-code/qwen-code/kimi-cli）共享的工程细节：
    - CLI 二进制存在性检测（复用 cli_utils.check_cli_binary，Part3）
    - Prompt 构建（组合复用 client.executor.TaskExecutor 私有方法 +
      executor.agent_executor.AgentWorkspaceExecutor 的多文件 prompt 格式，
      与仓库既有"组合复用、不重复实现"约定一致，不新增技能检索/prompt
      拼装逻辑）
    - subprocess 调用（复用 cli_utils.run_cli_subprocess）
    - 工作区 diff -> 生成文件收集（复用 executor.environment.WorkspaceManager）
    - Trajectory 组装（复用 executor.trajectory.TrajectoryCollector，
      产出字段与 AgentWorkspaceExecutor 完全同构，
      client/distiller.py::PatchDistiller 不需要任何改动即可消费）

子类只需提供：
    binary_name       CLI 可执行文件名
    version_args      版本检测参数
    build_argv()      子进程 argv
    build_env()       子进程环境变量（含 API key，从 profile.api_key_env 读取）
    success_marker()  可选：CLI 输出里表示"成功完成"的字符串标记
                       （仅用于审计 tool_events，不影响 reward——reward 仍由
                       统一的 verify() 步骤计算，与 CLI 自身报告的成功与否
                       解耦，避免"CLI 说成功但代码其实没通过测试"的假阳性）
"""

from __future__ import annotations

import logging
import os
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from client.executor import TaskExecutor as _BaseTaskExecutor
from core.constants import MAX_TRAJECTORY_PROMPT_CHARS
from core.datatypes import WorkerProfile
from core.exceptions import TaskExecutionError
from executor.environment import WorkspaceManager
from executor.trajectory import TrajectoryCollector
from harness.base_harness import BaseAgentHarness, HarnessExecutionResult
from harness.cli_utils import check_cli_binary, run_cli_subprocess
from harness.trajectory_adapter import (
    cli_event_to_record,
    parse_stream_json_lines,
    stream_json_events_to_steps,
)

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary

logger = logging.getLogger(__name__)


def _extract_cli_usage(stdout: str) -> tuple[int, float]:
    """从 CLI stream-json 的最终 result 事件提取 token 与实际成本。"""
    result_events = [
        event for event in parse_stream_json_lines(stdout)
        if event.get("type") == "result"
    ]
    if not result_events:
        return 0, 0.0
    event = result_events[-1]
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    token_fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    total_tokens = sum(
        int(usage.get(field, 0) or 0)
        for field in token_fields
        if isinstance(usage.get(field, 0), (int, float))
    )
    raw_cost = event.get("total_cost_usd", event.get("cost_usd", 0.0))
    cost_usd = float(raw_cost) if isinstance(raw_cost, (int, float)) else 0.0
    return total_tokens, cost_usd


class CLIAgentHarnessBase(BaseAgentHarness):
    """三个真实 CLI Harness（claude-code/qwen-code/kimi-cli）的公共基类。"""

    binary_name: str = ""
    version_args: tuple[str, ...] = ("--version",)
    default_timeout: float = 600.0
    supports_max_turns: bool = False

    # 官方对齐 Part4：在结束任务前必须完成的收尾校验步骤。真实实验发现的
    # bug——agent 用 Bash 跑了几个一次性 `python -c` 片段把正确答案打印到了
    # stdout，也单独写了一个 solution.py 文件，但从未真正执行 solution.py、
    # 也从未把结果落盘到任务要求的确切输出路径，就直接自称"任务已完成"结束
    # 了 session（harness/base_harness.py 的强制执行步骤是"最后一道防线"，
    # 这里是"第一道防线"：明确要求 agent 自己在结束前完成这些检查，尽量不
    # 依赖 harness 事后补救）。
    _VERIFICATION_DISCIPLINE_BLOCK = (
        "## 结束任务前必须完成的校验（不可省略）\n"
        "在你认为任务已经完成、准备结束这次会话之前，必须依次完成：\n"
        "1. 确认你已经把最终结果**真正写入**任务描述里要求的确切输出文件路径"
        "（不要只是把结果打印到终端/stdout 就当作完成——打印出来的内容不会被"
        "验证器读取，只有真正写入磁盘的文件才算数）。\n"
        "2. 实际执行一次你生成的主脚本（例如用 Bash 运行 `python3 solution.py`），"
        "而不是只用零散的一次性代码片段验证思路。\n"
        "3. 执行完成后，重新读取该输出文件，确认它存在、格式正确（如 JSON 可被"
        "解析）、内容符合任务描述的 schema。\n"
        "4. 只有在完成上述读回校验之后，才可以在最终回复里声明任务完成；如果"
        "校验发现问题，必须先修复并重新执行、重新校验，不能带着未验证通过的"
        "结果直接声明成功。"
    )

    def __init__(self, router: Any, top_k_skills: int = 3) -> None:
        super().__init__(router, top_k_skills)
        self._cli_version = ""
        self._runtime_mode = os.environ.get(
            "FEDERATEDSKILL_RUNTIME_MODE", "real_experiment"
        )
        runtime_path = Path(__file__).resolve().parents[1] / "configs" / "runtime.yaml"
        runtime_config = yaml.safe_load(runtime_path.read_text(encoding="utf-8")) or {}
        if self._runtime_mode not in runtime_config:
            raise ValueError(
                f"未知 CLI runtime mode={self._runtime_mode!r}; "
                f"合法值={sorted(runtime_config)}"
            )
        selected = runtime_config[self._runtime_mode] or {}
        self.default_timeout = float(selected.get("timeout", 600))
        max_turns = selected.get("max_turns")
        self.max_turns = int(max_turns) if max_turns is not None else None
        # 组合复用 TaskExecutor 的技能检索 / prompt 构建（与
        # executor/agent_executor.py::AgentWorkspaceExecutor 相同的复用方式，
        # 不重复实现、不修改其源码）。
        self._helper = _BaseTaskExecutor(router, top_k_skills=top_k_skills)

    # ------------------------------------------------------------------
    # initialize()
    # ------------------------------------------------------------------

    def initialize(self, task: "Task", profile: WorkerProfile) -> WorkspaceManager:
        self._cli_version = check_cli_binary(self.binary_name, self.version_args)
        ws = WorkspaceManager(prefix=f"{self.harness_name}_{task.task_id}_")
        source_environment_dir = task.metadata.get("source_environment_dir")
        if source_environment_dir:
            ws.copy_input_tree(source_environment_dir)
        else:
            ws.write_input_files(task.files)
        ws.snapshot()
        return ws

    # ------------------------------------------------------------------
    # execute_task()
    # ------------------------------------------------------------------

    def execute_task(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        workspace: WorkspaceManager,
    ) -> HarnessExecutionResult:
        relevant_entries = self._helper._retrieve_skills(task, library)
        skills_text = self._helper._format_skills(relevant_entries)
        system_prompt = self._helper._build_system_prompt(profile)
        user_prompt = self._build_workspace_prompt(task, skills_text)
        full_prompt = f"{system_prompt}\n\n{user_prompt}"

        argv = self.build_argv(profile)
        # [Runtime Protocol Alignment Issue1/Issue3] 优先使用 task 自带的
        # agent 执行超时（来自 task.toml [agent] timeout_sec，经
        # benchmark/skillflow_adapter/converter.py 写入 Task.metadata）；
        # 该字段缺失（如自建的非 SkillFlow 任务）时才回退到
        # configs/runtime.yaml 的仓库级 self.default_timeout。
        effective_timeout = float(
            task.metadata.get("agent_timeout_seconds") or self.default_timeout
        )
        if self.max_turns is not None:
            if self.supports_max_turns:
                argv.extend(["--max-turns", str(self.max_turns)])
            else:
                logger.warning(
                    "runtime=%s 配置 max_turns=%d，但 %s CLI %s 不支持 "
                    "--max-turns；不注入无效参数，使用 %.0fs wall-clock 硬上限",
                    self._runtime_mode, self.max_turns, self.harness_name,
                    self._cli_version or "unknown", effective_timeout,
                )
        env = self.build_env(profile)
        argv, input_text = self._build_invocation(profile, argv, full_prompt)

        cli_result = run_cli_subprocess(
            argv,
            cwd=str(workspace.path),
            input_text=input_text,
            env=env,
            timeout=effective_timeout,
        )
        cli_result.cli_version = self._cli_version
        self._validate_cli_result(cli_result, effective_timeout=effective_timeout)

        generated_paths = workspace.diff_generated_files()
        files = {p: workspace.read_generated_file(p) for p in generated_paths}

        tool_events = self._build_tool_events(task, profile, cli_result)
        total_tokens, cost_usd = _extract_cli_usage(cli_result.stdout)
        retrieved_skill_paths = [entry.path for entry in relevant_entries]

        return HarnessExecutionResult(
            files=files,
            stdout=cli_result.stdout,
            stderr=cli_result.stderr,
            tool_events=tool_events,
            returncode=cli_result.returncode,
            timed_out=cli_result.timed_out,
            exception=cli_result.exception,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            retrieved_skill_paths=retrieved_skill_paths,
        )

    def _build_invocation(
        self, profile: WorkerProfile, argv: list[str], full_prompt: str
    ) -> tuple[list[str], str | None]:
        """Kimi CLI Harness Fix（2026-07-21）新增的可覆盖钩子。

        默认行为（claude-code/qwen-code 现有行为，**逐字节不变**）：argv 本身
        不含 prompt 内容，完整 full_prompt 通过 subprocess stdin 管道传入
        （来源见 `ClaudeCodeHarness`/`QwenCodeHarness` docstring 引用的官方
        `input=prompt` 语法）。

        子类若发现目标 CLI 没有从 stdin 读取 prompt 的机制（如
        `KimiCLIHarness`，见其覆盖版 docstring 引用的 `kimi --help` 只读
        探测证据），可覆盖本方法把 full_prompt 直接写入 argv 里的某个参数
        值，并返回 `input_text=None`（不再打开 stdin 管道）。
        """
        return argv, full_prompt

    def _validate_cli_result(self, cli_result: Any, effective_timeout: float | None = None) -> None:
        """Strict 模式下 CLI 失败必须终止 trial，不能蒸馏失败 trajectory。

        [Runtime Protocol Alignment Issue1] effective_timeout 由调用方
        （execute_task()）传入本次调用实际使用的超时值（优先取自
        Task.metadata['agent_timeout_seconds']，缺失时才是
        self.default_timeout）；未传入时（如既有测试直接调用本方法）
        回退到 self.default_timeout，保持向后兼容。
        """
        timeout_for_message = effective_timeout if effective_timeout is not None else self.default_timeout
        marker = self.success_marker()
        failure_reason = ""
        if cli_result.timed_out:
            failure_reason = (
                f"timed_out=True timeout={timeout_for_message:.0f}s "
                f"reason={cli_result.timeout_reason or 'wall_clock_timeout'}"
            )
        elif cli_result.exception is not None:
            failure_reason = (
                f"exception={type(cli_result.exception).__name__}: "
                f"{cli_result.exception}"
            )
        elif cli_result.returncode != 0:
            failure_reason = f"returncode={cli_result.returncode}"
        elif marker and marker not in cli_result.stdout:
            failure_reason = f"missing_success_marker={marker!r}"

        if failure_reason:
            diagnostic = (cli_result.stderr or cli_result.stdout or "（无 CLI 输出）").strip()
            raise TaskExecutionError(
                f"{self.harness_name} CLI 执行失败: {failure_reason}; "
                f"output_tail={diagnostic[-4000:]!r}"
            )

    def _build_workspace_prompt(self, task: "Task", skills_text: str) -> str:
        """与 AgentWorkspaceExecutor._build_workspace_prompt 相同的多文件格式
        （不直接 import 私有方法跨类复用，因为它是 instance 方法且语义完全
        通用，这里保持一份等价副本，避免引入子类间不必要的耦合）。"""
        input_files = "、".join(task.files.keys()) if task.files else "（无预置输入文件）"
        return (
            f"## 任务\n{task.description}\n\n"
            f"## 工作区已有输入文件\n{input_files}\n\n"
            f"## 已有技能参考\n{skills_text}\n\n"
            f"{self._VERIFICATION_DISCIPLINE_BLOCK}\n\n"
            "## 输出格式（多文件工作区模式）\n"
            "如果需要生成多个文件，请为每个文件单独使用一个代码块，并在代码块起始处标注相对路径：\n"
            "```python:solution.py\n<文件内容>\n```\n"
            "如果只需要一个文件，也请使用同样的标注格式（文件名建议为 solution.py）。\n"
            "只输出代码块，不要输出代码块之外的解释文字。\n"
        )

    def _build_tool_events(self, task: "Task", profile: WorkerProfile, cli_result: Any) -> list[dict[str, Any]]:
        """把一次 CLI subprocess 调用转换成 Part4 要求 schema 的事件列表。"""
        events: list[dict[str, Any]] = []
        json_events = parse_stream_json_lines(cli_result.stdout)
        marker = self.success_marker()
        succeeded = bool(marker) and marker in cli_result.stdout
        events.append(
            cli_event_to_record(
                task_id=task.task_id,
                agent_id=profile.client_id,
                action="cli_invoke",
                observation=(cli_result.stdout or cli_result.stderr)[:2000],
                tool=self.harness_name,
                command=" ".join(cli_result.argv),
                result={
                    "command": cli_result.command,
                    "pid": cli_result.pid,
                    "cli_version": cli_result.cli_version,
                    "elapsed_seconds": cli_result.elapsed_seconds,
                    "stream_event_count": cli_result.stream_event_count,
                    "tool_call_count": cli_result.tool_call_count,
                    "last_event_type": cli_result.last_event_type,
                    "timeout_reason": cli_result.timeout_reason,
                    "returncode": cli_result.returncode,
                    "timed_out": cli_result.timed_out,
                    "stream_json_events": len(json_events),
                    "success_marker_found": succeeded,
                },
                skill_used=[],
                reward=None,
            )
        )
        return events

    # ------------------------------------------------------------------
    # collect_trajectory()
    # ------------------------------------------------------------------

    def collect_trajectory(
        self,
        task: "Task",
        profile: WorkerProfile,
        round_idx: int,
        workspace: WorkspaceManager,
        exec_result: HarnessExecutionResult,
        reward: float,
        verifier_output: str,
        verifier_subtest_failures: list[str],
    ) -> Any:
        collector = TrajectoryCollector(task.task_id, profile.client_id, round_idx)
        collector.add_action(
            "skill_retrieval",
            skill_paths=exec_result.retrieved_skill_paths,
            count=len(exec_result.retrieved_skill_paths),
        )
        collector.add_action(
            "cli_invoke",
            binary=self.binary_name,
            returncode=exec_result.returncode,
            timed_out=exec_result.timed_out,
        )
        for rel_path in exec_result.files:
            collector.add_action("write_file", path=rel_path, bytes=len(exec_result.files[rel_path]))
        stream_steps, cli_final_message = stream_json_events_to_steps(
            parse_stream_json_lines(exec_result.stdout)
        )
        if stream_steps:
            for step in stream_steps:
                collector.add_step(
                    role=step["role"],
                    content=step["content"][:MAX_TRAJECTORY_PROMPT_CHARS],
                    tool_calls=step["tool_calls"],
                    tool_results=step["tool_results"],
                    observation=step["observation"][:MAX_TRAJECTORY_PROMPT_CHARS],
                )
        else:
            collector.add_step(
                role="assistant",
                content=(exec_result.stdout or "")[:MAX_TRAJECTORY_PROMPT_CHARS],
                tool_calls=[
                    {"type": "function", "function": {"name": "cli_invoke", "arguments": {"binary": self.binary_name}}}
                ],
                observation=(exec_result.stdout or exec_result.stderr)[:300],
            )
        if exec_result.exception is not None:
            collector.add_exception(exec_result.exception, context="cli_subprocess")
        for event in exec_result.tool_events:
            collector.add_action("cli_event_record", **event)
        # 官方对齐 Part2/Part3：强制执行步骤的结果单独记为 execution_logs
        # （不是 agent 自己在 CLI session 里做的事，必须与 actions/cli_invoke 区分开）。
        if exec_result.forced_execution is not None:
            collector.add_execution_log("forced_solution_execution", **exec_result.forced_execution)
        collector.set_stdio(exec_result.stdout, exec_result.stderr)
        collector.add_tokens(exec_result.total_tokens, exec_result.cost_usd)
        generated_files_message = "\n".join(
            f"### {p}\n{c}" for p, c in exec_result.files.items()
        )
        collector.set_final_message(
            (cli_final_message or generated_files_message)[:MAX_TRAJECTORY_PROMPT_CHARS]
        )
        collector.add_action("verify", reward=reward)
        collector.add_step(
            role="user", content=f"[Verifier] reward={reward}",
            observation=verifier_output[:300],
        )
        collector.add_generated_files(list(exec_result.files.keys()))
        return collector.finalize(
            reward=reward,
            verifier_output=verifier_output,
            verifier_subtest_failures=verifier_subtest_failures,
        )

    # ------------------------------------------------------------------
    # cleanup()
    # ------------------------------------------------------------------

    def cleanup(self, workspace: WorkspaceManager) -> None:
        workspace.cleanup()

    # ------------------------------------------------------------------
    # 子类必须提供
    # ------------------------------------------------------------------

    @abstractmethod
    def build_argv(self, profile: WorkerProfile) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def build_env(self, profile: WorkerProfile) -> dict[str, str]:
        raise NotImplementedError

    def success_marker(self) -> str:
        """CLI 输出里表示成功完成的字符串标记；默认空字符串（不检测）。"""
        return ""

    @staticmethod
    def _resolve_api_key(profile: WorkerProfile) -> str:
        """从 profile.api_key_env 指定的环境变量名读取真实 key（.env 已加载）。"""
        return os.environ.get(profile.api_key_env, "")
