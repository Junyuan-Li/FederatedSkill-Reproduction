"""
fetch_skillflow_instructions.py — 只下载 SkillFlow 的 instruction.md + task.toml
                                   生成 benchmark/families/*.json

用法：
    python scripts/fetch_skillflow_instructions.py
    python scripts/fetch_skillflow_instructions.py --hf_endpoint https://hf-mirror.com

相比下载完整数据集（~1.6GB），本脚本只拉取 instruction 和元数据文件（<10MB），
足够我们的复现实验（不需要 Docker 环境和真实 test 脚本）。

输出：benchmark/families/<family_id>.json  ×20（覆盖已有的 placeholder）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

REPO_ID   = "zhang-ziao/SkillFlow-Task"
REPO_TYPE = "dataset"

FAMILIES_OUT = ROOT / "benchmark" / "families"


def _parse_toml_simple(text: str) -> dict:
    """极简 TOML 解析（只读 key = value / key = 数字 行）。"""
    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#") and not line.startswith("["):
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            try:
                result[k] = int(v)
            except ValueError:
                try:
                    result[k] = float(v)
                except ValueError:
                    result[k] = v
    return result


def build_families(hf_endpoint: str | None = None) -> None:
    # 不设置 HF_ENDPOINT — 直接用 requests 下载，避免镜像重定向问题
    try:
        from huggingface_hub import list_repo_files
        import requests
    except ImportError:
        logger.error("缺少依赖: pip install huggingface-hub requests")
        sys.exit(1)

    # 列表 API 可以走镜像（只需要 GET JSON，不下载文件）
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint
        logger.info("列表 API 使用镜像: %s", hf_endpoint)

    logger.info("列出仓库文件...")
    all_files = list(list_repo_files(REPO_ID, repo_type=REPO_TYPE))
    logger.info("仓库共 %d 个文件", len(all_files))

    # 恢复：下载文件直接走 huggingface.co（requests 已证明可达）
    os.environ.pop("HF_ENDPOINT", None)

    # 只下载 instruction.md 和 task.toml
    target_names = {"instruction.md", "task.toml"}
    target_files = [f for f in all_files if Path(f).name in target_names]
    logger.info("目标文件数: %d", len(target_files))

    HF_BASE = "https://huggingface.co"

    def _download_text(filepath: str) -> str:
        url = f"{HF_BASE}/datasets/{REPO_ID}/resolve/main/{filepath}"
        try:
            resp = requests.get(url, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                return resp.text
            logger.warning("HTTP %d: %s", resp.status_code, filepath)
        except Exception as e:
            logger.warning("下载异常 %s: %s", filepath, e)
        return ""

    # 按 family / task 分组
    from collections import defaultdict
    families: dict[str, dict[str, dict]] = defaultdict(dict)

    for i, filepath in enumerate(target_files):
        parts = filepath.split("/")
        if len(parts) < 4 or parts[0] != "test_tasks":
            continue
        _, family_id, task_id, filename = parts[0], parts[1], parts[2], parts[3]

        content = _download_text(filepath)
        if task_id not in families[family_id]:
            families[family_id][task_id] = {}
        families[family_id][task_id][filename] = content

        if (i + 1) % 20 == 0:
            logger.info("进度: %d/%d", i + 1, len(target_files))

    # 生成 TaskFamily JSON
    FAMILIES_OUT.mkdir(parents=True, exist_ok=True)
    logger.info("写入 benchmark/families/ ...")

    for family_id, tasks in sorted(families.items()):
        task_list = []
        for idx, (task_id, files) in enumerate(sorted(tasks.items()), 1):
            toml_data = _parse_toml_simple(files.get("task.toml", ""))
            difficulty = toml_data.get("difficulty", idx)
            instr = files.get("instruction.md") or f"Task: {task_id}"
            name = toml_data.get("name", task_id.replace("_", " ").title())

            task_list.append({
                "task_id": task_id,
                "family_id": family_id,
                "name": name,
                "description": instr,
                "difficulty": int(difficulty) if str(difficulty).isdigit() else idx,
                "tests": [],
                "timeout_seconds": toml_data.get("timeout", 300),
            })

        family_json = {
            "family_id": family_id,
            "description": f"SkillFlow benchmark family: {family_id}",
            "tasks": task_list,
        }
        out_path = FAMILIES_OUT / f"{family_id}.json"
        out_path.write_text(json.dumps(family_json, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("  写入 %s (%d tasks)", out_path.name, len(task_list))

    logger.info("完成！生成 %d 个 family JSON", len(families))


def main() -> None:
    parser = argparse.ArgumentParser(description="从 HuggingFace 下载 SkillFlow 指令文件并生成 family JSON")
    parser.add_argument("--hf_endpoint", default=None, help="HuggingFace 镜像站")
    args = parser.parse_args()
    build_families(hf_endpoint=args.hf_endpoint or os.environ.get("HF_ENDPOINT"))


if __name__ == "__main__":
    main()
