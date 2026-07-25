"""
skillflow_adapter — 真实 SkillFlow 数据集（HuggingFace: zhang-ziao/SkillFlow-Task）适配层

Sprint 1 交付：downloader / parser / converter / loader 代码骨架。

⚠ 重要：本 Sprint 只搭骨架，**不会自动下载**真实 ~1.6GB 数据集。
    downloader.download_skillflow_dataset() 需要用户显式调用才会触发网络请求，
    模块 import 阶段绝不会自动触发下载。

数据流向：
    downloader.py  下载 HuggingFace 数据集到本地目录（骨架，未调用）
        -> parser.py    解析单个真实任务目录（task.toml / instruction.md /
                         environment/ / tests/）为 RawSkillFlowTask
        -> converter.py 将 RawSkillFlowTask 映射为 benchmark.task.Task
                         （复用现有 Task/VerificationSpec 模型，不新建并行 schema）
        -> loader.py    批量加载整个 family 目录 / 整个数据集根目录，
                         产出 benchmark.family.TaskFamily（复用现有类，
                         与自建 family benchmark 走同一套下游流程）

不修改 core/ server/ client/（Phase 1 约束）。
"""

from benchmark.skillflow_adapter.parser import RawSkillFlowTask, parse_task_dir
from benchmark.skillflow_adapter.converter import to_task
from benchmark.skillflow_adapter.loader import load_skillflow_family, load_skillflow_benchmark
from benchmark.skillflow_adapter.downloader import download_skillflow_dataset, is_dataset_present

__all__ = [
    "RawSkillFlowTask", "parse_task_dir",
    "to_task",
    "load_skillflow_family", "load_skillflow_benchmark",
    "download_skillflow_dataset", "is_dataset_present",
]
