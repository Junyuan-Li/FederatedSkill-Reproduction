"""
trajectory.py — 轨迹压缩器（TrajectoryCompressor）

实现论文 Section 4.1.2 的轨迹压缩规格：

    'A compressed sequence retaining at most K_step agentic steps
     (the initial step plus the K_step − 1 most recent steps).
     Execution metrics are stripped, and environment observations are
     truncated to K_obs characters via an explicit <truncated> marker.'

与原版 SkillFlow TrajectoryCompactor 的区别：
  - 原版：CompactionConfig dataclass + 原版私有 API
  - 本版：TrajectoryCompressor 类，接口和参数命名完全对应论文符号
  - 新增：异常类型提取（exception_types 字段），供 TrialOutcome 使用
  - 新增：tool_call 净化（保留 function.name，去掉 function.arguments 和时间戳）
"""

from __future__ import annotations

from core.constants import K_OBS, K_STEP, TRUNCATION_MARKER
from core.datatypes import (
    CompactedTrajectory,
    Trajectory,
    TrajectoryStep,
)
from core.exceptions import TrajectoryError


class TrajectoryCompressor:
    """
    将原始轨迹 B_i^t 压缩为 CompactedTrajectory，供 PatchDistiller 使用。

    压缩保证：
      - 步骤数 ≤ K_step（初始步 + 最近 K_step-1 步）
      - 每步 observation 长度 ≤ K_obs（超出追加 TRUNCATION_MARKER）
      - tokens_used / 时间戳等执行指标清零（隐私保护）
      - tool_call 仅保留 function.name 和 type（去掉具体参数）

    Args:
        k_step: 保留的最大步骤数，对应论文常量 K_step，默认 20
        k_obs:  每步最大观察字符数，对应论文常量 K_obs，默认 3000
    """

    def __init__(
        self,
        k_step: int = K_STEP,
        k_obs: int = K_OBS,
    ) -> None:
        if k_step < 1:
            raise ValueError(f"k_step 必须 ≥ 1，当前: {k_step}")
        if k_obs < 1:
            raise ValueError(f"k_obs 必须 ≥ 1，当前: {k_obs}")
        self._k_step = k_step
        self._k_obs = k_obs

    @property
    def k_step(self) -> int:
        return self._k_step

    @property
    def k_obs(self) -> int:
        return self._k_obs

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def compress(self, trajectory: Trajectory) -> CompactedTrajectory:
        """
        主入口：将 Trajectory 压缩为 CompactedTrajectory。

        Raises:
            TrajectoryError: trajectory.steps 结构无效
        """
        if not isinstance(trajectory.steps, list):
            raise TrajectoryError(
                f"trajectory.steps 类型错误: {type(trajectory.steps).__name__}"
            )

        selected = self._select_steps(trajectory.steps)
        cleaned = [self._clean_step(s) for s in selected]
        exception_types = self._extract_exception_types(trajectory)

        return CompactedTrajectory(
            task_name=trajectory.task_name,
            worker_id=trajectory.worker_id,
            steps=cleaned,
            final_message=trajectory.final_message or "",
            exception_types=exception_types,
            original_step_count=len(trajectory.steps),
            k_step=self._k_step,
            k_obs=self._k_obs,
        )

    # ------------------------------------------------------------------
    # 步骤选取
    # ------------------------------------------------------------------

    def _select_steps(self, steps: list[TrajectoryStep]) -> list[TrajectoryStep]:
        """
        论文规格：初始步 + 最近 K_step-1 步。

        若步骤总数 ≤ K_step，则全部保留（无截断）。
        若步骤总数 > K_step，保留 steps[0] 和 steps[-(k_step-1):]。
        注意：若 k_step == 1，只保留初始步。
        """
        if not steps:
            return []
        total = len(steps)
        if total <= self._k_step:
            return list(steps)

        initial = steps[0:1]
        if self._k_step == 1:
            return initial

        recent_count = self._k_step - 1
        recent = steps[-recent_count:]
        # 避免重复（若轨迹极短，initial 和 recent 可能重叠）
        if recent and recent[0].step_index == initial[0].step_index:
            return list(steps[-self._k_step:])
        return initial + recent

    # ------------------------------------------------------------------
    # 步骤净化
    # ------------------------------------------------------------------

    def _clean_step(self, step: TrajectoryStep) -> TrajectoryStep:
        """
        对单步执行：
          1. 截断 observation 到 k_obs 字符，超出部分替换为 TRUNCATION_MARKER
          2. 清除 tokens_used（执行指标，不应入 patch）
          3. 净化 tool_calls（只保留 function.name，去掉参数和时间戳）
        """
        # 1. 观察截断
        obs = step.observation or ""
        if len(obs) > self._k_obs:
            obs = obs[: self._k_obs] + TRUNCATION_MARKER

        # 2. 净化 tool_calls（去掉 function.arguments 防泄漏具体参数）
        clean_tool_calls = self._strip_tool_call_args(step.tool_calls)

        # 3. tool_results 同样不暴露原始内容（保留结构，截断 content）
        clean_tool_results = self._truncate_tool_results(step.tool_results)

        return TrajectoryStep(
            step_index=step.step_index,
            role=step.role,
            content=step.content,
            tool_calls=clean_tool_calls,
            tool_results=clean_tool_results,
            observation=obs,
            tokens_used=0,  # 清零执行指标
        )

    @staticmethod
    def _strip_tool_call_args(tool_calls: list[dict]) -> list[dict]:
        """
        仅保留 tool_call 的 type 和 function.name，
        去掉 function.arguments（可能含任务特定数据）。
        """
        cleaned: list[dict] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            entry: dict = {}
            if "type" in tc:
                entry["type"] = tc["type"]
            fn = tc.get("function", {})
            if isinstance(fn, dict) and fn.get("name"):
                entry["function"] = {"name": fn["name"]}
            cleaned.append(entry)
        return cleaned

    @staticmethod
    def _truncate_tool_results(tool_results: list[dict]) -> list[dict]:
        """截断 tool_result 的 content 字段，最多保留 200 字符。"""
        cleaned: list[dict] = []
        for tr in tool_results:
            if not isinstance(tr, dict):
                continue
            entry = dict(tr)
            if "content" in entry and isinstance(entry["content"], str):
                if len(entry["content"]) > 200:
                    entry["content"] = entry["content"][:200] + TRUNCATION_MARKER
            cleaned.append(entry)
        return cleaned

    # ------------------------------------------------------------------
    # 异常类型提取
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_exception_types(trajectory: Trajectory) -> list[str]:
        """
        从 exception_info 字段提取异常类型名称。
        供 TrialOutcome.exception_types 使用，帮助 patcher 了解失败模式。
        """
        types: list[str] = []
        if trajectory.exception_info:
            et = trajectory.exception_info.get("exception_type")
            if et and isinstance(et, str):
                types.append(et)
        return types
