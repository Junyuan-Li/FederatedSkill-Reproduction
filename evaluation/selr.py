"""
selr.py — 论文 Appendix E：Sensitive Entity Leakage Rate（SELR）标准实现
（Phase14 任务3新增）

论文 Eq.(5)（Appendix E）：

    SELR(C) = |{e ∈ E_sens(τ) : leak(e, C)}| / |E_sens(τ)|

即：一份轨迹 τ 中的敏感实体集合 E_sens(τ)，有多大比例原样出现（"泄露"）在
被审计语料 C（如上传给服务器的 patch/upsert 内容）里。

本模块按论文 Appendix E 描述的三步流程，提供三个对应函数：
    1. extract_sensitive_entities(trajectory_text) —— 抽取 E_sens(τ)
    2. audit_patch_leakage(sensitive_entities, patch_text) —— 审计泄露子集
    3. compute_selr(sensitive_entities, leaked_entities) —— 按 Eq.(5) 算比例

以及 canary_injection_experiment()：对应 Appendix E.5 的合成 PII（canary）
注入实验，用已知的合成敏感值做"可控泄露审计"。

⚠ 复用说明：敏感实体的正则识别复用 evaluation/privacy.py 里已经实现、
   已有回归测试覆盖的 _SENSITIVE_PATTERNS / scan_sensitive_entities()，
   不重复维护第二套正则表（避免两套实体识别规则互相漂移、结果不一致）。
   本模块是在 evaluation/privacy.py 基础上，按论文 Appendix E 的函数
   命名/三步流程重新组织的调用方式，属于新增文件，不修改
   evaluation/privacy.py 的任何已有接口/行为。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from evaluation.privacy import inject_canaries
from evaluation.privacy import scan_sensitive_entities as _scan_sensitive_entities

__all__ = [
    "extract_sensitive_entities",
    "audit_patch_leakage",
    "compute_selr",
    "compute_selr_from_texts",
    "CanaryExperimentResult",
    "canary_injection_experiment",
    "canary_experiment_to_csv",
]


# ---------------------------------------------------------------------------
# 步骤①②③：抽取 -> 审计 -> 计算 SELR
# ---------------------------------------------------------------------------


def extract_sensitive_entities(trajectory_text: str) -> set[str]:
    """
    论文 Appendix E 步骤①：从完整轨迹文本中抽取敏感实体集合 E_sens(τ)。

    Args:
        trajectory_text: 完整轨迹文本（worker 侧 prompt+生成过程的拼接）

    Returns:
        去重后的敏感实体**值**集合（不含类别标签；需要类别标签时改用
        evaluation.privacy.scan_sensitive_entities() 的 (value, category) 输出）
    """
    return {value for value, _category in _scan_sensitive_entities(trajectory_text)}


def audit_patch_leakage(sensitive_entities: set[str], patch_text: str) -> set[str]:
    """
    论文 Appendix E 步骤②：审计泄露——逐一检查 sensitive_entities 中的每个
    实体是否**原样**出现在 patch_text（被审计语料 C，如上传的 patch）里。

    Args:
        sensitive_entities: extract_sensitive_entities() 的输出，E_sens(τ)
        patch_text: 被审计语料 C（如 patch 的 upsert 内容拼接）

    Returns:
        leaked_entities ⊆ sensitive_entities（原样出现在 patch_text 中的子集）
    """
    return {e for e in sensitive_entities if e and e in patch_text}


def compute_selr(sensitive_entities: set[str], leaked_entities: set[str]) -> float:
    """
    论文 Eq.(5)：SELR = |leaked_entities| / |sensitive_entities|。

    Args:
        sensitive_entities: E_sens(τ)
        leaked_entities: audit_patch_leakage() 的输出（应为 sensitive_entities 子集）

    Returns:
        SELR ∈ [0, 1]；sensitive_entities 为空时返回 0.0
        （轨迹本身没有敏感实体，定义上无泄露风险）
    """
    if not sensitive_entities:
        return 0.0
    return len(leaked_entities) / len(sensitive_entities)


def compute_selr_from_texts(trajectory_text: str, patch_text: str) -> dict:
    """
    便捷封装：一次性完成 抽取 -> 审计 -> 计算 SELR 三步
    （等价于依次调用 extract_sensitive_entities / audit_patch_leakage / compute_selr）。

    Returns:
        {"selr": float, "n_sensitive": int, "n_leaked": int,
         "leaked_entities": list[str]}
    """
    sensitive = extract_sensitive_entities(trajectory_text)
    leaked = audit_patch_leakage(sensitive, patch_text)
    return {
        "selr": compute_selr(sensitive, leaked),
        "n_sensitive": len(sensitive),
        "n_leaked": len(leaked),
        "leaked_entities": sorted(leaked),
    }


# ---------------------------------------------------------------------------
# Canary injection experiment（论文 Appendix E.5）
# ---------------------------------------------------------------------------


@dataclass
class CanaryExperimentResult:
    """一次 canary 注入实验的结果（对应论文 Table 9 风格的一行审计记录）。"""

    canaries: dict[str, str]
    leaked: dict[str, bool] = field(default_factory=dict)

    @property
    def leak_count(self) -> int:
        return sum(1 for is_leaked in self.leaked.values() if is_leaked)

    @property
    def selr(self) -> float:
        """本次试验的 canary-SELR = 泄露的 canary 数 / 注入的 canary 总数。"""
        if not self.canaries:
            return 0.0
        return self.leak_count / len(self.canaries)

    def to_dict(self) -> dict:
        row: dict = {
            "n_canaries": len(self.canaries),
            "n_leaked": self.leak_count,
            "selr": round(self.selr, 4),
        }
        row.update({f"leaked__{name}": is_leaked for name, is_leaked in self.leaked.items()})
        return row


def canary_injection_experiment(
    distill_fn: Callable[[str], str],
    trajectory_text: str = "",
    canaries: dict[str, str] | None = None,
    n_trials: int = 1,
) -> list[CanaryExperimentResult]:
    """
    论文 Appendix E.5：向轨迹注入已知合成 PII（canary），跑 n_trials 次
    蒸馏/压缩流程，审计每次结果里是否原样出现具体的 canary 值。

    Args:
        distill_fn: trajectory_text -> patch_text 的可调用对象（如包一层
            client.distiller.PatchDistiller.distill()；本模块不直接依赖
            具体的蒸馏器实现，避免耦合）
        trajectory_text: 基础轨迹文本，canary 会被追加/注入到其中
        canaries: 合成 PII 字典 {name: value}；默认复用
            evaluation.privacy.DEFAULT_CANARIES
        n_trials: 独立重复试验次数（对应论文多次审计取统计量的做法）

    Returns:
        每次试验对应一个 CanaryExperimentResult 的列表
    """
    results: list[CanaryExperimentResult] = []
    for _ in range(max(1, n_trials)):
        augmented_text, injected = inject_canaries(trajectory_text, canaries)
        patch_text = distill_fn(augmented_text)
        leaked = {name: (value in patch_text) for name, value in injected.items()}
        results.append(CanaryExperimentResult(canaries=injected, leaked=leaked))
    return results


def canary_experiment_to_csv(results: list[CanaryExperimentResult], path: str | Path) -> Path:
    """把多次 canary 实验结果写成 CSV（一行一次试验），用于论文 Table 9 风格汇总。"""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r.to_dict() for r in results]
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return out_path
    fieldnames = sorted({key for row in rows for key in row})
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path
