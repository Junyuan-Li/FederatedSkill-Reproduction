"""
router_executor.py — 按 verification.type 分派执行器（[ENGINEERING] 纯组合/薄封装）

背景 bug（"最终论文一致性收口" Priority 1）：
    main_trainer.py::_build_executor() 和 experiments/run_experiment.py::
    run_experiment() 里，整个实验只构造**一个**固定的 client.executor.TaskExecutor，
    从来不会选用已经写好、测试覆盖良好的 executor/skillflow_executor.py::
    SkillFlowTaskExecutor。而 TaskExecutor._verify() 对
    verification.type in {"skillflow_script", "docker"} 的任务会调用
    SkillFlowScriptVerifier/DockerScriptVerifier 的 verify()，这两个方法本身就
    显式 raise NotImplementedError（要求改用 verify_in_workspace()），
    该异常被 TaskExecutor._verify() 的通用 except 捕获后包装成一条含糊的
    "验证器内部异常"，reward 记为 0.0——这会把"执行器选错了"误报成
    "验证器出错了"，掩盖真实原因。

    本模块新增 VerificationAwareExecutor：按 task.verification.type 在
    TaskExecutor 和 SkillFlowTaskExecutor 之间做**只读分派**（组合两个已有、
    已测试的类，不新增任何算法/奖励机制，不修改这两个类本身一行代码）：
        - "skillflow_script" / "docker"  → SkillFlowTaskExecutor（真实工作区流程）
        - 其余（"python_test"/"function_test"/"output_match"/"none"）
          → client.executor.TaskExecutor

    对 main_trainer.py / experiments/run_experiment.py 而言是纯粹的
    drop-in 替换：两边原来传的都是"一个具有 .run(task, library, profile,
    round_idx) 方法的对象"，FederatedRunner/SelfEvolutionRunner 从未假设
    过具体类型（duck typing），因此替换零风险、不影响任何已通过测试的
    core/server/client 接口。

Phase16「真实实验链路审计 + 端到端跑通」step1 补充说明：
    - 已确认真实实验入口（main_trainer.py::_build_executor()、
      experiments/run_experiment.py::run_experiment()）均已把本类作为
      唯一构造的执行器注入 SelfEvolutionRunner / FederatedRunner；
      experiments/federated.py::FederatedRunner 与 experiments/baseline.py::
      SelfEvolutionRunner 均不自行构造 executor，只是把注入进来的实例存进
      self._executor 并按 `self._executor.run(task, library, profile,
      round_idx)` 统一调用（duck typing），因此"路由是否生效"完全由
      调用方传入哪个实例决定——两个真实入口都已改传本类，链路已收口。
    - 分派粒度是 task.verification.type 而不是一个假设存在的
      `task.has_skillflow_environment` 布尔字段（core/datatypes.py /
      benchmark/task.py 未定义该字段，本类禁止修改 core/datatypes.py，
      因此不新增该字段）；效果等价：真实 SkillFlow 任务的 verification.type
      恒为 "skillflow_script"/"docker"。
    - `executor/mock_executor.py::MockExecutor` 故意**不**接入本路由——它是
      纯测试/开发工具（不调用真实 LLM/subprocess），docs/SIMPLIFICATIONS.md
      已记录其"不用于真实实验路径"的定位，接入会让真实实验静默产出假 reward，
      风险等同于本次修复的原始 bug，故维持不接入。
    - `executor/agent_executor.py::AgentWorkspaceExecutor`（Phase12 的多文件
      workspace 执行器）审计中同样发现**从未被任何真实入口调用**（只在
      tests/test_agent_executor.py 和文档中出现），是与 SkillFlowTaskExecutor
      完全同类的"建好但未接线"问题，但用户本次任务只要求收口
      SkillFlowTaskExecutor 与 DecisionLogger 两处，未要求接入
      AgentWorkspaceExecutor（其接口与 SkillFlowTaskExecutor 有重叠但不完全
      一致，接入需要额外设计决策），故本轮不扩大范围处理，仅在此如实记录，
      留给后续步骤或用户决定。
    - 新增 dispatch_log（只读调用轨迹）：每次 run() 记录一条
      {"task_id","verification_type","executor","round_idx"}，供
      tests/test_executor_routing_e2e.py 断言"真实 SkillFlow 任务确实经过
      SkillFlowTaskExecutor"，以及供人工调试时用 get_dispatch_trace() 打印
      trace，不影响任何 reward/Trajectory 计算。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from client.executor import TaskExecutor
from executor.skillflow_executor import SkillFlowTaskExecutor
from llm.router import BackboneRouter

if TYPE_CHECKING:
    from benchmark.task import Task
    from client.library import SkillLibrary
    from core.datatypes import Trajectory, WorkerProfile

logger = logging.getLogger(__name__)

# verification.type 需要真实工作区（task.files + 生成文件都落盘后再验证）的类型集合
_WORKSPACE_VERIFICATION_TYPES = frozenset({"skillflow_script", "docker"})


class VerificationAwareExecutor:
    """
    根据 task.verification.type 分派到 TaskExecutor 或 SkillFlowTaskExecutor
    的薄路由层，对外接口与两者完全一致：run(task, library, profile, round_idx)。
    """

    def __init__(self, router: BackboneRouter, top_k_skills: int = 3) -> None:
        self._standard = TaskExecutor(router=router, top_k_skills=top_k_skills)
        self._skillflow = SkillFlowTaskExecutor(router=router, top_k_skills=top_k_skills)
        # Phase16 新增：只读调用轨迹，不参与任何 reward/合并决策计算，
        # 纯粹为审计"这个 task 真的走了哪个 executor"而存在。
        self.dispatch_log: list[dict[str, object]] = []

    def run(
        self,
        task: "Task",
        library: "SkillLibrary",
        profile: "WorkerProfile",
        round_idx: int = 0,
    ) -> "Trajectory":
        if task.verification.type in _WORKSPACE_VERIFICATION_TYPES:
            chosen = "SkillFlowTaskExecutor"
            logger.info(
                "[ROUTE] task=%s verification=%s round=%d -> %s",
                task.task_id, task.verification.type, round_idx, chosen,
            )
            self.dispatch_log.append({
                "task_id": task.task_id,
                "verification_type": task.verification.type,
                "executor": chosen,
                "round_idx": round_idx,
            })
            return self._skillflow.run(task=task, library=library, profile=profile, round_idx=round_idx)

        chosen = "TaskExecutor"
        logger.info(
            "[ROUTE] task=%s verification=%s round=%d -> %s",
            task.task_id, task.verification.type, round_idx, chosen,
        )
        self.dispatch_log.append({
            "task_id": task.task_id,
            "verification_type": task.verification.type,
            "executor": chosen,
            "round_idx": round_idx,
        })
        return self._standard.run(task=task, library=library, profile=profile, round_idx=round_idx)

    def get_dispatch_trace(self) -> list[dict[str, object]]:
        """返回目前累积的调用轨迹副本（供调试/测试断言使用）。"""
        return list(self.dispatch_log)
