"""
agent_executor.py — AgentWorkspaceExecutor：真实 agent workspace 模式执行器

    Model -> Agent Framework -> Skill Retrieval -> Tool Calling -> Environment -> Test

具体流程（相比 client/executor.py::TaskExecutor 的 5 步，新增了真实工作区
和多文件 Tool Calling 语义）：

  Step 1  Environment 初始化 —— WorkspaceManager 创建隔离工作区，
          写入 task.files（对应真实 SkillFlow 任务的 environment/ 输入）
  Step 2  Skill Retrieval    —— 复用 client.executor.TaskExecutor 的检索逻辑
            
  Step 3  Prompt Build       —— 要求模型以 ```python:<path>``` 多文件格式输出
  Step 4  LLM Generation     —— 解析多文件输出，每个文件 write_file 落盘到工作区
                                （Tool Calling：每次写文件都是一次 action）
  Step 5  Command Execution  —— CommandRunner 在工作区内以 cwd=workspace 执行
                                主解答文件

  Step 6  Verification       —— skillflow_script 任务在工作区内执行 test_script；
                                其余任务类型复用 benchmark.verifier 框架
  Step 7  Environment 清理   —— diff 出本次新生成的文件（generated_files），
                                随后清理工作区

对应论文公式：
    τ_i ~ π_i(·|L_i^t, ρ_i)

返回值使用 TrajectoryCollector 组装，Trajectory 包含
actions / tool_calls / generated_files / exceptions / verification /
reward / token_usage（Phase12 新增字段，见 core/datatypes.py）。

"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from benchmark.verifier import SkillFlowScriptVerifier
from client.executor import TaskExecutor as _BaseTaskExecutor
from core.constants import MAX_TRAJECTORY_PROMPT_CHARS
from core.datatypes import WorkerProfile
from core.exceptions import LLMCallError
from executor.base import BaseExecutor
from executor.environment import WorkspaceManager
from executor.runner import CommandRunner
from executor.trajectory import TrajectoryCollector
from llm.router import BackboneRouter

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary
    from core.datatypes import Trajectory

logger = logging.getLogger(__name__)

_DEFAULT_MAIN_FILE = "solution.py"
_SOLUTION_EXEC_TIMEOUT = 20

# 匹配 ```python:relative/path.py\n<内容>``` 形式的多文件代码块；
# 文件名标注可选——未标注时退化为单文件模式（兼容旧版单文件 prompt 习惯）。
_FILE_BLOCK_RE = re.compile(
    r"```(?:python)?(?::(?P<fname>[^\n`]+))?\s*\n(?P<content>.*?)```",
    re.DOTALL | re.IGNORECASE,
)


class AgentWorkspaceExecutor(BaseExecutor):
    """
    真实 agent workspace 模式执行器：Model -> Agent Framework -> Skill
    Retrieval -> Tool Calling -> Environment -> Test。

    Args:
        router:          BackboneRouter，按 worker_id 路由 LLM 调用
        top_k_skills:    检索时返回的最大相关技能数
        command_timeout: 工作区内命令执行超时（秒）
    """

    def __init__(
        self,
        router: BackboneRouter,
        top_k_skills: int = 3,
        command_timeout: int = _SOLUTION_EXEC_TIMEOUT,
    ) -> None:
        self._router = router
        # 组合复用 TaskExecutor 的技能检索 / prompt 构建 / 代码提取逻辑
        self._helper = _BaseTaskExecutor(router, top_k_skills=top_k_skills)
        self._runner = CommandRunner(default_timeout=command_timeout)

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> "Trajectory":
        worker_id = profile.client_id
        collector = TrajectoryCollector(task.task_id, worker_id, round_idx)
        logger.info(
            "AgentWorkspaceExecutor: worker=%s task=%s round=%d",
            worker_id, task.task_id, round_idx,
        )

        with WorkspaceManager(prefix=f"agentws_{task.task_id}_") as ws:
            # -- Step 1: Environment 初始化 --
            ws.write_input_files(task.files)
            collector.add_action(
                "setup_workspace", workspace=str(ws.path),
                input_files=list(task.files.keys()),
            )
            ws.snapshot()

            # -- Step 2: Skill Retrieval --
            relevant_entries = self._helper._retrieve_skills(task, library)
            skills_text = self._helper._format_skills(relevant_entries)
            collector.add_step(
                role="user",
                content=f"[Skill Retrieval] 检索到 {len(relevant_entries)} 个相关技能",
                observation="\n".join(e.path for e in relevant_entries) if relevant_entries else "（无相关技能）",
            )
            collector.add_action("skill_retrieval", retrieved=[e.path for e in relevant_entries])

            # -- Step 3: Prompt Build（多文件工作区格式） --
            system_prompt = self._helper._build_system_prompt(profile)
            user_prompt = self._build_workspace_prompt(task, skills_text)
            collector.add_step(role="user", content=f"[Prompt] {user_prompt[:300]}…")

            # -- Step 4: LLM Generation（多文件解析 + write_file 落盘） --
            files: dict[str, str] = {}
            try:
                backbone = self._router.get(worker_id)
                llm_result = backbone.call(user_prompt, system_prompt)
                collector.add_tokens(llm_result.total_tokens, llm_result.cost_usd)
                files = self._extract_files(llm_result.text)
                collector.add_step(
                    role="assistant",
                    content=llm_result.text[:MAX_TRAJECTORY_PROMPT_CHARS],
                    tokens_used=llm_result.total_tokens,
                    tool_calls=[
                        {"type": "function", "function": {"name": "write_file", "arguments": {"path": p}}}
                        for p in files
                    ],
                )
            except LLMCallError as exc:
                logger.error("LLM 调用失败 worker=%s task=%s: %s", worker_id, task.task_id, exc)
                collector.add_exception(exc, context="llm_generation")
                collector.add_step(role="assistant", content=f"[LLM 调用失败] {exc}")

            for rel_path, content in files.items():
                ws.write_file(rel_path, content)
                collector.add_action("write_file", path=rel_path, bytes=len(content))

            main_file = task.metadata.get("solution_filename", _DEFAULT_MAIN_FILE)
            if main_file not in files and files:
                main_file = next(iter(files))

            # -- Step 5: Command Execution（真实 subprocess，cwd=workspace） --
            exec_stdout, exec_stderr = "", ""
            if files:
                result = self._runner.run_python_file(main_file, cwd=ws.path)
                collector.add_action(
                    "run_command", command=result.command, returncode=result.returncode,
                    timed_out=result.timed_out,
                )
                if result.exception:
                    collector.add_exception(
                        RuntimeError(result.stderr), context="run_solution",
                    )
                exec_stdout, exec_stderr = result.stdout, result.stderr
                collector.add_step(
                    role="tool", content=main_file,
                    tool_calls=[{"type": "function", "function": {"name": "run_command", "arguments": {"file": main_file}}}],
                    tool_results=[{"stdout": exec_stdout[:500], "stderr": exec_stderr[:500]}],
                    observation=(exec_stdout or exec_stderr)[:300],
                )
            collector.set_stdio(exec_stdout, exec_stderr)
            collector.set_final_message(
                "\n".join(f"### {p}\n{c}" for p, c in files.items())[:1000]
            )

            # -- Step 6: Verification --
            reward, verifier_output, subtest_failures = self._verify(task, ws, files, main_file)
            collector.add_action("verify", reward=reward)
            collector.add_step(
                role="user", content=f"[Verifier] reward={reward}",
                observation=verifier_output[:300],
            )

            # -- Step 7: Environment 清理前，记录本次新生成的文件 --
            collector.add_generated_files(ws.diff_generated_files())

            trajectory = collector.finalize(
                reward=reward,
                verifier_output=verifier_output,
                verifier_subtest_failures=subtest_failures,
            )
            logger.info(
                "AgentWorkspaceExecutor 完成: worker=%s task=%s reward=%.1f files=%d",
                worker_id, task.task_id, reward, len(trajectory.generated_files),
            )
            return trajectory

    # ------------------------------------------------------------------
    # Prompt 构建（多文件工作区格式）
    # ------------------------------------------------------------------

    def _build_workspace_prompt(self, task: "Task", skills_text: str) -> str:
        input_files = "、".join(task.files.keys()) if task.files else "（无预置输入文件）"
        return (
            f"## 任务\n{task.description}\n\n"
            f"## 工作区已有输入文件\n{input_files}\n\n"
            f"## 已有技能参考\n{skills_text}\n\n"
            "## 输出格式（多文件工作区模式）\n"
            "如果需要生成多个文件，请为每个文件单独使用一个代码块，并在代码块起始处标注相对路径：\n"
            "```python:solution.py\n<文件内容>\n```\n"
            "如果只需要一个文件，也请使用同样的标注格式（文件名建议为 solution.py）。\n"
            "只输出代码块，不要输出代码块之外的解释文字。\n"
        )

    # ------------------------------------------------------------------
    # 多文件解析
    # ------------------------------------------------------------------

    def _extract_files(self, llm_response: str) -> dict[str, str]:
        """
        解析 LLM 响应里的多文件代码块。若模型未按 ```python:path``` 格式标注
        文件名，退化为单文件模式（复用 TaskExecutor._extract_code，写入
        solution.py），保证对未遵循新格式的模型输出依然健壮。
        """
        matches = list(_FILE_BLOCK_RE.finditer(llm_response))
        named = [m for m in matches if m.group("fname")]
        if named:
            files: dict[str, str] = {}
            for m in named:
                fname = m.group("fname").strip()
                if fname:
                    files[fname] = m.group("content").strip() + "\n"
            if files:
                return files

        code = self._helper._extract_code(llm_response)
        if code.strip():
            return {_DEFAULT_MAIN_FILE: code}
        return {}

    # ------------------------------------------------------------------
    # 验证
    # ------------------------------------------------------------------

    def _verify(
        self, task: "Task", ws: WorkspaceManager, files: dict[str, str], main_file: str,
    ) -> tuple[float, str, list[str]]:
        """
        计算 R_{i,x}(τ)。

        skillflow_script 任务：在工作区内执行 test_script
        其余任务类型：复用 benchmark.verifier 框架（与 client.executor.TaskExecutor
        保持一致的评分口径）。
        """
        spec = task.verification
        if spec.type == "skillflow_script":
            if not spec.test_script:
                return 0.0, "skillflow_script 缺少 test_script", []
            verification = SkillFlowScriptVerifier().verify_in_workspace(task, ws.path)
            output = f"stdout={verification.stdout}\nstderr={verification.stderr}"
            failures = verification.subtest_failures or (
                [] if verification.success else [verification.stderr[:200] or "非零退出码"]
            )
            return verification.reward, output, failures

        from benchmark.verifier import get_verifier

        generated_code = files.get(main_file, "")
        if not generated_code.strip():
            return 0.0, "生成代码为空", []
        try:
            verifier = get_verifier(spec.type)
            result = verifier.verify(task, generated_code)
            return result.reward, str(result), result.subtest_failures
        except Exception as exc:
            logger.error("验证器异常 task=%s: %s", task.task_id, exc)
            return 0.0, f"验证器内部异常: {exc}", []
