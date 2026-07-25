"""
library.py — 客户端本地技能库管理器（SkillLibrary）

[OFFICIAL] SKILL.md 目录 schema（name/description frontmatter + scripts/references/assets 子目录）

对应论文中的 L_i^t（每轮开始时读取，每轮结束时由 MergedPatch Δ_i^t 更新）。

目录结构约定（源自论文 Figure 3 及 paper_logs 中的实际技能文件）：
    <library_root>/
        <skill-name>/
            SKILL.md          ← 必需，YAML 前置元数据 + Markdown 工作流
            scripts/          ← 可选，辅助脚本
            references/       ← 可选，参考文档
            assets/           ← 可选，配置模板

与原版（SkillFlow 内置的 skill_dir 读写逻辑）的区别：
  - 原版：散落在 runner.py / patcher_bridge.py / merge.py 的多处文件操作
  - 本版：统一封装为 SkillLibrary 类，对外暴露 snapshot() / apply_patch() / rollback()
  - 新增：rollback() 可恢复到任意历史快照，适合实验复现
  - 新增：validate() 返回结构问题列表，而非静默忽略
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from core.constants import ALLOWED_SKILL_SUBDIRS, SKILL_FILENAME
from core.datatypes import (
    LibraryDigest,
    LibraryFileEntry,
    LibrarySnapshot,
    MergedPatch,
    WorkerPatch,
)
from core.exceptions import LibraryError, LibraryValidationError

if TYPE_CHECKING:
    pass


class SkillLibrary:
    """
    客户端本地技能库 L_i^t 的文件系统管理器。

    所有文件操作均通过此类进行，外部不直接读写技能目录，
    保证路径安全检查在统一位置执行。

    Args:
        root:      技能库根目录（不存在时自动创建）
        worker_id: 所属 worker 的 client_id（用于快照元数据）
    """

    def __init__(self, root: Path, worker_id: str) -> None:
        self._root = Path(root)
        self._worker_id = worker_id
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # ------------------------------------------------------------------
    # 读取操作
    # ------------------------------------------------------------------

    def snapshot(self, round_idx: int = 0) -> LibrarySnapshot:
        """
        读取当前技能库的完整快照（所有文件内容）。

        对应论文 Section 4.1.2 ❷ Library Snapshot：
            '一个 JSON 快照，包含 L_i^t 中所有文件路径及其对应内容'

        跳过二进制文件（UnicodeDecodeError）。
        路径规范化为 POSIX 正斜杠。
        """
        files: list[LibraryFileEntry] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self._root).as_posix()
            try:
                content = path.read_text(encoding="utf-8")
                files.append(LibraryFileEntry(path=rel, content=content))
            except (UnicodeDecodeError, OSError):
                # 跳过二进制或无法读取的文件
                pass
        return LibrarySnapshot(
            worker_id=self._worker_id,
            round_idx=round_idx,
            files=files,
        )

    def digest(self) -> list[LibraryDigest]:
        """
        读取每个技能的描述级摘要（名称 + 描述 + 标签）。

        对应论文 Section 4.2.1：
            '仅包含技能名称和描述的描述级摘要'，供 Stage 1 规划使用。
        不返回完整文件内容（保持信息最小化原则）。
        """
        digests: list[LibraryDigest] = []
        for skill_dir in sorted(self._root.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md_path = skill_dir / SKILL_FILENAME
            if not skill_md_path.exists():
                continue
            try:
                text = skill_md_path.read_text(encoding="utf-8")
                fm = _parse_frontmatter(text)
                if fm and fm.get("name"):
                    digests.append(LibraryDigest(
                        skill_name=str(fm["name"]),
                        description=str(fm.get("description", "")),
                        directory=skill_dir.name,
                        tags=list(fm.get("tags", [])) if isinstance(fm.get("tags"), list) else [],
                        trigger=str(fm.get("trigger", "")),
                    ))
            except Exception:
                # 前置元数据解析失败时静默跳过（validate() 会报告具体问题）
                pass
        return digests

    def skill_count(self) -> int:
        """返回技能数量（有 SKILL.md 的目录数）。"""
        return sum(
            1 for d in self._root.iterdir()
            if d.is_dir() and (d / SKILL_FILENAME).exists()
        )

    # ------------------------------------------------------------------
    # 写入操作
    # ------------------------------------------------------------------

    def apply_patch(self, patch: WorkerPatch | MergedPatch) -> None:
        """
        将 patch 应用到技能库。
        对应论文 Section 4.2.2：L_i^{t+1} = Apply(L_i^t, Δ_i^t)

        执行顺序：先删除，再写入。
        所有路径均已在 WorkerPatch / MergedPatch 的 field_validator 中验证安全。
        """
        # Step 1: 删除
        for rel_path in patch.deletions:
            target = self._root / rel_path
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()

        # Step 2: 写入 upserts
        for rel_path, content in patch.upserts.items():
            target = self._root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def rollback(self, snapshot: LibrarySnapshot) -> None:
        """
        将技能库完全恢复到给定快照的状态。

        用途：实验复现时将库回滚到某 round 开始前的状态。
        实现：清空整个根目录，然后重写所有快照文件。
        """
        if snapshot.worker_id != self._worker_id:
            raise LibraryError(
                f"快照 worker_id={snapshot.worker_id!r} 与 "
                f"当前库 worker_id={self._worker_id!r} 不匹配"
            )
        # 清空根目录（保留目录本身）
        for item in self._root.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        # 重写快照文件
        for entry in snapshot.files:
            target = self._root / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry.content, encoding="utf-8")

    # ------------------------------------------------------------------
    # 验证
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """
        验证技能库结构，返回所有发现的问题列表（空列表表示健康）。

        检查项：
          1. 每个技能目录必须有 SKILL.md
          2. SKILL.md 必须有合法的 YAML 前置元数据
          3. 前置元数据必须含 name 和 description
          4. 子目录只允许 scripts/ references/ assets/
        """
        issues: list[str] = []
        for skill_dir in sorted(self._root.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md_path = skill_dir / SKILL_FILENAME
            if not skill_md_path.exists():
                issues.append(f"{skill_dir.name}/: 缺少 SKILL.md")
                continue

            try:
                text = skill_md_path.read_text(encoding="utf-8")
            except Exception as e:
                issues.append(f"{skill_dir.name}/SKILL.md: 无法读取 — {e}")
                continue

            fm = _parse_frontmatter(text)
            if fm is None:
                issues.append(f"{skill_dir.name}/SKILL.md: 缺少 YAML 前置元数据")
            else:
                if not fm.get("name"):
                    issues.append(f"{skill_dir.name}/SKILL.md: 前置元数据缺少 'name'")
                if not fm.get("description"):
                    issues.append(f"{skill_dir.name}/SKILL.md: 前置元数据缺少 'description'")

            # 检查子目录
            for sub in skill_dir.iterdir():
                if sub.is_dir() and sub.name not in ALLOWED_SKILL_SUBDIRS:
                    issues.append(
                        f"{skill_dir.name}/{sub.name}/: "
                        f"不允许的子目录（允许: {sorted(ALLOWED_SKILL_SUBDIRS)}）"
                    )
        return issues

    def assert_valid(self) -> None:
        """验证库结构，若有问题则抛出 LibraryValidationError。"""
        issues = self.validate()
        if issues:
            msg = "\n  ".join(issues)
            raise LibraryValidationError(
                f"技能库 {self._root} 存在 {len(issues)} 个问题：\n  {msg}"
            )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict | None:
    """
    从 SKILL.md 文本中提取 YAML 前置元数据。

    YAML 前置元数据格式：
        ---
        name: ...
        description: ...
        ---
    """
    import re
    import yaml

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None
    try:
        result = yaml.safe_load(m.group(1))
        return result if isinstance(result, dict) else {}
    except yaml.YAMLError:
        return None
