"""
benchmark/families/ — SkillFlow-family 兼容的任务族命名空间

本目录下的 25 个 `*.json` 文件是自建 task family 数据（路径、内容均不变）。
本 `__init__.py` 是 Official Implementation Alignment Audit 新增的薄封装层，
让 `benchmark.families` 成为一个可直接 `import` 的 Python 包（与官方
`SkillFlow benchmark` 的 family 目录概念对齐），对外重新导出与
`benchmark.family`（已测试、完全未改动）完全相同的
`TaskFamily` / `load_family` / `load_all_families`，不重复实现加载逻辑。
"""

from __future__ import annotations

from benchmark.family import TaskFamily, load_all_families, load_family

__all__ = ["TaskFamily", "load_family", "load_all_families"]
