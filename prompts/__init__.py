"""
prompts/ — 官方实验性提示词配置（experimental prompt configuration）

[OFFICIAL] stage1_prompt.txt / stage2_prompt.txt 标签：保留官方实验资产
（非算法源码，允许原文保留）。patch_prompt.txt 为 [ENGINEERING]
（本项目自行设计，非官方原文）。

设计说明：
    Prompt 属于实验配置（experimental configuration / agent instruction），
    不属于算法源码。本目录下的 .txt 文件按官方 SkillFlow 实验中使用的
    Stage1 evolution prompt / Stage2 merge prompt / patch distillation prompt
    保留其文本内容，作为实验条件的一部分，与论文/官方实验保持一致。

    算法实现（Stage1 规划逻辑、Stage2 合并执行逻辑、Patch 蒸馏流水线等）
    独立编写，见 server/planner.py、server/merge.py、client/distiller.py ——
    这些模块只做「输入 → 调用 LLM → 解析输出」，不包含从官方源码复制的逻辑。

文件清单：
    stage1_prompt.txt — Stage1 (Evolution Planning) 系统提示词模板
                        （对应官方 task_update_skill/SKILL.md 描述的行为规范）
    stage2_prompt.txt — Stage2 (Per-Client Merge) 系统提示词模板
                        （对应官方 merge_skill/SKILL.md 描述的行为规范）
    patch_prompt.txt  — Patch 蒸馏（PatchDistiller）系统提示词模板
                        （本项目自行设计；官方 patcher 依赖不可见的外部
                        SkillFlow 库 `libs.skill_evolution.patcher.SkillPatchEvolver`，
                        其具体提示词文本本项目从未接触过，因此并非"保留官方原文"，
                        而是根据论文 Section 4.1.2 的输入/输出规格独立实现）
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(filename: str) -> str:
    """
    读取 prompts/ 目录下的提示词模板文件，返回原始文本（未 format）。

    Args:
        filename: 文件名，如 "stage1_prompt.txt"

    Returns:
        文件的完整文本内容（保留末尾换行由调用方决定是否 strip）
    """
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")
