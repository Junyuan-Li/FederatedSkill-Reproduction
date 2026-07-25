"""
check_dataset.py — 验证本地 SkillFlow 数据集完整性

用法：
    python benchmark/check_dataset.py
    python benchmark/check_dataset.py --data_dir benchmark/cache/SkillFlow-Task
    python benchmark/check_dataset.py --data_dir benchmark/cache/SkillFlow-Task --verbose

输出示例：
    ============================
    SkillFlow Dataset Summary
    ============================
    数据目录: benchmark/cache/SkillFlow-Task
    Family 数: 20
    总任务数:  178
    ---
    Family             任务数  难度范围  状态
    ──────────────────────────────────────
    bash-scripting     9      1-9      ✓
    data-processing    8      1-8      ✓
    ...
    ---
    ⚠ 0 个 family 存在字段缺失（instruction / tests）
    ✓ 数据集验证通过
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _check_task_dir(task_dir: Path) -> list[str]:
    """
    检查单个任务目录必要文件。
    返回缺失字段列表（空列表 = 完整）。
    """
    missing = []
    # instruction.md 或 instruction.txt
    has_instruction = any(
        (task_dir / name).exists()
        for name in ("instruction.md", "instruction.txt", "task.toml")
    )
    if not has_instruction:
        missing.append("instruction")

    # tests 目录或 tests.sh
    has_tests = (task_dir / "tests").is_dir() or (task_dir / "tests.sh").exists()
    if not has_tests:
        missing.append("tests")

    return missing


def check_dataset(data_dir: Path, verbose: bool = False) -> bool:
    """
    逐一检查 data_dir 下所有 family / task 目录，返回是否全部通过。
    """
    print()
    print("=" * 50)
    print("  SkillFlow Dataset Summary")
    print("=" * 50)

    if not data_dir.is_dir():
        print(f"\n[错误] 数据目录不存在: {data_dir}")
        print("请先下载数据集：python scripts/download_skillflow_dataset.py")
        return False

    # 收集 family 目录（一级子目录，且包含子子目录的视为 family）
    family_dirs = sorted(
        p for p in data_dir.iterdir()
        if p.is_dir() and any(c.is_dir() for c in p.iterdir() if c.is_dir())
    )

    # 若没有符合结构的 family，也接受 data_dir 本身就是一个 family 列表
    if not family_dirs:
        family_dirs = sorted(p for p in data_dir.iterdir() if p.is_dir())

    print(f"\n  数据目录: {data_dir}")
    print(f"  Family 数: {len(family_dirs)}")

    total_tasks = 0
    bad_families: list[str] = []
    family_rows: list[tuple] = []

    for fam_dir in family_dirs:
        task_dirs = sorted(p for p in fam_dir.iterdir() if p.is_dir())
        n_tasks = len(task_dirs)
        total_tasks += n_tasks

        # 检查每个任务
        issues: list[str] = []
        difficulties: list[int] = []
        for td in task_dirs:
            missing = _check_task_dir(td)
            if missing:
                issues.append(f"{td.name}: 缺少 {','.join(missing)}")

            # 尝试从目录名或 task.toml 读取难度
            try:
                # 惯例：目录名末尾数字为难度
                diff_str = "".join(c for c in td.name if c.isdigit())
                if diff_str:
                    difficulties.append(int(diff_str[-1]))
            except Exception:
                pass

        diff_range = (
            f"{min(difficulties)}-{max(difficulties)}"
            if difficulties else "?"
        )
        status = "✓" if not issues else f"⚠ {len(issues)} 个任务有问题"
        family_rows.append((fam_dir.name, n_tasks, diff_range, status))
        if issues:
            bad_families.append(fam_dir.name)
            if verbose:
                for issue in issues[:5]:   # 最多显示 5 个
                    print(f"    {fam_dir.name}/{issue}")

    print(f"  总任务数: {total_tasks}")
    print()

    # 打印 family 摘要表
    print(f"  {'Family':<25} {'任务数':>4}  {'难度范围':<8}  状态")
    print("  " + "─" * 55)
    for name, n_tasks, diff_range, status in family_rows:
        print(f"  {name:<25} {n_tasks:>4}  {diff_range:<8}  {status}")

    print()
    if bad_families:
        print(f"  ⚠ {len(bad_families)} 个 family 存在字段缺失：{', '.join(bad_families[:5])}")
    else:
        print(f"  ✓ 所有 family 结构完整（共 {len(family_dirs)} families / {total_tasks} tasks）")

    # 论文期望值检查
    if len(family_dirs) > 0:
        print()
        if len(family_dirs) == 20:
            print(f"  ✓ Family 数量符合论文（20）")
        else:
            print(f"  ⚠ Family 数量 {len(family_dirs)}（论文期望 20），数据集可能不完整")

        if 160 <= total_tasks <= 200:
            print(f"  ✓ 总任务数符合论文范围（{total_tasks}）")
        else:
            print(f"  ⚠ 总任务数 {total_tasks}（论文期望 160-200）")

    print()
    return len(bad_families) == 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="验证本地 SkillFlow 数据集完整性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--data_dir",
        default=str(ROOT / "benchmark" / "cache" / "SkillFlow-Task"),
        help="数据集根目录（默认: benchmark/cache/SkillFlow-Task）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="打印每个有问题的具体任务路径",
    )
    args = parser.parse_args()

    ok = check_dataset(Path(args.data_dir), verbose=args.verbose)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
