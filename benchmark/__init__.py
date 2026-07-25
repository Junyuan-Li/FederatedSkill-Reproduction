"""benchmark package — Benchmark 层公开接口"""

from benchmark.task import Task, VerificationSpec, TestCase
from benchmark.loader import TaskLoader
from benchmark.family import TaskFamily, load_family, load_all_families
from benchmark.sampler import TaskSampler, RandomSampler, HeterogeneousSampler, DifficultyAwareSampler
from benchmark.curriculum import FamilyCurriculumSampler, CurriculumSampler, SkillFlowFamilySampler
from benchmark.family_sampler import FamilyAwareSampler
from benchmark.dependencies import is_unlocked, eligible_tasks
from benchmark.curriculum_state import CurriculumState
from benchmark.verifier import (
    VerificationResult,
    PythonSandboxVerifier,
    FunctionTestVerifier,
    OutputMatchVerifier,
    get_verifier,
)
from benchmark.evaluator import BenchmarkEvaluator, RoundMetrics, ExperimentSummary

__all__ = [
    # task
    "Task", "VerificationSpec", "TestCase",
    # loader
    "TaskLoader",
    # family（SkillFlow 风格递增难度序列）
    "TaskFamily", "load_family", "load_all_families",
    # sampler
    "TaskSampler", "RandomSampler", "HeterogeneousSampler", "DifficultyAwareSampler",
    "FamilyCurriculumSampler", "CurriculumSampler", "SkillFlowFamilySampler", "FamilyAwareSampler",
    # dependencies / curriculum_state（Official Implementation Alignment Audit 新增：
    # 采样策略与依赖判定/状态管理解耦）
    "is_unlocked", "eligible_tasks", "CurriculumState",
    # verifier
    "VerificationResult", "PythonSandboxVerifier", "FunctionTestVerifier",
    "OutputMatchVerifier", "get_verifier",
    # evaluator
    "BenchmarkEvaluator", "RoundMetrics", "ExperimentSummary",
]
