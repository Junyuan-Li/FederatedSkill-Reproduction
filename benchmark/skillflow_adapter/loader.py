"""
loader.py — 批量加载真实 SkillFlow 数据集，产出 TaskFamily（复用 benchmark.family）

用法（数据集下载完成后）：
    families = load_skillflow_benchmark(Path("path/to/SkillFlow-Task"))
    # families: {family_id: TaskFamily}，与 benchmark.family.load_all_families()
    # 返回类型一致，可直接喂给 FamilyCurriculumSampler。

缓存布局（可选，cache_dir 非 None 时写入）：
    <cache_dir>/<family_id>/task1.json, task2.json, ...
    每个 json 是 Task.model_dump_json() 的内容，供离线复用，
    避免每次实验重新解析真实数据集目录。

⚠ 本文件不触发任何下载；root 目录必须已经存在（由用户显式调用
    downloader.download_skillflow_dataset() 或手动放置数据后才可用）。

[ENGINEERING] sync_families_to_benchmark()：完整闭环
    raw SkillFlow dataset -> parser -> converter -> benchmark/families/*.json
    把真实转换出的 TaskFamily 落盘为 benchmark/family.py::load_family() 能
    直接读取的 JSON 格式（覆盖同名的、此前只含 instruction 的占位文件）。
    只写入真实解析出的数据，任何家庭/任务解析失败都会原样抛出，
    不会用空 tests/environment 伪造出"看起来完整"的 JSON。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from benchmark.family import TaskFamily
from benchmark.skillflow_adapter.converter import to_task
from benchmark.skillflow_adapter.parser import parse_task_dir
from benchmark.task import Task

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(__file__).parent.parent / "cache"
DEFAULT_FAMILIES_DIR = Path(__file__).parent.parent / "families"


def resolve_family_task_dirs(family_dir: str | Path) -> list[Path]:
    """按官方 ranking 文件顺序返回任务目录，未排名任务按名称追加。"""
    family_dir = Path(family_dir)
    present = sorted(
        path
        for path in family_dir.iterdir()
        if path.is_dir() and (path / "task.toml").is_file()
    )
    ranking_path = family_dir / "ALL_TASK_DIFFICULTY_RANKING.json"
    if not ranking_path.is_file():
        return present

    ranking = json.loads(ranking_path.read_text(encoding="utf-8"))
    if not isinstance(ranking, list) or not all(isinstance(name, str) for name in ranking):
        raise ValueError(f"无效的任务排名文件（应为字符串数组）: {ranking_path}")

    by_name = {path.name: path for path in present}
    ordered: list[Path] = []
    seen: set[str] = set()
    for name in ranking:
        if name in by_name and name not in seen:
            ordered.append(by_name[name])
            seen.add(name)
    ordered.extend(path for path in present if path.name not in seen)
    return ordered


def load_skillflow_family(
    family_dir: str | Path,
    cache_dir: str | Path | None = None,
) -> TaskFamily:
    """
    加载单个真实 SkillFlow family 目录（其下每个子目录是一个任务）。

    Args:
        family_dir: <root>/<family_id>/ 目录
        cache_dir:  若提供，把转换后的 Task 写入 <cache_dir>/<family_id>/task*.json
    """
    family_dir = Path(family_dir)
    if not family_dir.is_dir():
        raise FileNotFoundError(f"family 目录不存在: {family_dir}")

    family_id = family_dir.name
    # [ENGINEERING] 官方真实数据集里，除了任务子目录，还会混入非任务目录
    # （已在真实下载数据中确认存在：Compensation-Scenario-Modeling/jobs/、
    # Supply-Chain-Replenishment/jobs/ ——官方 Harbor 测试运行留下的时间戳
    # 日志目录，不含 task.toml）。按"是否含 task.toml"过滤，跳过并 warning，
    # 不当成任务解析失败而中断整个 family（否则一个非任务目录会导致
    # 全部真实任务因为 load_skillflow_benchmark() 的 family 级 try/except
    # 被静默丢弃，无法反映"数据缺口已解决"的真实情况）。
    task_dirs = resolve_family_task_dirs(family_dir)
    for p in sorted(path for path in family_dir.iterdir() if path.is_dir()):
        if not (p / "task.toml").is_file():
            logger.warning(
                "跳过 family '%s' 下的非任务子目录（缺少 task.toml）: %s", family_id, p,
            )
    if not task_dirs:
        raise ValueError(f"family '{family_id}' 下没有任何任务子目录: {family_dir}")

    tasks: list[Task] = []
    for sequence_position, task_dir in enumerate(task_dirs, start=1):
        raw = parse_task_dir(task_dir, sequence_position=sequence_position)
        tasks.append(to_task(raw))

    if cache_dir is not None:
        _write_cache(family_id, tasks, Path(cache_dir))

    family = TaskFamily(
        family_id=family_id,
        description=f"真实 SkillFlow family: {family_id}（{len(tasks)} 个递增难度任务）",
        tasks=tasks,
    )
    logger.info("已加载真实 SkillFlow family=%s，%d 个任务", family_id, len(family))
    return family


def load_skillflow_benchmark(
    root: str | Path,
    cache_dir: str | Path | None = DEFAULT_CACHE_DIR,
) -> dict[str, TaskFamily]:
    """
    加载整个真实 SkillFlow 数据集根目录下的所有 family。

    Args:
        root:      数据集根目录（<root>/<family_id>/<task_id>/...）
        cache_dir: 缓存输出目录，默认 benchmark/cache/；传 None 关闭缓存写入
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(
            f"数据集根目录不存在: {root}。"
            "真实数据集需先调用 downloader.download_skillflow_dataset() 下载。"
        )

    families: dict[str, TaskFamily] = {}
    for family_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            families[family_dir.name] = load_skillflow_family(family_dir, cache_dir)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("跳过 family 目录 %s: %s", family_dir, exc)

    logger.info("真实 SkillFlow 数据集加载完成，共 %d 个 family", len(families))
    return families


def _write_cache(family_id: str, tasks: list[Task], cache_dir: Path) -> None:
    out_dir = cache_dir / family_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for idx, task in enumerate(tasks, start=1):
        out_path = out_dir / f"task{idx}.json"
        out_path.write_text(
            json.dumps(task.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def family_to_json_dict(family: TaskFamily) -> dict:
    """
    [ENGINEERING] 把一个 TaskFamily（真实转换结果）序列化成
    `benchmark/family.py::load_family()` 能直接读取的 JSON 结构：
        {"family_id", "description", "skill_name", "tasks": [Task.model_dump(), ...]}

    每个 task 用 `Task.model_dump()` 完整导出（含已经填好的
    `verification`（type="skillflow_script" + 真实 test_script）和
    `files`（真实 environment/ 文本内容）），不使用旧版扁平 JSON 里
    "tests"/"environment" 的原始别名格式——`family.py::_wire_real_verification()`
    只在遇到那两个别名字段时才做转换，遇到已经是 `verification`/`files`
    的标准字段会原样保留，两种写法对 load_family() 结果等价。
    """
    return {
        "family_id": family.family_id,
        "description": family.description,
        "skill_name": family.skill_name,
        "tasks": [task.model_dump(mode="json") for task in family.get_sequence()],
    }


def sync_families_to_benchmark(
    root: str | Path,
    families_dir: str | Path = DEFAULT_FAMILIES_DIR,
    only_family_ids: list[str] | None = None,
) -> dict[str, int]:
    """
    [ENGINEERING] 完整闭环：raw SkillFlow dataset -> parser -> converter
    -> benchmark/families/*.json。

    对 root 下每个真实 family 目录调用 `load_skillflow_family()`
    （parser.parse_task_dir + converter.to_task，无任何伪造字段），
    再把结果序列化写入 `families_dir/<family_id>.json`，覆盖同名的、
    此前只含 instruction/task metadata 的占位文件。

    Args:
        root:             真实数据集根目录（<root>/<family_id>/<task_id>/...）。
                          注意：HuggingFace 仓库 zhang-ziao/SkillFlow-Task 的
                          实际文件树是 <repo_root>/test_tasks/<family_id>/...，
                          即本参数通常应传 "<下载目录>/test_tasks"，而不是
                          下载目录本身（downloader.py 的目录结构说明未提及
                          这一层前缀，属于既有文档小误差，不在本次任务修改
                          范围内，这里用参数说明澄清）。
        families_dir:     写入目标目录，默认 benchmark/families/
        only_family_ids:  可选，只同步指定的 family_id 子集（用于增量/测试，
                          默认 None 表示同步 root 下发现的全部 family）

    Returns:
        {family_id: task_count}，只包含成功解析并写入的 family；
        单个 family 解析失败会记录 warning 并跳过（不写入该 family），
        不会用空数据覆盖已有的、之前能正常工作的占位 JSON。

    不生成任何 fake tests/环境数据——所有内容均来自 root 下真实存在的
    task.toml / instruction.md / environment/ / tests/ 文件。
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(
            f"数据集根目录不存在: {root}。"
            "真实数据集需先调用 downloader.download_skillflow_dataset() 下载。"
        )
    families_dir = Path(families_dir)
    families_dir.mkdir(parents=True, exist_ok=True)

    family_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if only_family_ids is not None:
        wanted = set(only_family_ids)
        family_dirs = [p for p in family_dirs if p.name in wanted]

    result: dict[str, int] = {}
    for family_dir in family_dirs:
        try:
            family = load_skillflow_family(family_dir, cache_dir=None)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("跳过 family 目录 %s（解析失败，未写入）: %s", family_dir, exc)
            continue

        out_path = families_dir / f"{family.family_id}.json"
        out_path.write_text(
            json.dumps(family_to_json_dict(family), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result[family.family_id] = len(family)
        logger.info(
            "已写入真实 family JSON: %s (%d 个任务) -> %s",
            family.family_id, len(family), out_path,
        )

    logger.info("sync_families_to_benchmark 完成: %d 个 family 写入 %s", len(result), families_dir)
    return result
