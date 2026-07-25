"""
download.py — download_skillflow_dataset() 的文件名别名模块

Phase13 任务1 要求文件名为 `download.py`；本仓库已测试的实现在
`downloader.py`（`load_skillflow_benchmark()` 等下游代码依赖它，
且 `tests/test_skillflow_adapter.py` 已覆盖其行为）。

为满足命名要求同时不重构/不重复实现已测试逻辑，这里只做纯转发别名，
不新增任何行为。真正的实现、文档、下载安全说明均在 downloader.py。
"""

from __future__ import annotations

from benchmark.skillflow_adapter.downloader import (
    DEFAULT_REPO_ID,
    download_skillflow_dataset,
    is_dataset_present,
)

__all__ = ["DEFAULT_REPO_ID", "download_skillflow_dataset", "is_dataset_present"]
