"""
loader.py — Benchmark 任务加载器

支持从 JSON / YAML 文件或目录批量加载 Task 对象。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from benchmark.task import Task

logger = logging.getLogger(__name__)

# 内置默认任务文件路径（相对本模块）
_DEFAULT_TASKS_FILE = Path(__file__).parent / "tasks" / "default_tasks.json"


class TaskLoader:
    """
    从 JSON / YAML 文件加载任务列表。

    文件格式：
      - 顶层为 list，每个元素是 Task 的 dict 表示；
      - 或顶层为 dict，包含 "tasks" 键。

    示例::

        loader = TaskLoader("benchmark/tasks/default_tasks.json")
        tasks = loader.load()
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[Task]:
        """加载并返回 Task 列表。"""
        if not self.path.exists():
            raise FileNotFoundError(f"任务文件不存在: {self.path}")

        raw = self.path.read_text(encoding="utf-8")

        suffix = self.path.suffix.lower()
        if suffix == ".json":
            data = json.loads(raw)
        elif suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:
                raise ImportError("需要安装 pyyaml: pip install pyyaml") from exc
            data = yaml.safe_load(raw)
        else:
            raise ValueError(f"不支持的任务文件格式: {self.path.suffix}（仅支持 .json / .yaml）")

        # 兼容 {"tasks": [...]} 包装格式
        if isinstance(data, dict) and "tasks" in data:
            data = data["tasks"]

        if not isinstance(data, list):
            raise ValueError(f"任务文件顶层应为 list，实际为 {type(data).__name__}")

        tasks = [Task(**item) for item in data]
        logger.info("已加载 %d 个任务，来源: %s", len(tasks), self.path)
        return tasks

    # ------------------------------------------------------------------
    # 类方法便捷接口
    # ------------------------------------------------------------------

    @classmethod
    def load_default(cls) -> list[Task]:
        """加载内置默认任务集（benchmark/tasks/default_tasks.json）。"""
        return cls(_DEFAULT_TASKS_FILE).load()

    @classmethod
    def load_directory(cls, directory: str | Path) -> list[Task]:
        """
        递归扫描目录下所有 .json / .yaml 文件并合并加载。

        Args:
            directory: 目录路径

        Returns:
            所有文件中任务的合并列表（去重逻辑：重复 task_id 保留最后一条）
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise NotADirectoryError(f"不是目录: {directory}")

        tasks_by_id: dict[str, Task] = {}
        for suffix in ("*.json", "*.yaml", "*.yml"):
            for f in sorted(directory.rglob(suffix)):
                try:
                    for task in cls(f).load():
                        tasks_by_id[task.task_id] = task
                except Exception as exc:
                    logger.warning("加载任务文件失败 %s: %s", f, exc)

        logger.info("目录 %s 共加载 %d 个去重任务", directory, len(tasks_by_id))
        return list(tasks_by_id.values())
