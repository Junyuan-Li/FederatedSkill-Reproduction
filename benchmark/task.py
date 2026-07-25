"""
task.py — Benchmark 任务数据模型

对应论文中的 x ∈ X（任务空间），以及每轮客户端执行的具体任务。

论文公式：
    τ_i ~ π_i(·|L_i^t, ρ_i)     （客户端 agent 基于技能库 + profile 执行任务）
    R_{i,x}(τ)                    （验证器评估执行奖励）
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator, field_validator


class TestCase(BaseModel):
    """单个函数测试用例——输入 + 期望输出。"""

    inputs: list[Any] = Field(description="位置参数列表，作为 *args 传入 entry_point")
    expected: Any = Field(description="期望返回值（使用 == 比较）")
    description: str = Field(default="", description="用例描述，出错时显示")


class VerificationSpec(BaseModel):
    """
    任务验证规格。

    支持六种验证方式：
      python_test      — 生成代码 + 内联 test_code 合并执行，exit(0)=成功
      function_test    — 注入 entry_point 函数并运行 test_cases 列表
      output_match     — 检查 stdout 是否包含 expected_output
      skillflow_script — 真实 SkillFlow 任务，subprocess 隔离执行 test_script（无 Docker）
      docker           — 真实 SkillFlow 任务，在真实 Docker 容器内构建镜像并执行 test_script
                         （Gap2：executor/harbor_adapter.py::HarborExecutor，无 Docker 时明确失败，不伪造成功）
      none             — 无验证脚本（数据集本身缺失官方 Harbor 测试时的诚实兜底，非 bug）
    """

    type: Literal[
        "python_test", "function_test", "output_match",
        "skillflow_script", "docker", "none",
    ] = "python_test"

    # python_test 模式：追加到生成代码后直接执行
    test_code: str | None = Field(
        default=None,
        description="完整测试 Python 脚本，追加到生成代码后执行。"
                    "约定：成功时 exit(0) 或正常结束，失败时 exit(1)。",
    )

    # function_test 模式
    entry_point: str | None = Field(
        default=None,
        description="function_test 模式下，生成代码应定义的函数名",
    )
    test_cases: list[TestCase] = Field(
        default_factory=list,
        description="function_test 模式下，自动构建的测试用例",
    )

    # output_match 模式
    expected_output: str | None = Field(
        default=None,
        description="output_match 模式下，检查 stdout 是否包含此字符串",
    )

    # skillflow_script 模式（真实 SkillFlow 任务，无 Docker，用 subprocess + 临时工作区代替）：
    # 在填好 task.files 的工作区目录里执行 test_script（cwd=workspace），
    # 退出码 0 视为通过。由 executor/skillflow_executor.py 调用，
    # 不走 benchmark/verifier.py 里针对单段生成代码的通用 verify() 接口。
    test_script: str | None = Field(
        default=None,
        description="skillflow_script/docker 模式下，在任务工作区/容器内执行的验证脚本内容"
                    "（对应真实 SkillFlow 任务 tests/ 目录下的验证脚本；docker 模式下"
                    "作为容器内 `sh -c` 执行的验证命令）",
    )

    # docker 模式专属（Gap2 新增）：本地镜像 tag，由 HarborExecutor 构建/复用。
    docker_image: str | None = Field(
        default=None,
        description="docker 模式下使用的本地镜像 tag（executor/harbor_adapter.py::HarborExecutor "
                    "会在任务工作区内执行 `docker build -t <docker_image> .`，要求工作区内含 Dockerfile）",
    )

    # [Runtime Protocol Alignment Issue4] 上限由 300 放宽到 3600：官方
    # SkillFlow [verifier] timeout_sec 抽样为 900-1200s，300s 的硬上限会让
    # 真实数据集的验证脚本被人为砍短。仍保留一个有限上限（不是无限
    # timeout），只是不再让它比官方数据集本身还严格。详见 timeout_policy_report.md。
    timeout_seconds: int = Field(default=10, ge=1, le=3600)

    @model_validator(mode="after")
    def _check_consistency(self) -> "VerificationSpec":
        if self.type == "none":
            return self  # 诚实兜底：数据集缺失官方 Harbor 测试时的显式无验证状态
        if self.type == "python_test" and not self.test_code:
            raise ValueError("python_test 模式需要提供 test_code")
        if self.type == "function_test" and not self.entry_point:
            raise ValueError("function_test 模式需要提供 entry_point")
        if self.type == "output_match" and not self.expected_output:
            raise ValueError("output_match 模式需要提供 expected_output")
        if self.type == "skillflow_script" and not self.test_script:
            raise ValueError("skillflow_script 模式需要提供 test_script")
        if self.type == "docker":
            if not self.docker_image:
                raise ValueError("docker 模式需要提供 docker_image")
            if not self.test_script:
                raise ValueError("docker 模式需要提供 test_script（容器内执行的验证命令）")
        return self


class Task(BaseModel):
    """
    论文中的单个任务 x ∈ X。

    FederatedSkill 使用 SkillFlow benchmark（workflow 型任务）：
    20 个 task family，每个 family 内是**同一技能**的递增难度序列，
    迫使 agent 跨轮演化同一份技能（而不是解决互不相关的孤立题目）。

    本复现的 benchmark/families/ 目录按同样的结构组织任务
    （见 benchmark/family.py 的 TaskFamily），family_id 字段
    标识任务所属的 family/workflow；difficulty 字段即 family 内的序号。
    """

    task_id: str = Field(description="任务唯一 ID，e.g. 'python_json_filter_001'")
    category: str = Field(
        default="",
        description="任务类别，e.g. 'data_processing' / 'file_ops' / 'text_analysis'"
    )
    family_id: str = Field(
        default="",
        description="所属 task family（对应论文 SkillFlow 的 workflow family）。"
                    "留空时自动等于 category，兼容旧版无 family 概念的任务文件。",
    )
    description: str = Field(description="自然语言任务描述，传给 agent 生成代码")
    required_skills: list[str] = Field(
        default_factory=list,
        description="完成该任务所需的技能 tag 列表，用于技能检索",
    )
    verification: VerificationSpec = Field(
        default_factory=lambda: VerificationSpec(type="none"),
        description="验证规格；SkillFlow 任务默认 type=none（Docker 验证由外部完成）",
    )
    difficulty: int = Field(
        default=1, ge=1, le=20,
        description="难度序号（family 内从 1 递增）。自建 benchmark 一般 1~5；"
                    "真实 SkillFlow family 有 8~9 个递增子任务，上限放宽到 20。",
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="本任务依赖的前置 task_id 列表（同一 family 内）。"
                    "⚠️ 本字段是 Phase12 自建扩展，并非论文原文或官方实现要求"
                    "（论文 Section 5.1 只描述 family 内任务难度递增、共享同一技能，"
                    "未给出形式化的任务依赖图；官方 TaskPartitioner 也不含依赖判定）。"
                    "默认空列表，向后兼容旧版 family JSON，不影响任何真实实验入口"
                    "（experiments/run_experiment.py、main_trainer.py 均不使用本字段）。"
                    "仅供 benchmark.family_sampler.FamilyAwareSampler 这一实验性/非核心"
                    "采样器使用，详见 docs/SIMPLIFICATIONS.md §2.4。",
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description="任务输入文件（相对路径 -> 文本内容），对应真实 SkillFlow 任务的 "
                    "environment/ 目录。仅供 executor/skillflow_executor.py 使用；"
                    "自建的 function_test/python_test/output_match 任务通常为空。"
                    "二进制文件（如 .xlsx/.pdf）不在此存储，见 metadata['binary_files']。",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("family_id")
    @classmethod
    def _default_family_id(cls, v: str, info: Any) -> str:
        if v:
            return v
        return str(info.data.get("category", ""))

    @property
    def is_easy(self) -> bool:
        return self.difficulty <= 2

    @property
    def is_hard(self) -> bool:
        return self.difficulty >= 4

    def __str__(self) -> str:
        return f"Task({self.task_id}, cat={self.category}, diff={self.difficulty})"
