"""
family.py — Task Family（技能族 / workflow family）

[OFFICIAL] benchmark/families/*.json 里 20 个真实 SkillFlow family 的
任务内容标签：来自官方 SkillFlow-Task 数据集下载/转换，非本项目发明；
另有 5 个手写 family 属于 [ENGINEERING]（本项目自建，用于
无网络环境下的本地回归测试）。`TaskFamily` 类本身的数据结构/加载逻辑属于
[ENGINEERING]（独立实现，对应论文 Section 5.1 描述的抽象结构）。

对应论文 Section 5.1 的 SkillFlow benchmark 结构：
    "20 diverse task families ... each family contains a sequence of tasks
     of increasing difficulty that all require the SAME underlying skill
     to be progressively evolved."

与旧版 benchmark/tasks/default_tasks.json（20 个互相独立的任务，随机采样）不同，
TaskFamily 把任务组织成"同一技能的递增难度序列"，让 Capability Matrix 的
covered/absorbing/broken/gap 状态转移和跨轮技能演化真正有意义。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from benchmark.task import Task

logger = logging.getLogger(__name__)

# 内置 family 目录（相对本模块）
_DEFAULT_FAMILIES_DIR = Path(__file__).parent / "families"


class TaskFamily:
    """
    一个 task family：同一技能的递增难度任务序列。

    Attributes:
        family_id:   family 唯一标识（对应论文 workflow family）
        description: family 描述（该技能整体在做什么）
        skill_name:  该 family 演化的目标技能名（信息性，不强制校验）
        tasks:       按 difficulty 升序排列的 Task 列表
    """

    def __init__(
        self,
        family_id: str,
        description: str,
        tasks: list[Task],
        skill_name: str = "",
    ) -> None:
        if not tasks:
            raise ValueError(f"family '{family_id}' 不能没有任务")
        self.family_id = family_id
        self.description = description
        self.skill_name = skill_name or family_id
        # 按难度升序排列，保证 get_sequence() / get_task_by_difficulty() 语义正确
        self.tasks: list[Task] = sorted(tasks, key=lambda t: t.difficulty)

    def get_sequence(self) -> list[Task]:
        """返回按难度升序排列的完整任务序列。"""
        return list(self.tasks)

    def get_task_by_difficulty(self, level: int) -> Task:
        """
        返回难度 == level 的任务；若不存在恰好匹配的难度，
        回退到序列中难度最接近且不超过 level 的最后一个任务
        （level 超出上限时钳制到最高难度任务，避免越界异常）。
        """
        for task in self.tasks:
            if task.difficulty == level:
                return task
        # 未找到精确匹配：钳制到区间内最接近的任务
        eligible = [t for t in self.tasks if t.difficulty <= level]
        if eligible:
            return eligible[-1]
        return self.tasks[0]

    def __len__(self) -> int:
        return len(self.tasks)

    def __repr__(self) -> str:
        return f"TaskFamily(family_id={self.family_id!r}, tasks={len(self.tasks)})"


def _wire_real_verification(item: dict) -> dict:
    """
    [ENGINEERING] 从原始 family JSON 的单个 task 字典中，把真实 SkillFlow
    的 `tests`/`environment` 字段正确映射成 VerificationSpec/Task.files
    所需的字段（`verification.test_script` / `files`），返回可以直接
    `dict.update()` 进 task 字段的增量字典。

    背景 bug：旧版 load_family() 把整份 JSON task 字典原样传给
    `Task(**item)`。但 `Task`/`VerificationSpec` 模型里根本没有名为
    "tests"/"environment"/"name"/顶层"timeout_seconds" 的字段——Pydantic v2
    对未知字段的默认行为是 `extra="ignore"`（静默丢弃，不报错），所以哪怕
    某个 family 的 JSON 里真的写了验证脚本，也永远进不了
    `VerificationSpec`，`verification.type` 只会停留在默认值 "none"。

    修复后的行为：
      - `tests` 非空时：拼接为 `test_script`，构造
        `verification={"type": "skillflow_script", "test_script": ...}`。
        对应官方 SkillFlow 目录结构里的 tests/ 目录（见
        benchmark/skillflow_adapter/parser.py 的 docstring），但这里处理的
        是本仓库实际使用的扁平 JSON 序列化格式，不是官方目录树格式——
        两者是同一套协议的不同表示，不是另造的验证机制。
      - `environment`（dict[路径 -> 文本内容]）非空时：映射进
        `Task.files`（executor 会把它们写入任务工作区，对应官方 environment/
        目录下的输入文件）。
      - `tests`/`environment` 都为空（当前 benchmark/families/ 目录下全部
        20 个真实 family 的现状——已通过 grep 确认）时：返回空字典，
        行为与修复前完全一致（`verification.type` 仍是默认值 "none"），
        不会伪造出不存在的验证数据。
      - 若 task 字典里已经直接提供了合法的 `"verification"` 字段（本仓库
        5 个手写 family 的写法），本函数不会覆盖它——只在检测到 `tests`/
        `environment` 时才产出 `verification`/`files` 键。

    Returns:
        {} 或 {"verification": {...}} 和/或 {"files": {...}}
    """
    tests = item.get("tests") or []
    environment = item.get("environment") or {}

    result: dict = {}

    if isinstance(environment, dict) and environment:
        files = {
            rel_path: content
            for rel_path, content in environment.items()
            if isinstance(rel_path, str) and isinstance(content, str)
        }
        if files:
            result["files"] = files

    if tests:
        # tests 里每一项可以是纯字符串（已拼接好的脚本），
        # 也可以是 {"path"/"filename": ..., "content": ...} 字典
        # （对应官方 tests/ 目录下的多个验证脚本文件，拼接执行）。
        script_parts: list[str] = []
        for entry in tests:
            if isinstance(entry, str):
                script_parts.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("content"), str):
                name = entry.get("path") or entry.get("filename") or "test.py"
                script_parts.append(f"# --- {name} ---\n{entry['content']}")
        if script_parts:
            verification: dict = {
                "type": "skillflow_script",
                "test_script": "\n\n".join(script_parts),
            }
            timeout_seconds = item.get("timeout_seconds")
            if isinstance(timeout_seconds, int) and timeout_seconds > 0:
                verification["timeout_seconds"] = min(timeout_seconds, 300)
            result["verification"] = verification

    return result


def load_family(path: str | Path) -> TaskFamily:
    """
    从单个 family JSON 文件加载 TaskFamily。

    文件格式::

        {
          "family_id": "data_cleaning",
          "description": "...",
          "skill_name": "csv_row_cleaning",
          "tasks": [ {...Task 字段...}, ... ]
        }

    [ENGINEERING] 每个 task 字典在传入 Task(**...) 之前，会先经过
    _wire_real_verification() 做一次真实 `tests`/`environment` 字段的映射
    （见该函数 docstring），再剔除 Task 模型里不存在的原始字段名
    （"tests"/"environment"/"name"/"timeout_seconds"），避免 Pydantic
    的静默 extra-field-drop 行为把真实验证数据丢在半路。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"family 文件不存在: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    family_id = data.get("family_id") or path.stem

    tasks = []
    for item in data.get("tasks", []):
        wired = _wire_real_verification(item)
        task_fields = {
            k: v for k, v in item.items()
            if k not in {"tests", "environment", "name", "timeout_seconds"}
        }
        task_fields.update(wired)
        tasks.append(Task(**task_fields))

    family = TaskFamily(
        family_id=family_id,
        description=data.get("description", ""),
        tasks=tasks,
        skill_name=data.get("skill_name", ""),
    )
    logger.info("已加载 family=%s，%d 个任务", family_id, len(family))
    return family


def load_all_families(directory: str | Path = _DEFAULT_FAMILIES_DIR) -> dict[str, TaskFamily]:
    """
    加载目录下所有 *.json family 文件，返回 {family_id: TaskFamily}。

    Args:
        directory: family 文件所在目录；默认 benchmark/families/
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"不是目录: {directory}")

    families: dict[str, TaskFamily] = {}
    for f in sorted(directory.glob("*.json")):
        try:
            family = load_family(f)
            families[family.family_id] = family
        except Exception as exc:
            logger.warning("加载 family 文件失败 %s: %s", f, exc)

    logger.info("目录 %s 共加载 %d 个 family", directory, len(families))
    return families
