"""evaluation package — 实验指标与结果输出"""

from evaluation.metrics import FederatedMetrics, TrialSnapshot
from evaluation.evaluator import ExperimentEvaluator, ExperimentResult, RoundEvalResult
from evaluation.reporter import ResultReporter
from evaluation.privacy import (
    scan_for_sensitive_entities, scan_sensitive_entities,
    compute_selr_for_patch, compute_selr_for_trajectory, compute_SELR,
    PrivacySummary, privacy_summaries_to_csv,
    DEFAULT_CANARIES, inject_canaries, canary_injection_test,
    CanaryAuditResult, canary_reports_to_csv,
)
from evaluation.plotter import (
    plot_success_rate_curves, plot_library_size_curves,
    plot_cost_per_task, generate_all_figures,
)
from evaluation.results_exporter import ResultsExporter, ExportSummary
from evaluation.capability_tracker import CapabilityEvolutionTracker, RoundCapabilitySummary
from evaluation.federated_score import (
    weighted_global_score, normalize_weights, weighted_global_score_over_rounds,
)
from evaluation.selr import (
    extract_sensitive_entities, audit_patch_leakage, compute_selr,
    compute_selr_from_texts, CanaryExperimentResult,
    canary_injection_experiment, canary_experiment_to_csv,
)
from evaluation.paper_export import export_setting_csvs, scaffold_all_settings
from evaluation.audit_trace import (
    EvolutionTraceRecord, AuditTraceRecorder,
    build_trace_records, compute_hash, compute_diff,
    load_trace_jsonl, find_records,
    SKILL_EVOLUTION_FIELDS, FAMILY_SKILL_EVOLUTION_FIELDS, build_skill_evolution_rows,
)
from evaluation.cost_accounting import (
    LLMCallCostRecord, CostAccountant,
    CommunicationAuditRecord, CommunicationAuditor,
    measure_patch_bytes, measure_snapshot_bytes, measure_trajectory_bytes_if_transmitted,
    build_communication_record,
)
from evaluation.fusion_trace import (
    FusionTraceRecord, FusionTraceRecorder, build_fusion_trace_record,
)
from evaluation.memory_trace import MemoryAccessRecord, MemoryTraceRecorder
from evaluation.transfer_trace import (
    TransferTraceRecord, TransferTraceRecorder, build_transfer_trace_record,
)

__all__ = [
    "FederatedMetrics", "TrialSnapshot",
    "ExperimentEvaluator", "ExperimentResult", "RoundEvalResult",
    "ResultReporter",
    # Privacy analysis (Appendix E / Table 8)
    "scan_for_sensitive_entities", "scan_sensitive_entities",
    "compute_selr_for_patch", "compute_selr_for_trajectory", "compute_SELR",
    "PrivacySummary", "privacy_summaries_to_csv",
    "DEFAULT_CANARIES", "inject_canaries", "canary_injection_test",
    "CanaryAuditResult", "canary_reports_to_csv",
    # Paper figure generation
    "plot_success_rate_curves", "plot_library_size_curves",
    "plot_cost_per_task", "generate_all_figures",
    # Results CSV + figure export
    "ResultsExporter", "ExportSummary",
    # Phase13: capability matrix evolution history + CSV
    "CapabilityEvolutionTracker", "RoundCapabilitySummary",
    # Phase14 任务2: 论文 Eq.(3) 全局加权得分
    "weighted_global_score", "normalize_weights", "weighted_global_score_over_rounds",
    # Phase14 任务3: Appendix E SELR 标准实现（复用 evaluation.privacy 正则引擎）
    "extract_sensitive_entities", "audit_patch_leakage", "compute_selr",
    "compute_selr_from_texts", "CanaryExperimentResult",
    "canary_injection_experiment", "canary_experiment_to_csv",
    # Phase14 任务4: 按 setting 导出论文 Figure/Table CSV
    "export_setting_csvs", "scaffold_all_settings",
    # Appendix A 复现能力: EvolutionTraceRecord 审计追踪（TASK3，纯审计日志，
    # 不参与 Stage1/Stage2 决策）
    "EvolutionTraceRecord", "AuditTraceRecorder",
    "build_trace_records", "compute_hash", "compute_diff",
    "load_trace_jsonl", "find_records",
    # Figure 3 技能增长明细（Result Reproduction Readiness Audit TASK4，纯只读
    # 派生统计，不改变任何演化决策）
    "SKILL_EVOLUTION_FIELDS", "FAMILY_SKILL_EVOLUTION_FIELDS", "build_skill_evolution_rows",
    # Appendix C 复现能力: 统一 LLM 调用成本核算 + client↔server 通信字节审计
    # （TASK4，纯审计，不改变任何执行/决策逻辑）
    "LLMCallCostRecord", "CostAccountant",
    "CommunicationAuditRecord", "CommunicationAuditor",
    "measure_patch_bytes", "measure_snapshot_bytes", "measure_trajectory_bytes_if_transmitted",
    "build_communication_record",
    # Full Reproduction Alignment Audit TASK4: Skill Fusion Fidelity 追踪
    # （纯旁路审计，不改变任何 Stage2 合并决策）
    "FusionTraceRecord", "FusionTraceRecorder", "build_fusion_trace_record",
    # Full Reproduction Alignment Audit TASK5: 两级记忆读/用/写审计追踪
    "MemoryAccessRecord", "MemoryTraceRecorder",
    # Full Reproduction Alignment Audit TASK6: 跨客户端技能迁移验证 +
    # transfer_report.json
    "TransferTraceRecord", "TransferTraceRecorder", "build_transfer_trace_record",
]
