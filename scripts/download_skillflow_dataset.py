"""
download_skillflow_dataset.py — 下载 SkillFlow 真实数据集 CLI

用法：
    python scripts/download_skillflow_dataset.py
    python scripts/download_skillflow_dataset.py --cache_dir data/skillflow
    python scripts/download_skillflow_dataset.py --force       # 强制重新下载
    python scripts/download_skillflow_dataset.py --token HF_xxx  # 私有/限流

注意：
    本脚本会下载约 1.6GB 数据，请确认网络可访问 huggingface.co
    或通过 HF_ENDPOINT 环境变量指定镜像站（如 https://hf-mirror.com）

下载完成后验证：
    python benchmark/check_dataset.py --data_dir benchmark/cache/SkillFlow-Task
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="下载 SkillFlow-Task 数据集到本地缓存目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--cache_dir",
        default=str(ROOT / "benchmark" / "cache"),
        help="本地缓存根目录（默认: benchmark/cache）",
    )
    parser.add_argument(
        "--repo_id",
        default="zhang-ziao/SkillFlow-Task",
        help="HuggingFace dataset repo id",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="HuggingFace access token（私有数据集或绕限流需要）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使本地已有数据也强制重新下载",
    )
    parser.add_argument(
        "--hf_endpoint",
        default=None,
        help="HuggingFace 镜像站（如 https://hf-mirror.com），会覆盖 HF_ENDPOINT 环境变量",
    )
    args = parser.parse_args()

    # 支持 HF 镜像站（国内加速）
    if args.hf_endpoint:
        os.environ["HF_ENDPOINT"] = args.hf_endpoint
        logger.info("使用 HuggingFace 镜像: %s", args.hf_endpoint)
    elif os.environ.get("HF_ENDPOINT"):
        logger.info("使用 HuggingFace 镜像 (来自环境变量): %s", os.environ["HF_ENDPOINT"])

    # 目标目录 = cache_dir/SkillFlow-Task
    dest_dir = Path(args.cache_dir) / "SkillFlow-Task"
    logger.info("目标目录: %s", dest_dir)

    # 调用已有 downloader
    try:
        from benchmark.skillflow_adapter.downloader import (
            is_dataset_present,
            download_skillflow_dataset,
        )
    except ImportError as e:
        logger.error("导入 downloader 失败: %s", e)
        sys.exit(1)

    if is_dataset_present(dest_dir) and not args.force:
        logger.info("数据集已存在: %s", dest_dir)
        logger.info("如需重新下载请添加 --force 参数")
        print(f"\n数据集路径: {dest_dir}")
        print("运行验证: python benchmark/check_dataset.py --data_dir", dest_dir)
        return

    print("=" * 50)
    print(f"  即将下载 {args.repo_id}")
    print(f"  目标: {dest_dir}")
    print(f"  预计大小: ~1.6 GB")
    print("=" * 50)
    confirm = input("\n确认下载？(yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("已取消下载。")
        return

    logger.info("开始下载...")
    try:
        local_path = download_skillflow_dataset(
            dest_dir=dest_dir,
            repo_id=args.repo_id,
            token=args.token,
            force=args.force,
        )
        logger.info("下载完成: %s", local_path)
        print("\n" + "=" * 50)
        print(f"  ✓ 下载完成")
        print(f"  数据集路径: {local_path}")
        print(f"  运行验证: python benchmark/check_dataset.py --data_dir {local_path}")
        print("=" * 50)
    except RuntimeError as e:
        logger.error("下载错误: %s", e)
        sys.exit(1)
    except ImportError as e:
        logger.error("%s", e)
        print("\n安装: pip install huggingface-hub")
        sys.exit(1)
    except Exception as e:
        logger.error("下载失败: %s", e)
        raise


if __name__ == "__main__":
    main()
