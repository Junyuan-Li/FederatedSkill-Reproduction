"""
base_harness.py — BaseAgentHarness：统一 Agent CLI Harness 接口（Part2 要求）

用户 Part2 原文要求的生命周期方法：
    class BaseAgentHarness:
        initialize()
        execute_task()
        collect_trajectory()
        cleanup()

本类额外提供一个**模板方法** run()（组合上面四步 + 复用既有验证逻辑），
统一对外入口，三个具体 CLI Harness（claude-code/qwen-code/kimi-cli）与
APIWorkspaceHarness 都实现同一个 `.run(task, library, profile, round_idx)
-> Trajectory` 签名，与 executor.base.BaseExecutor.run() 完全一致——这样
executor/harness_executor.py::HarnessAwareExecutor 才能对任意 harness
做 duck typing 调用，不需要 if/else 判断具体子类。

[ENGINEERING] 本模块标签：工程实现细节（Agent Runtime 层统一接口），不改变
论文给出的任何算法/奖励公式。verify() 复用（组合调用）既有
executor/agent_executor.py::AgentWorkspaceExecutor._verify()，与仓库既有
"组合复用私有方法、不修改源码"的约定一致（参见 executor/agent_executor.py
docstring 里对 client.executor.TaskExecutor 私有方法的复用方式）。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.datatypes import Trajectory, WorkerProfile
from executor.agent_executor import AgentWorkspaceExecutor
from executor.environment import WorkspaceManager
from executor.trajectory import TrajectoryCollector

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary

logger = logging.getLogger(__name__)

#: 官方对齐 Part2 强制执行步骤的 subprocess 超时（秒）。与
#: executor/skillflow_executor.py::_SOLUTION_EXEC_TIMEOUT（20s，API 模式）
#: 刻意不同——CLI 模式下 agent 生成的脚本可能涉及更重的 I/O（PDF/Excel 解析等，
#: 真实实验里观察到的情形），给更宽松的上限，但仍然是一个有界值，不是
#: agent 自身 CLI session 那个更大的 effective_timeout。
_FORCED_EXEC_TIMEOUT = 90.0


@dataclass
class HarnessExecutionResult:
    """execute_task() 的返回值：本次任务执行后工作区里的产物 + 原始 CLI 输出。"""

    files: dict[str, str] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    returncode: int | None = None
    timed_out: bool = False
    exception: Exception | None = None
    total_tokens: int = 0
    cost_usd: float = 0.0
    retrieved_skill_paths: list[str] = field(default_factory=list)
    # 官方对齐 Part2 新增：强制执行步骤的结果（见 BaseAgentHarness._force_execute_solution）。
    # None 表示尚未跑到该步骤；dict 内容见该方法 docstring。
    forced_execution: dict[str, Any] | None = None


class BaseAgentHarness(ABC):
    """
    Agent CLI Harness 统一接口。

    子类必须实现：
        initialize(task, profile)               -> WorkspaceManager
        execute_task(task, library, profile, ws) -> HarnessExecutionResult
        collect_trajectory(...)                  -> Trajectory
        cleanup(ws)                              -> None

    模板方法 run() 按顺序调用上面四步，并在 execute_task 与
    collect_trajectory 之间插入统一的验证（verify）步骤（复用
    AgentWorkspaceExecutor._verify()，不重新实现验证逻辑）。
    """

    #: 子类需要覆盖：agent_harness 名称（对应 WorkerProfile.agent_harness），
    #: 用于 factory.py 按名分派、以及 profile_hash 审计。
    harness_name: str = "base"

    def __init__(self, router: Any, top_k_skills: int = 3) -> None:
        self._router = router
        # 组合复用既有 AgentWorkspaceExecutor：
        #   1. 提供 ._verify()（验证/reward 计算，不重复实现）
        #   2. APIWorkspaceHarness 直接把 run() 整体委托给它（见该子类文档）
        # 不修改 AgentWorkspaceExecutor 源码一行。
        self._agent_ws_executor = AgentWorkspaceExecutor(router=router, top_k_skills=top_k_skills)

    # ------------------------------------------------------------------
    # 用户 Part2 要求的四个生命周期方法
    # ------------------------------------------------------------------

    @abstractmethod
    def initialize(self, task: "Task", profile: WorkerProfile) -> WorkspaceManager:
        """创建隔离工作区并写入 task.files（Environment 初始化）。"""
        raise NotImplementedError

    @abstractmethod
    def execute_task(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        workspace: WorkspaceManager,
    ) -> HarnessExecutionResult:
        """真正执行任务（真实 CLI subprocess 或 API 调用），返回工作区产物。"""
        raise NotImplementedError

    @abstractmethod
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
    ) -> Trajectory:
        """把执行结果 + 验证结果组装成论文要求的 Trajectory τ_i。"""
        raise NotImplementedError

    @abstractmethod
    def cleanup(self, workspace: WorkspaceManager) -> None:
        """清理工作区（幂等）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 模板方法：统一对外入口，与 executor.base.BaseExecutor.run() 签名一致
    # ------------------------------------------------------------------

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> Trajectory:
        workspace = self.initialize(task, profile)
        try:
            exec_result = self.execute_task(task, library, profile, workspace)
            main_file = self._resolve_main_file(task, exec_result.files)
            # 官方对齐 Part2：无论 agent 在 CLI session 内部是否已经"自己"执行过
            # 生成的代码、也无论 agent 最后一条消息是否自称成功，harness 都必须
            # 独立、确定性地强制执行一次生成的主文件，并把执行后工作区里新增/
            # 修改的产物重新读入 exec_result.files，再进入 verify()——真实实验
            # 发现的根因是 agent 只在 CLI session 里用零散 Bash/python -c 片段
            # 把答案打印到了 stdout，从未把结果真正写入 verifier 要求的输出文件
            # 就自称"solution works correctly"结束了 session；本步骤消除了对
            # agent 自我declare success 的依赖。
            self._force_execute_solution(workspace, main_file, exec_result)
            reward, verifier_output, subtest_failures = self._agent_ws_executor._verify(
                task, workspace, exec_result.files, main_file,
            )
            return self.collect_trajectory(
                task, profile, round_idx, workspace, exec_result,
                reward, verifier_output, subtest_failures,
            )
        finally:
            self.cleanup(workspace)

    @staticmethod
    def _resolve_main_file(task: "Task", files: dict[str, str]) -> str:
        main_file = task.metadata.get("solution_filename", "solution.py")
        if main_file not in files and files:
            main_file = next(iter(files))
        return main_file

    @staticmethod
    def _force_execute_solution(
        workspace: WorkspaceManager, main_file: str, exec_result: HarnessExecutionResult,
    ) -> None:
        """官方对齐 Part2：强制执行步骤（'3.执行生成代码 4.检查artifact'）。

        在 workspace 内以 cwd=workspace.path 强制执行一次 agent 生成的主文件
        （若为 .py 且确实存在于 exec_result.files 中），捕获 stdout/stderr，
        并把执行完成后工作区内新增/修改的所有文件重新读入
        exec_result.files——这样即使 agent 自己在 CLI session 里只是把正确
        结果打印到 stdout、从未真正落盘，只要它写的脚本本身逻辑正确，这一步
        强制重跑就会把该产物真正物化到磁盘，供随后的 verify() 检查。

        对非 .py 主文件（如 solve.sh）或找不到主文件的情况，不尝试执行，只
        如实记录 `forced_execution = {"executed": False, "reason": ...}`——
        不新增对 shell 解释器的隐式依赖假设。
        """
        if not main_file.endswith(".py") or main_file not in exec_result.files:
            exec_result.forced_execution = {
                "executed": False,
                "reason": f"未找到可强制执行的 .py 主文件（main_file={main_file!r}）",
            }
            return
        try:
            proc = subprocess.run(
                [sys.executable, main_file],
                cwd=str(workspace.path),
                capture_output=True, text=True, timeout=_FORCED_EXEC_TIMEOUT,
            )
            exec_result.forced_execution = {
                "executed": True,
                "returncode": proc.returncode,
                "stdout": proc.stdout[:2000],
                "stderr": proc.stderr[:2000],
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            exec_result.forced_execution = {
                "executed": True,
                "timed_out": True,
                "stdout": "",
                "stderr": f"强制执行 solution 超时（>{_FORCED_EXEC_TIMEOUT}s）",
            }
        except Exception as exc:  # noqa: BLE001 — 必须捕获任意执行期异常，如实记录，不让其向上冒泡中断 run()
            exec_result.forced_execution = {
                "executed": True,
                "timed_out": False,
                "stdout": "",
                "stderr": f"强制执行 solution 异常: {type(exc).__name__}: {exc}",
            }
        # 无论上面执行是否成功，都重新扫描工作区，把强制执行后工作区里的
        # 实际内容同步进 exec_result.files——不仅新增此前不存在的文件，
        # 也刷新此前已存在的文件（若强制执行改写了它们），确保 verify()/
        # collect_trajectory() 看到的是"强制执行之后"的真实磁盘状态，而不是
        # agent CLI session 结束那一刻的旧快照。
        for rel_path in workspace.diff_generated_files():
            exec_result.files[rel_path] = workspace.read_generated_file(rel_path)
