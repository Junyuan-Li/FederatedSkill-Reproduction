"""
downloader.py — 真实 SkillFlow 数据集下载器（HuggingFace: zhang-ziao/SkillFlow-Task）

⚠ Sprint 1 明确约定：本文件里的函数**不会被任何 import 或测试自动调用**，
    必须由使用者显式执行 `download_skillflow_dataset(...)` 才会触发网络请求
    和 ~1.6GB 磁盘写入。这是应用户要求的"先写骨架，不下载，等确认再下载"。

真实数据集结构（约定，来自官方 FederatedSkill 仓库 README）：
    SkillFlow-Task/
      <family_id>/                 20 个 family
        <task_id>/                 每个 family 8~9 个递增难度子任务
          task.toml                 任务元信息（timeout / difficulty 等）
          instruction.md            自然语言任务说明（对应 Task.description）
          environment/              任务输入文件（Dockerfile + 数据文件）
          solution/                 参考解（可选，用于人工核对，不传给 agent）
          tests/                    验证脚本（在容器里跑；本复现用 subprocess 代替）
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_REPO_ID = "zhang-ziao/SkillFlow-Task"


def is_dataset_present(dest_dir: str | Path) -> bool:
    """检查本地是否已存在数据集（粗略判断：目录存在且非空）。"""
    dest_dir = Path(dest_dir)
    return dest_dir.is_dir() and any(dest_dir.iterdir())


def download_skillflow_dataset(
    dest_dir: str | Path,
    repo_id: str = DEFAULT_REPO_ID,
    token: str | None = None,
    force: bool = False,
) -> Path:
    """
    从 HuggingFace 下载真实 SkillFlow-Task 数据集到 dest_dir。

    ⚠ 调用本函数会产生真实网络请求（~1.6GB），仅在用户显式确认后调用。
    需要额外依赖 `huggingface_hub`（未在 requirements.txt 中默认安装，
    因为 Sprint 1 阶段不下载真实数据）。

    Args:
        dest_dir: 本地目标目录
        repo_id:  HuggingFace dataset repo id
        token:    HuggingFace access token（私有/限流数据集需要）
        force:    True 时即使 dest_dir 已有内容也重新下载

    Returns:
        本地数据集根目录路径

    Raises:
        ImportError: 未安装 huggingface_hub
        RuntimeError: dest_dir 已存在数据且 force=False
    """
    dest_dir = Path(dest_dir)
    if is_dataset_present(dest_dir) and not force:
        raise RuntimeError(
            f"{dest_dir} 已存在数据，若确认要重新下载请传 force=True"
        )

    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "下载真实 SkillFlow 数据集需要 huggingface_hub："
            "pip install huggingface_hub"
        ) from exc

    logger.warning(
        "即将从 HuggingFace 下载真实数据集 repo_id=%s -> %s（约 1.6GB，需要网络）",
        repo_id, dest_dir,
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    local_path = snapshot_download(
        repo_id=repo_id,
        repo_type="dataset",
        local_dir=str(dest_dir),
        token=token,
    )
    logger.info("数据集下载完成: %s", local_path)
    return Path(local_path)
