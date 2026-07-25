"""
paper_export.py — 按 setting 导出论文 Figure/Table 所需的四张 CSV（Phase14 任务4）

读取 experiments/run_experiment.py::_save_results() 写出的
round_<N>_summary.json（schema 见该函数注释，本模块只读不改），为**单个
setting 目录**生成四张 CSV：

    metrics.csv     —— round_idx, success_rate, mean_library_size,
                        mean_skill_growth, n_solved, n_total
                        对应论文 Table 1 / Figure 2 / Figure 3

    capability.csv  —— round_idx, mean_library_size_before,
                        mean_library_size_after, mean_skill_growth,
                        covered, absorbing, broken, gap, coverage_ratio,
                        capability_data_source
                        若 round JSON 带有 run_experiment.py 新增的
                        "capability_summary"键（来自
                        evaluation.capability_tracker.CapabilityEvolutionTracker
                        记录的真实 covered/absorbing/broken/gap 四态快照），
                        则 covered/absorbing/broken/gap/coverage_ratio 为真实数据，
                        capability_data_source="real_capability_matrix"；
                        否则（如 Setting1 自进化没有 server/能力矩阵概念，或读取
                        旧版 round JSON），那五个字段为空，
                        capability_data_source="library_size_proxy"。
                        mean_library_size_before/after/mean_skill_growth 无论哪种
                        情形都会填写（来自 round summary JSON 本身就有的
                        library_size_before/after 快照）。

    privacy.csv     —— round_idx, compression_ratio, privacy_gain_proxy,
                        mean_selr, privacy_data_source
                        若 round JSON 里的 snapshots 带有 run_experiment.py 新增的
                        "selr" 字段（来自 evaluation.selr.compute_selr_from_texts()
                        对真实轨迹/patch 文本的计算，见
                        experiments/federated.py 和 experiments/baseline.py 的
                        _run_round()/_run_client_trial()），则 mean_selr 为真实
                        均值，privacy_data_source="text_selr"；否则（旧版 round
                        JSON）mean_selr 为空，privacy_data_source="token_proxy_only"，
                        仅保留 compression_ratio/privacy_gain_proxy 这对 token 级代理指标。

    cost.csv        —— round_idx, total_cost_usd, cost_per_solved_task,
                        client_cost_usd, server_cost_usd, total_cost_usd_unified,
                        communication_bytes, communication_trajectory_bytes
                        对应论文 Figure 4 通信/算力成本曲线；后五列为 TASK4
                        （Appendix C 成本复现审计）新增，来自
                        evaluation/cost_accounting.py::CostAccountant/
                        CommunicationAuditor（见该模块 docstring），旧版
                        round JSON（未经过 TASK4 wiring）该五列回退为空字符串。

不修改 evaluation/results_exporter.py 的任何已有接口/输出：
results_exporter 输出的是**跨 setting 汇总**CSV（写在
<results_dir>/tables/ 下），本模块输出的是**单 setting 目录**CSV
（写在 <setting_dir>/ 下），命名空间不冲突，两者并存。

Phase2（按 family 循环重构后新增）：
    当 <setting_dir>/experiment_summary.json 存在且 "mode" == "family_loop"
    （见 experiments/run_experiment.py::_save_family_loop_summary()）时，
    round_*_summary.json 实际存放在 <setting_dir>/families/<family_id>/ 下
    （每个 family 独立目录、独立轮次），本模块自动切换到
    export_family_loop_csvs()：
        metrics/capability/privacy/cost.csv 在原有列基础上新增
        "family_id" 列，按 family_id 再按 round_idx 排序——对应论文
        Figure 2（每个 family 的 SR-vs-round 曲线）/ Figure 3
        （每个 family 的技能库规模增长曲线）。
        table1.csv —— family_id, setting, is_paper_family, n_rounds,
        final_success_rate, final_mean_library_size,
        final_weighted_global_score：对应论文 Table 1（每个 family 一行的
        最终成功率汇总）。is_paper_family=False 的行是本项目自建的 5 个
        legacy family（data_cleaning/data_transformation/
        document_processing/financial_analysis/report_generation，见
        benchmark/family.py 模块 docstring），不对应论文 20 个真实
        SkillFlow family，做论文对比时应过滤掉，只是保留在 CSV 里做
        工程回归用。末尾额外追加一行 family_id="__MACRO_AVG__"，给出
        macro_avg_success_rate（仅对 is_paper_family=True 的 20 个官方
        family 取算术平均，TASK4）。
    调用方（run.py）无需改动：export_setting_csvs() 会自动探测并转发。
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from evaluation.audit_trace import (
    FAMILY_SKILL_EVOLUTION_FIELDS, SKILL_EVOLUTION_FIELDS,
    build_skill_evolution_rows, load_trace_jsonl,
)

logger = logging.getLogger(__name__)

#: 四张 CSV 各自的列名（供真实导出与占位 scaffold 共用，避免两处 schema 漂移）
METRICS_FIELDS = [
    "round_idx", "success_rate", "weighted_global_score",
    "mean_library_size", "mean_skill_growth", "n_solved", "n_total",
]
CAPABILITY_FIELDS = [
    "round_idx", "mean_library_size_before", "mean_library_size_after", "mean_skill_growth",
    "covered", "absorbing", "broken", "gap", "coverage_ratio", "capability_data_source",
]
PRIVACY_FIELDS = ["round_idx", "compression_ratio", "privacy_gain_proxy", "mean_selr", "privacy_data_source"]
#: TASK4（Appendix C 成本复现审计）新增五列，追加在旧列之后，不删除/不改名已有列：
#   client_cost_usd、server_cost_usd、total_cost_usd_unified 来自
#   evaluation/cost_accounting.py::CostAccountant（统一追踪 client_execution/
#   patch_distiller/stage1_planner/stage2_merge 四个环节的真实 LLM 调用成本，
#   补齐旧的 total_cost_usd 只计 client_execution 一个环节的缺口）；
#   communication_bytes、communication_trajectory_bytes 来自
#   CommunicationAuditor（后者恆为 0，确认隐私保证“trajectory bytes = 0”）。
#   旧版 round JSON（未提供 output_dir/未运行 TASK4 wiring）无这五个键时，
#   _rows_from_record() 回退为 "" 而不伪造 0.0（与 weighted_global_score/
#   mean_selr 的回退策略一致）。
COST_FIELDS = [
    "round_idx", "total_cost_usd", "cost_per_solved_task",
    "client_cost_usd", "server_cost_usd", "total_cost_usd_unified",
    "communication_bytes", "communication_trajectory_bytes",
    # Result Reproduction Readiness Audit TASK3 新增：四个组件的明细成本
    # （直接复用 CostAccountant.total_by_component() 的分类，见
    # experiments/federated.py::run()）+ total_cost_unified（与
    # total_cost_usd_unified 同值，只是换一个任务要求的列名，不重新
    # 计算）。不删除/不重命名旧列。
    "client_execution_cost", "patch_distill_cost", "stage1_cost", "stage2_cost",
    "total_cost_unified",
]

#: Result Reconstruction Audit（Figure 3）新增：论文 Figure 3 展示的是"每个
#: worker 在每个 family 下逐轮的技能库规模增长曲线"，而 capability.csv 的
#: mean_library_size_before/after 是跨 worker 求平均（见 _rows_from_record()），
#: 丢失了逐 worker 的粒度。round_*_summary.json 的 snapshots 列表里本身就
#: 有 worker_id + library_size_after（逐 worker 真实数据，见
#: experiments/run_experiment.py::_save_results()），这里只是换一种不做平均的
#: 方式导出成单独一张 CSV，不修改/不影响 metrics/capability/privacy/cost 四张
#: 已有 CSV 的任何字段或计算方式。
SKILL_GROWTH_FIELDS = ["worker_id", "round_idx", "number_of_skills"]
FAMILY_SKILL_GROWTH_FIELDS = ["family_id"] + SKILL_GROWTH_FIELDS

#: Result Reproduction Readiness Audit（Figure 2）TASK2 新增：逐 worker 的成功率
#: 明细表，关联 backbone/harness（与 TASK1 写入 experiment_summary.json 的
#: "workers" 块同源），不重新计算任何 success_rate（直接读 round JSON 里
#: 已经算好的 per_worker[worker_id]["success_rate"]）。family_id 在扫平/
#: family-loop 两种模式下都固定包含（扫平模式下为空字符串），便于两种
#: 模式共用同一列顺序。
SUCCESS_RATE_DETAIL_FIELDS = ["family_id", "round_idx", "worker_id", "backbone", "harness", "success_rate"]

#: family-loop 模式下，四张明细 CSV 在原列前插入 "family_id" 列
FAMILY_METRICS_FIELDS = ["family_id"] + METRICS_FIELDS
FAMILY_CAPABILITY_FIELDS = ["family_id"] + CAPABILITY_FIELDS
FAMILY_PRIVACY_FIELDS = ["family_id"] + PRIVACY_FIELDS
FAMILY_COST_FIELDS = ["family_id"] + COST_FIELDS
TASK_METRICS_FIELDS = [
    "family_id", "completed_tasks", "failed_tasks", "checkpointed_tasks",
    "total_tasks", "success_rate",
]

#: 对应论文 Table 1（每个 family 一行的最终成功率汇总）
#: TASK4 修正：新增 "setting" 列（标识本次结果来自哪个 setting/配置），并在
#: 末尾追加一行 family_id="__MACRO_AVG__" 的汇总行，给出
#: macro_avg_success_rate（仅对论文 20 个官方 family 取平均，见
#: export_family_loop_csvs() 内的计算逻辑）——不新造指标，只是把
#: run_experiment.py 里已经算过的“各 family 平均成功率”导出到 table1.csv。
TABLE1_FIELDS = [
    "family_id", "setting", "is_paper_family", "n_rounds",
    "final_success_rate", "final_mean_library_size", "final_weighted_global_score",
]

#: 本项目自建的 5 个 legacy family（非官方 SkillFlow 数据集内容），
#: 详见 benchmark/family.py 模块 docstring；论文只有 20 个真实 family。
LEGACY_ENGINEERING_FAMILY_IDS = {
    "data_cleaning", "data_transformation", "document_processing",
    "financial_analysis", "report_generation",
}


def _load_round_jsons(setting_dir: Path) -> list[dict]:
    """按 round_idx 排序加载一个 setting 目录下所有 round_*_summary.json。"""
    records = []
    for p in sorted(setting_dir.glob("round_*_summary.json")):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    records.sort(key=lambda r: r.get("round_idx", 0))
    return records


def _load_family_round_jsons(setting_dir: Path) -> dict[str, list[dict]]:
    """
    按 family_id 排序加载 <setting_dir>/families/<family_id>/round_*_summary.json。

    对应 experiments/run_experiment.py::_run_family_loop() 的输出布局。
    """
    families_dir = setting_dir / "families"
    result: dict[str, list[dict]] = {}
    if not families_dir.is_dir():
        return result
    for family_subdir in sorted(families_dir.iterdir()):
        if family_subdir.is_dir():
            result[family_subdir.name] = _load_round_jsons(family_subdir)
    return result


def _write_csv(rows: list[dict], path: Path, fieldnames: list[str] | None = None) -> Path:
    """写 CSV；rows 为空但提供 fieldnames 时，仍写出只含表头的占位文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not fieldnames:
        path.write_text("", encoding="utf-8")
        return path
    used_fieldnames = fieldnames or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=used_fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _load_worker_lookup(setting_dir: Path) -> dict[str, dict]:
    """
    读取 <setting_dir>/experiment_summary.json 的 "workers" 块（Result
    Reproduction Readiness Audit TASK1 新增），构建
    worker_id -> {"backbone":..., "harness":...} 查找表，供
    success_rate_detail.csv（TASK2）关联 backbone/harness 列。

    旧版 experiment_summary.json（TASK1 之前产出，没有 "workers" 键）或
    文件不存在时返回空字典，对应列回退为空字符串，不报错、不猜测。
    """
    summary_path = setting_dir / "experiment_summary.json"
    if not summary_path.is_file():
        return {}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    workers = summary.get("workers") or {}
    return {
        wid: {"backbone": meta.get("backbone_model", ""), "harness": meta.get("agent_harness", "")}
        for wid, meta in workers.items()
    }


def _rows_from_record(rec: dict) -> tuple[dict, dict, dict, dict]:
    """
    把单个 round_*_summary.json 记录拆成 (metrics_row, capability_row,
    privacy_row, cost_row) 四个字典（均不含 "round_idx" 以外的分组键，
    如 family_id 由调用方按需追加）。

    抽取自旧版 export_setting_csvs() 主循环体，供扁平模式与 family-loop
    模式共用，避免两处 schema 漂移。
    """
    round_idx = rec.get("round_idx", 0)
    metrics = rec.get("metrics", {}) or {}
    snapshots = rec.get("snapshots", []) or []

    metrics_row = {
        "round_idx": round_idx,
        "success_rate": metrics.get("success_rate", 0.0),
        # 论文 Eq.(3)：若 round JSON 来自旧版运行（未经过新版 ExperimentEvaluator），
        # 该键不存在，回退为空字符串而不是伪造 0.0
        "weighted_global_score": metrics.get("weighted_global_score", ""),
        "mean_library_size": metrics.get("mean_library_size", 0.0),
        "mean_skill_growth": metrics.get("mean_skill_growth", 0.0),
        "n_solved": metrics.get("n_solved", 0.0),
        "n_total": metrics.get("n_total", 0.0),
    }

    if snapshots:
        mean_before = sum(s.get("library_size_before", 0) for s in snapshots) / len(snapshots)
        mean_after = sum(s.get("library_size_after", 0) for s in snapshots) / len(snapshots)
    else:
        mean_before = mean_after = 0.0

    cap_summary = rec.get("capability_summary")
    if cap_summary:
        capability_row = {
            "round_idx": round_idx,
            "mean_library_size_before": mean_before,
            "mean_library_size_after": mean_after,
            "mean_skill_growth": metrics.get("mean_skill_growth", 0.0),
            "covered": cap_summary.get("covered", 0),
            "absorbing": cap_summary.get("absorbing", 0),
            "broken": cap_summary.get("broken", 0),
            "gap": cap_summary.get("gap", 0),
            "coverage_ratio": cap_summary.get("coverage_ratio", 0.0),
            "capability_data_source": "real_capability_matrix",
        }
    else:
        capability_row = {
            "round_idx": round_idx,
            "mean_library_size_before": mean_before,
            "mean_library_size_after": mean_after,
            "mean_skill_growth": metrics.get("mean_skill_growth", 0.0),
            "covered": "", "absorbing": "", "broken": "", "gap": "", "coverage_ratio": "",
            "capability_data_source": "library_size_proxy",
        }

    selr_values = [s["selr"] for s in snapshots if s.get("selr") is not None]
    if selr_values:
        privacy_row = {
            "round_idx": round_idx,
            "compression_ratio": metrics.get("compression_ratio", 0.0),
            "privacy_gain_proxy": metrics.get("privacy_gain", 0.0),
            "mean_selr": sum(selr_values) / len(selr_values),
            "privacy_data_source": "text_selr",
        }
    else:
        privacy_row = {
            "round_idx": round_idx,
            "compression_ratio": metrics.get("compression_ratio", 0.0),
            "privacy_gain_proxy": metrics.get("privacy_gain", 0.0),
            "mean_selr": "",
            "privacy_data_source": "token_proxy_only",
        }

    cost_row = {
        "round_idx": round_idx,
        "total_cost_usd": metrics.get("total_cost_usd", 0.0),
        "cost_per_solved_task": metrics.get("cost_per_solved_task", 0.0),
        # TASK4 新增：旧版 round JSON 没有这五个键时回退为 ""（不伪造 0.0）。
        "client_cost_usd": metrics.get("client_cost_usd", ""),
        "server_cost_usd": metrics.get("server_cost_usd", ""),
        "total_cost_usd_unified": metrics.get("total_cost_usd_unified", ""),
        "communication_bytes": metrics.get("communication_bytes", ""),
        "communication_trajectory_bytes": metrics.get("communication_trajectory_bytes", ""),
        # Result Reproduction Readiness Audit TASK3 新增：四组件明细直接来自
        # experiments/federated.py::run() 里写入 round_result.metrics 的同名键
        # （由 CostAccountant.total_by_component() 算出，不在这里重新计算）；
        # total_cost_unified 是 total_cost_usd_unified 的同值别名列，满足
        # TASK3 要求的确切列名，不是第二套成本口径。
        "client_execution_cost": metrics.get("client_execution_cost", ""),
        "patch_distill_cost": metrics.get("patch_distill_cost", ""),
        "stage1_cost": metrics.get("stage1_cost", ""),
        "stage2_cost": metrics.get("stage2_cost", ""),
        "total_cost_unified": metrics.get("total_cost_usd_unified", ""),
    }

    return metrics_row, capability_row, privacy_row, cost_row


def _success_rate_detail_rows_from_record(rec: dict, worker_lookup: dict[str, dict]) -> list[dict]:
    """
    Result Reproduction Readiness Audit TASK2（Figure 2 client-level success
    export）：把 round_*_summary.json 里已经算好的
    per_worker[worker_id]["success_rate"] 逐 worker 展开成一行，不重新计算
    任何评估逻辑（复用 evaluation/evaluator.py::ExperimentEvaluator.record_round()
    早已算出的 per_worker 字典）。backbone/harness 来自调用方传入的
    worker_lookup（见 _load_worker_lookup()），找不到对应 worker_id 时回退
    为空字符串。
    """
    round_idx = rec.get("round_idx", 0)
    per_worker = rec.get("per_worker", {}) or {}
    rows = []
    for worker_id, wmetrics in per_worker.items():
        lookup = worker_lookup.get(worker_id, {})
        rows.append({
            "round_idx": round_idx,
            "worker_id": worker_id,
            "backbone": lookup.get("backbone", ""),
            "harness": lookup.get("harness", ""),
            "success_rate": (wmetrics or {}).get("success_rate", 0.0),
        })
    return rows


def _warn_if_legacy_cost_incomplete(cost_rows: list[dict]) -> None:
    """
    Result Reproduction Readiness Audit TASK3：若本次导出出现
    total_cost_usd_unified（四组件统一总成本）与旧列 total_cost_usd
    （只计 client_execution 一项）不一致的行，说明旧列系统性漏计了
    patch_distiller/stage1_planner/stage2_merge 三个环节的真实 LLM 调用
    成本，只打一次 WARNING 提示，不删除/不修改任何已有列的数值。
    """
    for row in cost_rows:
        unified = row.get("total_cost_usd_unified", "")
        legacy = row.get("total_cost_usd", "")
        if unified not in ("", None) and legacy not in ("", None) and unified != legacy:
            logger.warning(
                "cost.csv 的旧列 total_cost_usd（仅计 client_execution）与新列 "
                "total_cost_usd_unified / total_cost_unified"
                "（client_execution+patch_distiller+stage1_planner+stage2_merge "
                "四组件统一总成本）不一致（round_idx=%s: %s vs %s）。做 Appendix C "
                "成本复现分析时请使用 total_cost_unified，total_cost_usd 已废弃。",
                row.get("round_idx"), legacy, unified,
            )
            return


def _skill_growth_rows_from_record(rec: dict) -> list[dict]:
    """
    把单个 round_*_summary.json 记录里的 snapshots 列表，逐 worker 展开成
    [{"worker_id":..., "round_idx":..., "number_of_skills":...}, ...]（不做
    跨 worker 平均，与 _rows_from_record() 里 capability_row 的
    mean_library_size_after 是两条不同粒度的导出路径，互不影响）。

    number_of_skills 取自 snapshots[i]["library_size_after"]（本轮结束后该
    worker 的技能库文件数，见 experiments/federated.py 里
    `lib_after = client.library.snapshot(round_idx).skill_count`）。
    """
    round_idx = rec.get("round_idx", 0)
    snapshots = rec.get("snapshots", []) or []
    rows = []
    for s in snapshots:
        worker_id = s.get("worker_id")
        if worker_id is None:
            continue
        rows.append({
            "worker_id": worker_id,
            "round_idx": round_idx,
            "number_of_skills": s.get("library_size_after", 0),
        })
    return rows


def export_setting_csvs(setting_dir: str | Path) -> dict[str, Path]:
    """
    为一个 setting 目录生成 metrics.csv / capability.csv / privacy.csv / cost.csv。

    若检测到 <setting_dir>/experiment_summary.json 且其 "mode" 字段为
    "family_loop"（experiments/run_experiment.py::_run_family_loop() 产出），
    自动转发给 export_family_loop_csvs()（按 family 循环时 round JSON 存放在
    <setting_dir>/families/<family_id>/ 子目录下，见该函数说明）。

    Args:
        setting_dir: 单个 setting 的输出目录（含 run_experiment.py 写出的
            round_*_summary.json），如 results/setting1/

    Returns:
        {"metrics": Path, "capability": Path, "privacy": Path, "cost": Path}
        （family-loop 模式下额外含 "table1": Path）
    """
    setting_dir = Path(setting_dir)

    summary_path = setting_dir / "experiment_summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            summary = {}
        if summary.get("mode") == "family_loop":
            return export_family_loop_csvs(setting_dir)

    records = _load_round_jsons(setting_dir)
    worker_lookup = _load_worker_lookup(setting_dir)

    metrics_rows: list[dict] = []
    capability_rows: list[dict] = []
    privacy_rows: list[dict] = []
    cost_rows: list[dict] = []
    skill_growth_rows: list[dict] = []
    success_rate_detail_rows: list[dict] = []

    for rec in records:
        metrics_row, capability_row, privacy_row, cost_row = _rows_from_record(rec)
        metrics_rows.append(metrics_row)
        capability_rows.append(capability_row)
        privacy_rows.append(privacy_row)
        cost_rows.append(cost_row)
        skill_growth_rows.extend(_skill_growth_rows_from_record(rec))
        for sr_row in _success_rate_detail_rows_from_record(rec, worker_lookup):
            success_rate_detail_rows.append({"family_id": "", **sr_row})

    _warn_if_legacy_cost_incomplete(cost_rows)

    # Result Reproduction Readiness Audit TASK4：skill_evolution.csv（扩平
    # 模式下从 <setting_dir>/evolution_trace.jsonl 派生，文件不存在
    # （如 Setting1 SE 基线没有 AuditTraceRecorder）时 load_trace_jsonl()
    # 返回空列表，导出只带表头的占位 CSV。
    trace_records = load_trace_jsonl(setting_dir / "evolution_trace.jsonl")
    skill_evolution_rows = build_skill_evolution_rows(trace_records, family_id=None)

    return {
        "metrics": _write_csv(metrics_rows, setting_dir / "metrics.csv", METRICS_FIELDS),
        "capability": _write_csv(capability_rows, setting_dir / "capability.csv", CAPABILITY_FIELDS),
        "privacy": _write_csv(privacy_rows, setting_dir / "privacy.csv", PRIVACY_FIELDS),
        "cost": _write_csv(cost_rows, setting_dir / "cost.csv", COST_FIELDS),
        "skill_growth": _write_csv(skill_growth_rows, setting_dir / "skill_growth.csv", SKILL_GROWTH_FIELDS),
        "success_rate_detail": _write_csv(
            success_rate_detail_rows, setting_dir / "success_rate_detail.csv", SUCCESS_RATE_DETAIL_FIELDS
        ),
        "skill_evolution": _write_csv(
            skill_evolution_rows, setting_dir / "skill_evolution.csv", FAMILY_SKILL_EVOLUTION_FIELDS
        ),
    }


def export_family_loop_csvs(setting_dir: str | Path, setting: str | None = None) -> dict[str, Path]:
    """
    为按 family 循环产出的 setting 目录生成五张 CSV（metrics/capability/
    privacy/cost 各加 "family_id" 列 + table1.csv 汇总）。

    Args:
        setting_dir: 含 <setting_dir>/families/<family_id>/round_*_summary.json
            和 <setting_dir>/experiment_summary.json 的 setting 目录。
        setting: 本次结果对应的 setting 名称（写入 table1.csv 的 "setting" 列，
            TASK4 要求）；未显式传入时默认取 setting_dir 的目录名。

    Returns:
        {"metrics": Path, "capability": Path, "privacy": Path, "cost": Path,
         "table1": Path}
        table1.csv 末尾会追加一行 family_id="__MACRO_AVG__" 的汇总行，给出
        macro_avg_success_rate（对 is_paper_family=True 的 family 取算术平均，
        与 experiment_summary.json 里 mean_success_rate 的口径一致）。
    """
    setting_dir = Path(setting_dir)
    setting_name = setting if setting is not None else setting_dir.name
    family_records = _load_family_round_jsons(setting_dir)
    worker_lookup = _load_worker_lookup(setting_dir)
    summary_path = setting_dir / "experiment_summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    task_metrics_by_family = summary.get("task_metrics_by_family", {})

    metrics_rows: list[dict] = []
    capability_rows: list[dict] = []
    privacy_rows: list[dict] = []
    cost_rows: list[dict] = []
    table1_rows: list[dict] = []
    skill_growth_rows: list[dict] = []
    success_rate_detail_rows: list[dict] = []
    skill_evolution_rows: list[dict] = []
    task_metric_rows: list[dict] = []

    family_ids = set(family_records) | set(task_metrics_by_family)
    for family_id in sorted(family_ids):
        records = family_records.get(family_id, [])
        is_paper_family = family_id not in LEGACY_ENGINEERING_FAMILY_IDS
        task_metrics = task_metrics_by_family.get(family_id, {})
        task_success_rate = task_metrics.get("success_rate")
        if task_metrics:
            task_metric_rows.append({"family_id": family_id, **task_metrics})

        last_metrics_row: dict | None = None
        for rec in records:
            metrics_row, capability_row, privacy_row, cost_row = _rows_from_record(rec)
            metrics_rows.append({"family_id": family_id, **metrics_row})
            capability_rows.append({"family_id": family_id, **capability_row})
            privacy_rows.append({"family_id": family_id, **privacy_row})
            cost_rows.append({"family_id": family_id, **cost_row})
            for sg_row in _skill_growth_rows_from_record(rec):
                skill_growth_rows.append({"family_id": family_id, **sg_row})
            for sr_row in _success_rate_detail_rows_from_record(rec, worker_lookup):
                success_rate_detail_rows.append({"family_id": family_id, **sr_row})
            last_metrics_row = metrics_row

        # Result Reproduction Readiness Audit TASK4：每个 family 独立一份
        # evolution_trace.jsonl（见 experiments/run_experiment.py::_run_family_loop()
        # 每个 family 独立 AuditTraceRecorder），逐 family 派生后按 family_id
        # 汇总，不跨 family 混淆存活技能集合。
        family_trace_records = load_trace_jsonl(setting_dir / "families" / family_id / "evolution_trace.jsonl")
        skill_evolution_rows.extend(build_skill_evolution_rows(family_trace_records, family_id=family_id))

        if last_metrics_row is not None:
            table1_rows.append({
                "family_id": family_id,
                "setting": setting_name,
                "is_paper_family": is_paper_family,
                "n_rounds": len(records),
                "final_success_rate": (
                    task_success_rate
                    if task_success_rate is not None
                    else last_metrics_row["success_rate"]
                ),
                "final_mean_library_size": last_metrics_row["mean_library_size"],
                "final_weighted_global_score": last_metrics_row["weighted_global_score"],
            })
        else:
            table1_rows.append({
                "family_id": family_id,
                "setting": setting_name,
                "is_paper_family": is_paper_family,
                "n_rounds": int(task_metrics.get("checkpointed_tasks", 0)),
                "final_success_rate": (
                    task_success_rate if task_success_rate is not None else ""
                ),
                "final_mean_library_size": "",
                "final_weighted_global_score": "",
            })

    # TASK4：追加 macro_avg_success_rate 汇总行——只对论文官方 family
    # （is_paper_family=True 且 final_success_rate 非空）取算术平均，
    # 口径与 experiments/run_experiment.py::_save_family_loop_summary()
    # 里的 mean_success_rate 一致，不是新指标，只是把已有的平均值也写进
    # table1.csv 方便直接对照论文 Table 1。
    macro_avg_success_rate = summary.get(
        "success_rate", summary.get("mean_success_rate", "")
    )
    table1_rows.append({
        "family_id": "__MACRO_AVG__",
        "setting": setting_name,
        "is_paper_family": "",
        "n_rounds": "",
        "final_success_rate": macro_avg_success_rate,
        "final_mean_library_size": "",
        "final_weighted_global_score": "",
    })
    task_metric_rows.append({
        "family_id": "__TOTAL__",
        "completed_tasks": summary.get("completed_tasks", 0),
        "failed_tasks": sum(
            int(item.get("failed_tasks", 0)) for item in task_metrics_by_family.values()
        ),
        "checkpointed_tasks": sum(
            int(item.get("checkpointed_tasks", 0)) for item in task_metrics_by_family.values()
        ),
        "total_tasks": summary.get("total_tasks", 0),
        "success_rate": summary.get("success_rate", summary.get("mean_success_rate", 0.0)),
    })

    _warn_if_legacy_cost_incomplete(cost_rows)

    return {
        "metrics": _write_csv(metrics_rows, setting_dir / "metrics.csv", FAMILY_METRICS_FIELDS),
        "capability": _write_csv(capability_rows, setting_dir / "capability.csv", FAMILY_CAPABILITY_FIELDS),
        "privacy": _write_csv(privacy_rows, setting_dir / "privacy.csv", FAMILY_PRIVACY_FIELDS),
        "cost": _write_csv(cost_rows, setting_dir / "cost.csv", FAMILY_COST_FIELDS),
        "table1": _write_csv(table1_rows, setting_dir / "table1.csv", TABLE1_FIELDS),
        "task_metrics": _write_csv(
            task_metric_rows, setting_dir / "task_metrics.csv", TASK_METRICS_FIELDS
        ),
        "skill_growth": _write_csv(
            skill_growth_rows, setting_dir / "skill_growth.csv", FAMILY_SKILL_GROWTH_FIELDS
        ),
        "success_rate_detail": _write_csv(
            success_rate_detail_rows, setting_dir / "success_rate_detail.csv", SUCCESS_RATE_DETAIL_FIELDS
        ),
        "skill_evolution": _write_csv(
            skill_evolution_rows, setting_dir / "skill_evolution.csv", FAMILY_SKILL_EVOLUTION_FIELDS
        ),
    }


def scaffold_all_settings(
    results_root: str | Path = "results", settings: tuple[int, ...] = (1, 2, 3, 4),
) -> dict[int, dict[str, Path]]:
    """
    生成论文实验输出目录骨架：results/setting1/ .. setting4/，每个目录下先
    放置只含表头的占位 CSV（metrics.csv/capability.csv/privacy.csv/cost.csv）。

    真实实验数据由 run.py（Phase14 任务5）调用 export_setting_csvs() 在
    run_experiment.py 产出 round_*_summary.json 之后覆盖写入；本函数只
    负责"先把目录结构和列名占位建好"，不产生任何虚构的实验数值。

    Returns:
        {setting_num: {"metrics": Path, ...}}
    """
    results_root = Path(results_root)
    out: dict[int, dict[str, Path]] = {}
    for n in settings:
        setting_dir = results_root / f"setting{n}"
        setting_dir.mkdir(parents=True, exist_ok=True)
        out[n] = {
            "metrics": _write_csv([], setting_dir / "metrics.csv", METRICS_FIELDS),
            "capability": _write_csv([], setting_dir / "capability.csv", CAPABILITY_FIELDS),
            "privacy": _write_csv([], setting_dir / "privacy.csv", PRIVACY_FIELDS),
            "cost": _write_csv([], setting_dir / "cost.csv", COST_FIELDS),
            "skill_growth": _write_csv([], setting_dir / "skill_growth.csv", SKILL_GROWTH_FIELDS),
        }
    return out
