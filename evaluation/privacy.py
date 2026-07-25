"""
privacy.py — 隐私分析指标（对应论文 Appendix E / Table 8）

论文 Table 8 核心指标：
  SELR (Sensitive Entity Leakage Rate) = 敏感实体泄露率
    FederatedSkill:   5.08%
    Full Trajectory:  52.0%

SELR 的计算需要扫描上传内容中的敏感实体，本模块实现两个层次：
  1. TokenCompressionProxy  — token 层面的代理指标（与 compression_ratio 数值相同）
  2. SensitiveEntityScanner — 基于正则的敏感实体计数器（对应论文真实 SELR 计算）

注意：论文声明该隐私保证是 empirical 而非 cryptographic。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# 敏感实体类别正则（对应论文 Appendix E 中的 entity type list）
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: dict[str, re.Pattern] = {
    # 文件名和路径（任务特定）
    "file_path":       re.compile(r"\b(?:[\w./-]+\.(?:csv|txt|xlsx?|json|py|pdf|toml|yaml|yml))\b", re.I),
    # 行业特定 ID（如 supplier ID、SKU、invoice#）
    "domain_id":       re.compile(r"\b(?:[A-Z]{2,6}-?\d{4,10}|INV-?\d+|SKU-?\d+|SUP-?\d+)\b"),
    # 数字常量（金额、数量等任务特定值）
    "numeric_literal": re.compile(r"\b\d{4,}(?:\.\d+)?\b"),
    # 日期（任务输入中的特定日期）
    "date":            re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}/\d{2}/\d{4}\b"),
    # API key / secret 形式
    "api_secret":      re.compile(r"\b[A-Za-z0-9]{32,}\b"),
    # 任务描述中的专有名词（简单启发：全大写单词序列）
    "proper_noun":     re.compile(r"\b[A-Z][A-Z0-9]{3,}\b"),
    # 邮箱地址（Task2 新增：Appendix E entity type 之一）
    "email":           re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    # 人名（弱启发式：连续两个首字母大写的单词，如 "John Smith"）
    "person_name":     re.compile(r"\b[A-Z][a-z]{1,20}\s+[A-Z][a-z]{1,20}\b"),
    # 银行账号 / 身份证号等长数字串（9~18 位，覆盖 SSN 风格 xxx-xx-xxxx）
    "bank_account":    re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{9,18}\b"),
    # 电话号码
    "phone_number":    re.compile(r"\b1[3-9]\d{9}\b|\b\d{3}-\d{3,4}-\d{4}\b"),
}

# 允许出现在技能库 SKILL.md 中的"非敏感"伪实体（白名单前缀）
_ALLOWED_PREFIXES = (
    "README", "SKILL", "DONE", "DECISIONS", "True", "False", "None",
    "SKILL.md", "scripts", "references", "assets", "library",
)


@dataclass
class EntityScanResult:
    """
    对一段文本的敏感实体扫描结果。
    """
    text_len: int = 0
    total_entities: int = 0
    leaked_entities: int = 0              # 不在白名单中的命中数
    by_category: dict[str, int] = field(default_factory=dict)

    @property
    def selr(self) -> float:
        """
        Sensitive Entity Leakage Rate = leaked / total_entities

        若 total_entities = 0 则返回 0.0（无实体 → 无泄露）。
        """
        if self.total_entities == 0:
            return 0.0
        return self.leaked_entities / self.total_entities


def scan_for_sensitive_entities(text: str) -> EntityScanResult:
    """
    对一段文本执行敏感实体扫描。

    Args:
        text:  上传内容字符串（patch upsert_files 拼接后的内容）

    Returns:
        EntityScanResult 含 SELR
    """
    result = EntityScanResult(text_len=len(text))
    all_matches: list[str] = []

    for cat, pattern in _SENSITIVE_PATTERNS.items():
        matches = pattern.findall(text)
        count = 0
        for m in matches:
            if not any(m.startswith(p) for p in _ALLOWED_PREFIXES):
                count += 1
                all_matches.append(m)
        result.by_category[cat] = count
        result.total_entities += count

    # leaked = 全部命中（没有二次过滤，直接等于 total，
    # 与 total_entities 等价，但留接口供后续细化白名单）
    result.leaked_entities = result.total_entities
    return result


def scan_sensitive_entities(text: str) -> list[tuple[str, str]]:
    """
    对一段文本执行敏感实体扫描，返回**详细实体清单**（值 + 类别），而非
    scan_for_sensitive_entities() 那样只返回计数汇总。

    这是 Task2 要求的 `scan_sensitive_entities()` 接口——用于 compute_SELR()
    和 canary_injection_test() 需要拿到具体实体值去和目标文本做逐一比对的场景
    （scan_for_sensitive_entities() 的计数汇总接口不够用）。

    Args:
        text: 待扫描文本

    Returns:
        [(entity_value, category), ...]，已按 _ALLOWED_PREFIXES 白名单过滤，
        未去重（同一实体出现多次会重复出现，便于统计频次）。
    """
    entities: list[tuple[str, str]] = []
    for cat, pattern in _SENSITIVE_PATTERNS.items():
        for m in pattern.findall(text):
            if not any(m.startswith(p) for p in _ALLOWED_PREFIXES):
                entities.append((m, cat))
    return entities


def compute_selr_for_patch(upsert_files: dict[str, str]) -> float:
    """
    计算一个 WorkerPatch 的 SELR。

    Args:
        upsert_files:  {relative_path: file_content} — patch 上传的文件内容

    Returns:
        SELR ∈ [0.0, 1.0]
    """
    if not upsert_files:
        return 0.0
    combined = "\n".join(upsert_files.values())
    return scan_for_sensitive_entities(combined).selr


def compute_selr_for_trajectory(trajectory_text: str) -> float:
    """
    计算完整轨迹文本的 SELR（A3 ablation 的对比基线）。

    Args:
        trajectory_text: 完整轨迹字符串（含工具调用参数等任务特定内容）

    Returns:
        SELR ∈ [0.0, 1.0]（通常远高于 patch 的 SELR）
    """
    return scan_for_sensitive_entities(trajectory_text).selr


def compute_SELR(source_text: str, target_text: str) -> dict:
    """
    严格版 SELR：从 source_text（如完整轨迹）里提取敏感实体，逐一检查它们
    是否**原样出现**在 target_text（如上传的 patch）里，泄露率 = 出现在
    target 中的实体数 / source 中的实体总数。

    与 compute_selr_for_patch() / compute_selr_for_trajectory() 的区别：
    那两个函数各自独立扫描**单一文本**，把"该文本里出现了多少实体"直接
    当成 SELR，本质是"实体密度"而非真正的"泄露率"（没有 source→target 的
    比对，无法回答"轨迹里的敏感信息有没有流进 patch"这个问题）。
    compute_SELR() 才是论文 Appendix E 定义的真正版本：以完整轨迹为
    "泄露上限"，衡量 patch 相对轨迹泄露了多大比例的敏感实体。

    Args:
        source_text: 敏感实体的来源文本（通常是完整 trajectory，代表"能泄露的上限"）
        target_text: 被检查是否发生泄露的目标文本（通常是 patch upsert 内容）

    Returns:
        {
            "selr": float,                # 泄露率 ∈ [0, 1]；source 无实体时为 0.0
            "total_entities": int,        # source 中检测到的（去重后）敏感实体数
            "leaked_entities": int,       # 其中原样出现在 target 中的数量
            "leaked_values": list[str],   # 具体泄露的实体值（便于人工复核/审计）
        }
    """
    source_entities = {value for value, _cat in scan_sensitive_entities(source_text)}
    if not source_entities:
        return {"selr": 0.0, "total_entities": 0, "leaked_entities": 0, "leaked_values": []}

    leaked = [value for value in source_entities if value in target_text]
    return {
        "selr": len(leaked) / len(source_entities),
        "total_entities": len(source_entities),
        "leaked_entities": len(leaked),
        "leaked_values": sorted(leaked),
    }


# ---------------------------------------------------------------------------
# 隐私汇总（跨所有 worker 和 rounds）
# ---------------------------------------------------------------------------

@dataclass
class PrivacySummary:
    """
    整个实验的隐私指标汇总。

    对应论文 Appendix E Table 8 的一行（某 setting 的汇总）。
    """
    setting_name: str
    n_patches: int = 0
    mean_patch_selr: float = 0.0       # 平均 patch SELR（论文 ~5.08%）
    mean_trajectory_selr: float = 0.0  # 平均全轨迹 SELR（论文 ~52.0%）
    mean_compression_ratio: float = 0.0

    def privacy_gain(self) -> float:
        """
        实际隐私增益 = trajectory_SELR - patch_SELR

        论文 Table 8：52.0% - 5.08% ≈ 46.92 个百分点的增益。
        """
        return max(0.0, self.mean_trajectory_selr - self.mean_patch_selr)

    def to_dict(self) -> dict:
        return {
            "setting_name": self.setting_name,
            "n_patches": self.n_patches,
            "mean_patch_selr_pct": round(self.mean_patch_selr * 100, 2),
            "mean_trajectory_selr_pct": round(self.mean_trajectory_selr * 100, 2),
            "privacy_gain_pct": round(self.privacy_gain() * 100, 2),
            "mean_compression_ratio_pct": round(self.mean_compression_ratio * 100, 2),
        }


# ---------------------------------------------------------------------------
# Table-like CSV 导出（Task2 要求：SELR 结果导出为可直接放进论文对比表的 CSV）
# ---------------------------------------------------------------------------


def privacy_summaries_to_csv(summaries: list["PrivacySummary"], path: str | Path) -> Path:
    """把多个 setting 的 PrivacySummary 写成一张 CSV 表（对应论文 Table 8 的多行）。"""
    import csv

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [s.to_dict() for s in summaries]
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return out_path
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return out_path


# ---------------------------------------------------------------------------
# Canary injection test（论文 Appendix E：合成 PII 注入审计）
# ---------------------------------------------------------------------------

# 默认注入的合成敏感实体——均为虚构值，不对应任何真实个人/机构信息，
# 覆盖 person_name / bank_account(SSN 风格) / proper_noun(项目代号) / email 四类。
DEFAULT_CANARIES: dict[str, str] = {
    "person_name": "John Smith",
    "ssn": "123-45-6789",
    "project_codename": "PROJECTNIGHTOWL",
    "email": "john.smith@internal-corp.example",
}


def inject_canaries(
    trajectory_text: str, canaries: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """
    把一组"金丝雀"合成敏感信息注入到轨迹文本末尾。

    对应论文 Appendix E 的审计方法：人为在 trajectory 里插入已知的合成 PII
    （不是任务本身产生的真实敏感信息），之后检查这些具体值是否会出现在
    最终上传的 patch 里——如果一个都没有泄露，说明蒸馏/压缩过程是"安全"的。

    Args:
        trajectory_text: 基础轨迹文本（不含 canary）
        canaries: 要注入的合成 PII 字典；默认 DEFAULT_CANARIES

    Returns:
        (增强后的轨迹文本, 实际注入的 canary 字典)
    """
    canaries = dict(canaries or DEFAULT_CANARIES)
    canary_block = "\n".join(f"[CANARY:{k}] {v}" for k, v in canaries.items())
    augmented = (
        f"{trajectory_text}\n\n# --- injected canaries (privacy audit only) ---\n"
        f"{canary_block}\n"
    )
    return augmented, canaries


@dataclass
class CanaryAuditResult:
    """单次 canary 注入审计结果。"""

    canaries: dict[str, str]
    leaked: dict[str, bool] = field(default_factory=dict)

    @property
    def any_leaked(self) -> bool:
        return any(self.leaked.values())

    @property
    def leak_count(self) -> int:
        return sum(1 for v in self.leaked.values() if v)

    def to_dict(self) -> dict:
        row = {
            "n_canaries": len(self.canaries),
            "n_leaked": self.leak_count,
            "any_leaked": self.any_leaked,
        }
        row.update({f"leaked__{name}": leaked for name, leaked in self.leaked.items()})
        return row


def canary_injection_test(
    distill_fn: Callable[[str], str],
    trajectory_text: str = "",
    canaries: dict[str, str] | None = None,
) -> CanaryAuditResult:
    """
    对一次真实的蒸馏/压缩流程做 canary 注入审计（论文 Appendix E）。

    Args:
        distill_fn: 输入完整轨迹文本、返回蒸馏后 patch 文本的可调用对象。
                    典型用法是包一层调用 client.distiller.PatchDistiller
                    （本函数不直接 import PatchDistiller，避免
                    evaluation -> client 的循环/强耦合依赖，也便于单测里
                    传入纯函数 mock，不需要真实 LLM 调用）。
        trajectory_text: 基础轨迹文本（不含 canary），默认空字符串。
        canaries: 要注入的合成 PII 字典，默认 DEFAULT_CANARIES。

    Returns:
        CanaryAuditResult：每个 canary 是否原样出现在蒸馏后的 patch 里。
        对应论文 Table 9 的"54 次审计、0 次泄露"这类结果——理想情况下
        result.any_leaked 应为 False。
    """
    augmented_trajectory, injected = inject_canaries(trajectory_text, canaries)
    patch_text = distill_fn(augmented_trajectory)
    leaked = {name: (value in patch_text) for name, value in injected.items()}
    return CanaryAuditResult(canaries=injected, leaked=leaked)


def canary_reports_to_csv(reports: list[CanaryAuditResult], path: str | Path) -> Path:
    """把多次 canary 审计结果写成 CSV（Table-like，每行一次审计）。"""
    import csv

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r.to_dict() for r in reports]
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return out_path
    fieldnames = sorted({k for row in rows for k in row})
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path
