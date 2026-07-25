"""
skillflow_executor.py — 真实 SkillFlow 任务执行器（默认 subprocess 隔离，可选 Docker）

对应论文 τ_i ~ π_i(·|L_i^t, ρ_i)，用于跑通过 benchmark/skillflow_adapter/ 转换出的
真实 SkillFlow 任务（VerificationSpec.type == "skillflow_script" / "docker"）。

默认不使用 Harbor/容器，用 subprocess + 临时目录代替：
    1. 创建临时工作区目录
    2. 把 task.files（真实任务 environment/ 输入文件）写入工作区
    3. 技能检索 + 构建 prompt（复用 client.executor.TaskExecutor 的内部逻辑，不重复实现）
    4. 调用 LLM 生成解答，写入工作区内的 solution 文件
    5. 根据 verification.type 选择验证器（_get_verifier_for）：
         - "skillflow_script"（默认）：在工作区目录内以 subprocess 执行 test_script
         - "docker"（Gap2 新增，可选）：真实 Docker 容器构建 + 执行，
           见 executor/harbor_adapter.py::HarborExecutor；本机无 docker 时明确失败，
           不会静默退化为 subprocess 假装通过
    6. 收集 reward，删除临时工作区


"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from client.executor import TaskExecutor as _BaseTaskExecutor
from benchmark.verifier import SkillFlowScriptVerifier
from core.datatypes import Trajectory, TrajectoryStep, WorkerProfile
from core.exceptions import LLMCallError
from executor.environment import WorkspaceManager
from llm.router import BackboneRouter

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary

logger = logging.getLogger(__name__)

_DEFAULT_SOLUTION_FILENAME = "solution.py"
_SOLUTION_EXEC_TIMEOUT = 20


class SkillFlowTaskExecutor:
    """真实 SkillFlow 任务执行器：subprocess + 临时工作区，替代 Harbor/Docker 容器隔离。"""

    def __init__(self, router: BackboneRouter, top_k_skills: int = 3) -> None:
        self._router = router
        # 复用 TaskExecutor 的技能检索 / prompt 构建 / 代码提取逻辑，避免重复实现。
        # （通过组合调用，不修改 client/executor.py 本身）
        self._helper = _BaseTaskExecutor(router, top_k_skills=top_k_skills)
        self._verifier = SkillFlowScriptVerifier()

    def _get_verifier_for(self, task: "Task"):
        """
        按 task.verification.type 选择验证器（Gap2 新增分支）。

        - "docker" -> DockerScriptVerifier（真实 Docker 容器构建 + 执行，
          见 executor/harbor_adapter.py::HarborExecutor；本机无 docker 时
          会明确失败，不会静默退化为 subprocess）。
        - 其余（默认 "skillflow_script"）-> 沿用既有 SkillFlowScriptVerifier
          （subprocess 隔离，不改动其行为）。
        """
        if task.verification.type == "docker":
            from benchmark.verifier import DockerScriptVerifier
            return DockerScriptVerifier()
        return self._verifier

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> Trajectory:
        t_start = time.monotonic()
        worker_id = profile.client_id
        steps: list[TrajectoryStep] = []
        total_tokens = 0
        total_cost = 0.0

        logger.info(
            "SkillFlowTaskExecutor: worker=%s task=%s round=%d",
            worker_id, task.task_id, round_idx,
        )

        workspace_manager = WorkspaceManager(prefix=f"skillflow_{task.task_id}_")
        workspace = workspace_manager.path
        try:
            # -- Step 1: 复制任务输入文件到临时工作区 --
            source_environment_dir = task.metadata.get("source_environment_dir")
            if source_environment_dir:
                workspace_manager.copy_input_tree(source_environment_dir)
            else:
                workspace_manager.write_input_files(task.files)
            workspace_manager.snapshot()
            initial_files = self._snapshot_workspace_files(workspace)
            steps.append(TrajectoryStep(
                step_index=0,
                role="user",
                content=f"[Workspace] 已物化 {len(initial_files)} 个输入文件到临时目录",
                observation=str(workspace),
            ))

            # -- Step 2: 技能检索 + Prompt 构建 --
            relevant_entries = self._helper._retrieve_skills(task, library)
            skills_text = self._helper._format_skills(relevant_entries)
            skill_paths = [entry.path for entry in relevant_entries]
            steps.append(TrajectoryStep(
                step_index=1,
                role="user",
                content=f"[Skill Retrieval] 检索到 {len(relevant_entries)} 个相关技能",
                observation="\n".join(skill_paths) if skill_paths else "（无相关技能）",
            ))
            system_prompt = self._build_system_prompt(profile)
            user_prompt = self._build_user_prompt(task, skills_text, solution_filename=_DEFAULT_SOLUTION_FILENAME)
            steps.append(TrajectoryStep(
                step_index=2,
                role="user",
                content=f"[Prompt] {user_prompt[:300]}…",
            ))

            # -- Step 3: LLM 生成解答 --
            generated_code = ""
            exception_info: dict | None = None
            try:
                backbone = self._router.get(worker_id)
                llm_result = backbone.call(user_prompt, system_prompt)
                total_tokens += llm_result.total_tokens
                total_cost += llm_result.cost_usd
                generated_code = self._helper._extract_code(llm_result.text)
                steps.append(TrajectoryStep(
                    step_index=3,
                    role="assistant",
                    content=llm_result.text[:2000],
                    tokens_used=llm_result.total_tokens,
                ))
            except LLMCallError as exc:
                logger.error("LLM 调用失败 worker=%s task=%s: %s", worker_id, task.task_id, exc)
                exception_info = {"exception_type": type(exc).__name__, "exception_message": str(exc)}
                steps.append(TrajectoryStep(
                    step_index=3,
                    role="assistant",
                    content=f"[LLM 调用失败] {exc}",
                ))

            # -- Step 4: 把生成的解答写入工作区 --
            solution_filename = task.metadata.get("solution_filename", _DEFAULT_SOLUTION_FILENAME)
            (workspace / solution_filename).write_text(generated_code, encoding="utf-8")
            steps.append(TrajectoryStep(
                step_index=4,
                role="tool",
                content=generated_code[:2000],
                observation=f"[Solution] 已写入 {solution_filename}",
            ))

            # -- Step 4b: 在工作区目录内执行 solution（cwd=workspace，让相对路径读写
            #    task.files 里的输入文件），产出的中间文件供随后 test_script 验证 --
            exec_stdout, exec_stderr = self._run_solution(workspace, solution_filename)
            steps.append(TrajectoryStep(
                step_index=5,
                role="tool",
                content=f"[Run Solution] stdout={exec_stdout[:200]!r} stderr={exec_stderr[:200]!r}",
            ))

            # Phase14 新增：生成文件检测——对比 solution 执行前后的工作区快照，
            # 填入 Trajectory 已有但本执行器一直没填过的 generated_files 字段
            # （字段本身早已存在于 core/datatypes.py::Trajectory，仅
            # AgentWorkspaceExecutor 填充过，现在本 executor 也一致填充，
            # 不改动 Trajectory 任何字段定义/默认值）。
            generated_files = [
                rel for rel in workspace_manager.diff_generated_files()
                if rel != solution_filename
            ]

            # -- Step 5: 根据 verification.type 选择验证器并执行 --
            #    skillflow_script（默认，subprocess 隔离，无 Docker）/
            #    docker（Gap2 新增：真实 Docker 容器构建 + 执行，见 executor/harbor_adapter.py）
            verifier = self._get_verifier_for(task)
            verification = verifier.verify_in_workspace(task, workspace)
            reward = verification.reward
            steps.append(TrajectoryStep(
                step_index=6,
                role="user",
                content=f"[Verifier] {verification}",
                observation=(verification.stdout or verification.stderr)[:300],
            ))

            elapsed = time.monotonic() - t_start
            logger.info(
                "SkillFlowTaskExecutor 完成: worker=%s task=%s reward=%.1f elapsed=%.2fs",
                worker_id, task.task_id, reward, elapsed,
            )

            return Trajectory(
                task_name=task.task_id,
                worker_id=worker_id,
                round_idx=round_idx,
                steps=steps,
                stdout=(exec_stdout + "\n" + verification.stdout)[:2000],
                stderr=(exec_stderr + "\n" + verification.stderr)[:2000],
                final_message=generated_code[:1000],
                reward=reward,
                verifier_output=str(verification),
                total_tokens=total_tokens,
                runtime_seconds=elapsed,
                cost_usd=total_cost,
                exception_info=exception_info,
                generated_files=generated_files,
            )
        finally:
            # -- Step 6: 删除临时工作区（无论成功/失败都清理） --
            workspace_manager.cleanup()

    def _build_system_prompt(self, profile: WorkerProfile) -> str:
        harness = profile.agent_harness.lower()
        if "claude" in harness:
            style = "Write compact, direct Python scripts and verify filesystem side effects."
        elif "qwen" in harness:
            style = "Write complete, efficient Python scripts that operate on local files."
        else:
            style = "Write correct Python scripts that operate on local files."

        return (
            "You are solving a SkillFlow workspace task. "
            f"{style}\n"
            "Return exactly one Python code block for solution.py.\n"
            "The code will be written into the task workspace and executed with cwd set to that workspace.\n"
            "Use relative paths from the current working directory. Do not use `/root/`, `/tmp/`, "
            "Windows drive letters, or any absolute path.\n"
            "Create the output files requested by the task before exiting. After writing an output file, "
            "re-open it or otherwise validate that it exists and has the expected schema.\n"
            "Treat the retrieved skills as mandatory operating procedures unless they conflict with the task.\n"
        )

    def _build_user_prompt(self, task: "Task", skills_text: str, solution_filename: str) -> str:
        input_files = sorted(task.files)
        if input_files:
            files_section = "\n".join(f"- {path}" for path in input_files)
        else:
            files_section = "(No text input files are declared; inspect the current working directory if needed.)"

        return (
            f"## Task\n{task.description}\n\n"
            f"## Workspace Contract\n"
            f"- Your code will be saved as `{solution_filename}` and run from the workspace root.\n"
            "- Read inputs using relative paths only. Never hardcode `/root/` or any absolute path.\n"
            "- Produce the exact output file(s) requested by the task description.\n"
            "- Do not print an answer instead of creating the required output file unless the task explicitly asks for stdout.\n"
            "- Before finishing, check that each required output file exists and can be parsed/read back.\n\n"
            f"## Available Input Files\n{files_section}\n\n"
            f"## Retrieved Skills\n{skills_text}\n\n"
            "## Response\n"
            "Return only a Python code block containing the complete script."
        )

    def _run_solution(self, workspace: Path, solution_filename: str) -> tuple[str, str]:
        """在 workspace 内以 cwd=workspace 执行 solution 文件，返回 (stdout, stderr)。

        与 client.executor.TaskExecutor._run_code_raw 不同之处：这里必须设置
        cwd=workspace，让 solution 里对 task.files 输入文件的相对路径读写
        （例如 open('input.txt')）落在同一个工作区目录内，而不是系统临时目录。
        """
        try:
            proc = subprocess.run(
                [sys.executable, solution_filename],
                cwd=str(workspace),
                capture_output=True, text=True, timeout=_SOLUTION_EXEC_TIMEOUT,
            )
            return proc.stdout[:2000], proc.stderr[:2000]
        except subprocess.TimeoutExpired:
            return "", f"solution 执行超时（>{_SOLUTION_EXEC_TIMEOUT}s）"
        except Exception as exc:
            return "", str(exc)

    @staticmethod
    def _snapshot_workspace_files(workspace: Path) -> dict[str, float]:
        """记录工作区内每个文件的 相对路径 -> mtime 快照，用于检测
        solution 执行前后新增/修改的文件（Phase14 generated files detection）。"""
        snapshot: dict[str, float] = {}
        for p in workspace.rglob("*"):
            if p.is_file():
                snapshot[str(p.relative_to(workspace))] = p.stat().st_mtime
        return snapshot


# Phase12：按用户要求的目录/类命名对外暴露 SkillFlowExecutor（等同于 SkillFlowTaskExecutor，
# 纯别名，不重命名/不改动已通过测试的原类，保持向后兼容）。
SkillFlowExecutor = SkillFlowTaskExecutor
