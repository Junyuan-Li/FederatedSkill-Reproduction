"""
agent.py — AgentRuntime：Planner-Action-Observation 循环

对应论文 Section 4.1.1:
    τ_i ~ π_i(·|L_i^t, ρ_i)

完整 agentic 循环：
    Task
      │
      ▼
    [Skill Retrieval]   — tool: skill_search
      │
      ▼
    [LLM Planner]       — backbone call → action (tool_call or final_answer)
      │
      ▼
    [Tool Execution]    — ToolRegistry.call(action.name, **action.args)
      │
      ▼
    [Observation]       — 工具返回结果写入 TrajectoryStep.observation
      │
      ▼
    循环直到 final_answer 或 max_steps
      │
      ▼
    [Verifier]          — R_{i,x}(τ) 由 TaskExecutor 调用

设计说明：
  - AgentRuntime 不关心 verification，只负责生成 Trajectory
  - 最大步数由 WorkerProfile.max_context_tokens 间接限制
  - 每一步的 tokens_used 写入 TrajectoryStep，后续被 compressor 剥离
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.datatypes import Trajectory, TrajectoryStep, WorkerProfile
from core.constants import K_STEP

if TYPE_CHECKING:
    from client.agent_runtime.tools import ToolRegistry
    from benchmark.task import Task
    from llm.backbone import LLMBackbone

logger = logging.getLogger(__name__)

# 每轮最大 agentic 步数（安全上限，K_STEP 用于压缩）
_MAX_AGENT_STEPS = 20


class AgentRuntime:
    """
    Agentic 执行运行时：在技能库 + profile 的条件下执行单个任务。

    论文核心抽象：
        π_i(·|L_i^t, ρ_i) — 以技能库和 profile 为条件的 agent 策略

    Args:
        backbone:       worker 的 LLM backbone（m_i，与 distiller 共享）
        tool_registry:  工具注册表（python_execute / skill_search / file_write）
        max_steps:      最大 agentic 步数（默认 _MAX_AGENT_STEPS = 20）
    """

    def __init__(
        self,
        backbone: "LLMBackbone",
        tool_registry: "ToolRegistry",
        max_steps: int = _MAX_AGENT_STEPS,
    ) -> None:
        self._backbone = backbone
        self._tools = tool_registry
        self._max_steps = max_steps

    def run(
        self,
        task: "Task",
        profile: WorkerProfile,
        round_idx: int = 0,
    ) -> Trajectory:
        """
        执行单个任务，返回完整原始轨迹 τ_i。

        论文流程（Algorithm 1 client 侧）：
            1. 技能检索：skill_search(task.required_skills)
            2. Planner LLM 调用：生成代码 / tool call
            3. 工具执行 + Observation 记录
            4. 重复直到 final_answer 或 max_steps

        Args:
            task:      本轮任务 x
            profile:   worker profile ρ_i
            round_idx: 当前 round 序号 t

        Returns:
            Trajectory τ_i（reward 字段留空，由 TaskExecutor 填充）
        """
        t_start = time.monotonic()
        worker_id = profile.client_id
        steps: list[TrajectoryStep] = []
        total_tokens = 0
        total_cost = 0.0
        final_message = ""
        exception_info: dict[str, Any] | None = None

        logger.info(
            "AgentRuntime.run: worker=%s task=%s round=%d",
            worker_id, task.task_id, round_idx,
        )

        # ── Step 0: 技能检索 ─────────────────────────────────────────────
        retrieval_query = " ".join(task.required_skills + [task.category])
        retrieval_result = self._tools.call("skill_search", query=retrieval_query)

        steps.append(TrajectoryStep(
            step_index=0,
            role="user",
            content=f"[Skill Retrieval] query={retrieval_query!r}",
            tool_calls=[{"name": "skill_search", "arguments": {"query": retrieval_query}}],
            tool_results=[{"output": retrieval_result.combined_output()}],
            observation=retrieval_result.stdout[:2000],
        ))

        # ── 构建初始 system + user prompt ────────────────────────────────
        system_prompt = self._build_system_prompt(profile, retrieval_result.stdout)
        user_prompt = self._build_user_prompt(task)

        # ── Planner-Action-Observation 循环 ──────────────────────────────
        for step_idx in range(1, self._max_steps + 1):
            try:
                llm_result = self._backbone.call(user_prompt, system_prompt)
                total_tokens += llm_result.total_tokens
                total_cost += llm_result.cost_usd
                response_text = llm_result.text

                # 解析 LLM 输出（代码块 or 直接回答）
                code_blocks = self._extract_code_blocks(response_text)
                tool_calls_made: list[dict[str, Any]] = []
                tool_results_made: list[dict[str, Any]] = []
                observation = ""

                if code_blocks:
                    # 执行第一个代码块
                    code = code_blocks[0]
                    exec_result = self._tools.call("python_execute", code=code)
                    tool_calls_made = [{"name": "python_execute", "arguments": {"code": code[:200] + "..."}}]
                    tool_results_made = [{"exit_code": exec_result.exit_code, "output": exec_result.combined_output()[:1000]}]
                    observation = exec_result.combined_output()

                    # 将执行结果反馈给下一轮 prompt
                    user_prompt = self._build_followup_prompt(task, response_text, exec_result.combined_output())

                    if exec_result.success or step_idx >= self._max_steps - 1:
                        # 执行成功或接近步数上限 → 结束循环
                        final_message = response_text
                        steps.append(TrajectoryStep(
                            step_index=step_idx,
                            role="assistant",
                            content=response_text[:3000],
                            tool_calls=tool_calls_made,
                            tool_results=tool_results_made,
                            observation=observation[:2000],
                            tokens_used=llm_result.total_tokens,
                        ))
                        if exec_result.success:
                            break
                        continue
                else:
                    # 无代码块 → 视为最终回答
                    final_message = response_text
                    steps.append(TrajectoryStep(
                        step_index=step_idx,
                        role="assistant",
                        content=response_text[:3000],
                        observation="(final answer, no tool call)",
                        tokens_used=llm_result.total_tokens,
                    ))
                    break

                steps.append(TrajectoryStep(
                    step_index=step_idx,
                    role="assistant",
                    content=response_text[:3000],
                    tool_calls=tool_calls_made,
                    tool_results=tool_results_made,
                    observation=observation[:2000],
                    tokens_used=llm_result.total_tokens,
                ))

            except Exception as exc:
                logger.error("AgentRuntime step %d 异常: %s", step_idx, exc)
                exception_info = {
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
                steps.append(TrajectoryStep(
                    step_index=step_idx,
                    role="assistant",
                    content=f"[异常] {exc}",
                ))
                break

        runtime = time.monotonic() - t_start
        logger.info(
            "AgentRuntime 完成: worker=%s steps=%d tokens=%d cost=%.4f time=%.1fs",
            worker_id, len(steps), total_tokens, total_cost, runtime,
        )

        return Trajectory(
            task_name=task.task_id,
            worker_id=worker_id,
            round_idx=round_idx,
            steps=steps,
            final_message=final_message,
            total_tokens=total_tokens,
            runtime_seconds=runtime,
            cost_usd=total_cost,
            exception_info=exception_info,
            # reward 字段由 TaskExecutor 在 verification 后填充
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _build_system_prompt(self, profile: WorkerProfile, retrieved_skills: str) -> str:
        """
        构建 system prompt，融入检索到的技能（§4.1.1 条件化策略）。

        不同 harness 的 system prompt 风格不同（qwen-code vs kimi-cli vs claude-code）。
        """
        harness_hints = {
            "qwen-code":   "你是 Qwen Code 模式。优先输出结构化 Python 代码，注释用中文。",
            "kimi-cli":    "你是 Kimi CLI 模式。直接输出可运行脚本，避免复杂解释。",
            "claude-code": "你是 Claude Code 模式。分步骤说明，最后给出完整可运行代码。",
        }
        hint = harness_hints.get(profile.agent_harness, "输出完整可运行的 Python 代码。")

        skills_section = ""
        if retrieved_skills and retrieved_skills.strip() != "（技能库中未找到相关技能）":
            skills_section = f"\n\n## 可用技能库\n{retrieved_skills[:3000]}"

        return (
            f"你是一个 Python 编程 agent。{hint}\n"
            f"worker_id: {profile.client_id} | backbone: {profile.backbone_model}"
            f"{skills_section}\n\n"
            "规则：\n"
            "1. 用 ```python ... ``` 代码块包裹所有 Python 代码\n"
            "2. 代码必须完整可独立运行\n"
            "3. 若需要定义函数，确保函数名与任务要求完全一致"
        )

    def _build_user_prompt(self, task: "Task") -> str:
        """构建初始任务 user prompt。"""
        return (
            f"## 任务\n{task.description}\n\n"
            f"任务 ID: {task.task_id}  |  类别: {task.category}  |  难度: {task.difficulty}\n\n"
            "请用 Python 代码解决上述任务。用 ```python ... ``` 包裹代码块。"
        )

    def _build_followup_prompt(
        self, task: "Task", prev_response: str, exec_output: str
    ) -> str:
        """执行失败后的跟进 prompt（反馈执行结果）。"""
        return (
            f"上一次代码执行结果：\n```\n{exec_output[:1000]}\n```\n\n"
            "请根据执行结果修正代码。确保：\n"
            f"1. 函数名完全符合要求：{task.description[:200]}\n"
            "2. 代码用 ```python ... ``` 包裹\n"
            "3. 代码完整可独立运行"
        )

    @staticmethod
    def _extract_code_blocks(text: str) -> list[str]:
        """从 LLM 响应中提取所有 ```python ... ``` 代码块。"""
        pattern = re.compile(r"```python\s*(.*?)```", re.DOTALL | re.IGNORECASE)
        blocks = pattern.findall(text)
        if blocks:
            return [b.strip() for b in blocks if b.strip()]
        # 降级：提取裸 ``` 块
        bare = re.compile(r"```\s*(.*?)```", re.DOTALL)
        return [b.strip() for b in bare.findall(text) if b.strip()]
