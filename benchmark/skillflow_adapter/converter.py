"""
converter.py — 将 RawSkillFlowTask 映射为 benchmark.task.Task

字段映射（真实 SkillFlow schema -> 本复现 Task 模型）：
    task_id      -> task_id
    family_id    -> family_id（同时作为 category，保持与自建 family 一致的路由方式）
    difficulty   -> difficulty
    instruction  -> description（Task 模型里承担"instruction"角色的字段）
    files        -> files
    test_script  -> verification.test_script（type="skillflow_script"）

没有新建并行的 TaskSchema 类——复用现有 Task/VerificationSpec，
与自建 family benchmark 走同一套 TaskFamily / FamilyCurriculumSampler /
evaluation 下游流程，避免维护两套任务表示。

[ENGINEERING] convert_environment()/convert_tests() 是把原本内联在
to_task() 里的两段映射逻辑拆成的具名函数（对应用户任务2的显式要求，
便于单独测试/审计），行为与拆分前完全一致，不改变 Task/VerificationSpec
schema，也不改变 to_task() 的最终输出。
"""

from __future__ import annotations

from benchmark.skillflow_adapter.parser import RawSkillFlowTask
from benchmark.task import Task, VerificationSpec


def convert_environment(raw: RawSkillFlowTask) -> dict[str, str]:
    """
    [ENGINEERING] 把官方 task/environment/ 目录的解析结果转换为 Task.files。

    真正的目录读取（文本/二进制文件区分）发生在
    `parser.py::parse_task_dir()` → `_read_text_files()`；本函数只做
    "raw.files（已经是 {相对路径: 文本内容}）→ Task.files" 这一步字段映射，
    不重新扫描磁盘，避免与 parser.py 重复实现同一段逻辑。二进制文件
    （如 .xlsx/.pdf）不在此返回，由调用方从 `raw.binary_files`
    单独记录进 `Task.metadata['binary_files']`。
    """
    return dict(raw.files)


def convert_tests(raw: RawSkillFlowTask) -> VerificationSpec:
    """
    [ENGINEERING] 把官方 task/tests/ 目录的解析结果转换为 VerificationSpec。

    真正的脚本拼接发生在 `parser.py::parse_task_dir()` →
    `_read_test_script()`（把 tests/ 下所有 .py，或没有 .py 时的 .sh，
    按文件名排序拼接成一段脚本文本）；本函数只做
    "raw.test_script（已经是拼接好的脚本文本）→ VerificationSpec" 这一步
    字段映射。

    保持 type="skillflow_script"（本仓库既有的、无 Docker 依赖的验证方式，
    由 executor/skillflow_executor.py 在临时工作区里 subprocess 执行），
    没有新增 type="docker" 或落盘脚本文件到 script_path 的路径式验证——
    真实 SkillFlow 任务的验证脚本内容直接内嵌进 VerificationSpec.test_script
    字段（与本仓库其余 skillflow_script 任务的既有约定一致，不是另起一套
    表示；若未来要接 executor/harbor_adapter.py::HarborExecutor 的真实
    Docker 验证，需要的是 Dockerfile + docker_image，而不是脚本路径，
    详见 benchmark/task.py::VerificationSpec 的 docker 模式）。
    """
    return VerificationSpec(
        type="skillflow_script",
        test_script=raw.test_script,
        # [Runtime Protocol Alignment Issue4] 不再 clamp 到 300s——官方
        # [verifier] timeout_sec 抽样为 900-1200s，人为砍到 300s 会让验证
        # 脚本较重的任务被误判超时失败。VerificationSpec.timeout_seconds
        # 的上限已同步放宽（见 benchmark/task.py），详见 timeout_policy_report.md。
        timeout_seconds=int(max(raw.verifier_timeout_seconds, 1)),
    )


def to_task(raw: RawSkillFlowTask) -> Task:
    """将解析后的真实 SkillFlow 任务转换为 Task 实例。"""
    verification = convert_tests(raw)
    files = convert_environment(raw)
    return Task(
        task_id=raw.task_id,
        category=raw.family_id,
        family_id=raw.family_id,
        description=raw.instruction,
        difficulty=raw.difficulty,
        verification=verification,
        files=files,
        metadata={
            "source": "skillflow_real",
            "binary_files": raw.binary_files,
            "source_environment_dir": raw.source_environment_dir,
            "raw_toml": raw.raw_toml,
            "environment": dict(raw.raw_toml.get("environment") or {}),
            # [Runtime Protocol Alignment Issue1/Issue3] agent 执行（CLI
            # trajectory）超时——由 harness/cli_harness_base.py 读取，
            # 优先于 configs/runtime.yaml 的仓库级 fallback 常量。
            "agent_timeout_seconds": raw.agent_timeout_seconds,
            "agent_timeout_source": raw.agent_timeout_source,
            "verifier_timeout_seconds": raw.verifier_timeout_seconds,
            "verifier_timeout_source": raw.verifier_timeout_source,
            "environment_timeout_seconds": raw.environment_timeout_seconds,
            "environment_timeout_source": raw.environment_timeout_source,
        },
    )
