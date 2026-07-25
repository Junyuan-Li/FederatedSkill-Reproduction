"""
parser.py — 解析单个真实 SkillFlow 任务目录

约定目录结构（见 downloader.py 顶部说明）：
    <task_dir>/
      task.toml          任务元信息
      instruction.md     自然语言任务说明
      environment/       输入文件（文本文件读入 files；二进制文件记录到 binary_files）
      tests/             验证脚本（*.py 拼接为 test_script；无 .py 则用 *.sh）

未下载真实数据集时，parser 对不存在的目录只会抛出清晰的 FileNotFoundError，
不会静默返回空结果——避免"骨架写完但从没验证过格式假设"的隐藏 bug。
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_KNOWN_BINARY_SUFFIXES = {
    ".xlsx", ".xls", ".docx", ".doc", ".pdf", ".pptx", ".ppt",
    ".png", ".jpg", ".jpeg", ".gif", ".zip", ".tar", ".gz", ".db", ".sqlite",
}

# [ENGINEERING] 文本内联的大小上限：真实数据集里个别 family（如
# SEC-13F-Financial-Analysis）的 environment/ 下带有 300MB+ 的 .tsv 原始数据文件，
# 若整份读入内存塞进 Task.files（最终会被 json.dump 进 benchmark/families/*.json），
# 会导致同步耗时暴涨甚至内存/IO 异常。超过此上限的文件只记录相对路径到
# binary_files（不代表它真的是二进制，只是不适合内联存储），Task.files 仍保留
# 该任务 environment 下体积较小的文本文件（如 Dockerfile/说明文件）。
_MAX_INLINE_TEXT_BYTES = 1 * 1024 * 1024  # 1MB


# [Runtime Protocol Alignment Issue3] 官方 task.toml 用嵌套 section 分别声明
# 三个独立超时（不是顶层 timeout_seconds/timeout 键——旧版本在这里找错了
# 键路径，永远匹配不到真实数据，静默 fallback 到硬编码 30，
# 详见 timeout_policy_report.md）：
#   [agent]       timeout_sec        — agent/CLI 执行整条 trajectory 的超时
#   [verifier]    timeout_sec        — 验证脚本执行超时
#   [environment] build_timeout_sec  — Docker 环境构建超时（少数任务用 timeout_sec）
# fallback 默认值取自官方仓库/抽样数据的默认值，只在该 task 的 toml 里
# 确实没有对应 section/key 时才生效（不是从别的 section 级联下来）。
_AGENT_TIMEOUT_DEFAULT = 1800.0
_VERIFIER_TIMEOUT_DEFAULT = 900.0
_ENVIRONMENT_TIMEOUT_DEFAULT = 600.0


def _section_timeout_sec(
    raw_toml: dict,
    section: str,
    keys: tuple[str, ...],
    default: float,
    *,
    task_path: Path,
) -> tuple[float, str]:
    """读取官方 section timeout；缺失时显式告警并标记默认值来源。"""
    section_dict = raw_toml.get(section)
    if isinstance(section_dict, dict):
        for key in keys:
            if key in section_dict:
                return float(section_dict[key]), f"task.toml:[{section}].{key}"
    logger.warning(
        "SkillFlow task 缺少 [%s].%s，使用显式默认值 %.1fs: %s",
        section, "/".join(keys), default, task_path,
    )
    return default, "default_with_warning"


@dataclass
class RawSkillFlowTask:
    """单个真实 SkillFlow 任务的解析中间结果（未映射到 Task 模型）。"""

    task_id: str
    family_id: str
    difficulty: int
    instruction: str
    files: dict[str, str] = field(default_factory=dict)       # 相对路径 -> 文本内容
    binary_files: list[str] = field(default_factory=list)      # 无法解码为文本的文件路径
    test_script: str = ""
    # [Runtime Protocol Alignment Issue3] 三个独立超时字段，替代此前唯一
    # 且解析错误的 timeout_seconds（见上方模块级注释）。
    agent_timeout_seconds: float = _AGENT_TIMEOUT_DEFAULT
    verifier_timeout_seconds: float = _VERIFIER_TIMEOUT_DEFAULT
    environment_timeout_seconds: float = _ENVIRONMENT_TIMEOUT_DEFAULT
    agent_timeout_source: str = "default_with_warning"
    verifier_timeout_source: str = "default_with_warning"
    environment_timeout_source: str = "default_with_warning"
    source_environment_dir: str = ""
    raw_toml: dict = field(default_factory=dict)


def _read_text_files(root: Path) -> tuple[dict[str, str], list[str]]:
    """
    递归读取 root 下的文本文件，返回 (相对路径->内容, 二进制文件相对路径列表)。

    [ENGINEERING] 判定逻辑：
    1) 扩展名在 `_KNOWN_BINARY_SUFFIXES` 里的文件直接归为二进制（跳过解码，快路径）；
    2) 体积超过 `_MAX_INLINE_TEXT_BYTES` 的文件也直接归为二进制（真实数据集里
       SEC-13F-Financial-Analysis 等 family 的 environment/ 下有 300MB+ 的
       .tsv 原始数据，整份读入内存塞进 Task.files 会导致同步挂起/内存暴涨）；
    3) 其余文件（包括真实数据集里大量存在的、**没有扩展名**的 `Dockerfile` ——
       每个真实 SkillFlow 任务的 environment/ 目录都有一份，此前用"扩展名
       白名单"判定会把它错误归为二进制，导致 Task.files 里丢失 Dockerfile
       内容）统一尝试 UTF-8 解码，解码失败才归为二进制——用真实的
       "能否解码成文本"代替不完整的扩展名白名单，对已下载的真实数据验证过：
       Dockerfile 现在能正确进入 files。
    """
    files: dict[str, str] = {}
    binary_files: list[str] = []
    if not root.is_dir():
        return files, binary_files
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        too_large = p.stat().st_size > _MAX_INLINE_TEXT_BYTES
        if p.suffix.lower() not in _KNOWN_BINARY_SUFFIXES and not too_large:
            try:
                files[rel] = p.read_text(encoding="utf-8")
                continue
            except UnicodeDecodeError:
                pass
        binary_files.append(rel)
    return files, binary_files


def _read_test_script(tests_dir: Path) -> str:
    """拼接 tests/ 目录下所有 .py 脚本；若没有 .py 则拼接 .sh。"""
    if not tests_dir.is_dir():
        return ""
    py_scripts = sorted(tests_dir.rglob("*.py"))
    scripts = py_scripts if py_scripts else sorted(tests_dir.rglob("*.sh"))
    parts = []
    for p in scripts:
        parts.append(f"# --- {p.relative_to(tests_dir)} ---\n{p.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def parse_task_dir(
    task_dir: str | Path,
    *,
    sequence_position: int | None = None,
) -> RawSkillFlowTask:
    """
    解析单个任务目录为 RawSkillFlowTask。

    task_dir 结构参见模块 docstring。family_id 取 task_dir 的父目录名，
    task_id 取 task_dir 自身目录名。
    """
    task_dir = Path(task_dir)
    if not task_dir.is_dir():
        raise FileNotFoundError(f"任务目录不存在: {task_dir}")

    toml_path = task_dir / "task.toml"
    raw_toml: dict = {}
    if toml_path.exists():
        raw_toml = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    instruction_path = task_dir / "instruction.md"
    instruction = instruction_path.read_text(encoding="utf-8") if instruction_path.exists() else ""

    files, binary_files = _read_text_files(task_dir / "environment")
    test_script = _read_test_script(task_dir / "tests")

    # 官方任务的 [metadata].difficulty 是 "hard" 等分类标签，不承担
    # family 内顺序语义。正式加载时由 loader 根据
    # ALL_TASK_DIFFICULTY_RANKING.json 显式传入 1..N；保留顶层数值字段
    # 仅用于兼容既有自建测试数据和旧缓存。
    difficulty = (
        sequence_position
        if sequence_position is not None
        else int(raw_toml.get("difficulty", 1))
    )
    # [Runtime Protocol Alignment Issue3] 从各自的嵌套 section 读取，互不
    # 级联 fallback（agent 缺失不会退到 verifier 的值，反之亦然）。
    agent_timeout_seconds, agent_timeout_source = _section_timeout_sec(
        raw_toml, "agent", ("timeout_sec",), _AGENT_TIMEOUT_DEFAULT,
        task_path=toml_path,
    )
    verifier_timeout_seconds, verifier_timeout_source = _section_timeout_sec(
        raw_toml, "verifier", ("timeout_sec",), _VERIFIER_TIMEOUT_DEFAULT,
        task_path=toml_path,
    )
    environment_timeout_seconds, environment_timeout_source = _section_timeout_sec(
        raw_toml, "environment", ("timeout_sec", "build_timeout_sec"),
        _ENVIRONMENT_TIMEOUT_DEFAULT, task_path=toml_path,
    )

    task_id = task_dir.name
    family_id = task_dir.parent.name

    logger.info(
        "解析 SkillFlow 任务: family=%s task=%s difficulty=%d files=%d binary=%d",
        family_id, task_id, difficulty, len(files), len(binary_files),
    )

    return RawSkillFlowTask(
        task_id=task_id,
        family_id=family_id,
        difficulty=difficulty,
        instruction=instruction,
        files=files,
        binary_files=binary_files,
        test_script=test_script,
        agent_timeout_seconds=agent_timeout_seconds,
        verifier_timeout_seconds=verifier_timeout_seconds,
        environment_timeout_seconds=environment_timeout_seconds,
        agent_timeout_source=agent_timeout_source,
        verifier_timeout_source=verifier_timeout_source,
        environment_timeout_source=environment_timeout_source,
        source_environment_dir=str((task_dir / "environment").resolve()),
        raw_toml=raw_toml,
    )
