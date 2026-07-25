# Phase 7 Metric Dataflow

Object audited: `evaluation.metrics`, `evaluation.evaluator`, `experiments.task_checkpoint`, `experiments.aggregation`, `evaluation.paper_export`, `evaluation.reporter`.

## Metric Source Chain

```text
Verifier reward
  -> Trajectory.reward
  -> TrialSnapshot.reward
  -> ExperimentEvaluator.record_round
  -> round_XX_summary.json
  -> _save_family_loop_summary / experiment_summary.json
  -> paper_export CSV / reporter tables / aggregation
```

## Per-Metric Trace

| Metric | Immediate variable | Producer | JSON/CSV output | Notes |
|---|---|---|---|---|
| Success Rate (round) | `TrialSnapshot.reward` | `FederatedMetrics.success_rate()` via `ExperimentEvaluator.record_round()` | `round_XX_summary.json.metrics.success_rate`, `metrics.csv.success_rate` | Per round and per worker. |
| Success Rate (family) | `task_status.json.status == completed` | `collect_task_checkpoint_stats()` | `experiment_summary.json.task_metrics_by_family[family].success_rate` | Covers whole family, preferred by `aggregation.py`. |
| Overall success | all snapshots | `ExperimentEvaluator.finalize()` | `ExperimentResult.final_metrics.overall_success_rate` | For non-family-loop final result. |
| Capability Improvement | `CapabilityTracker` snapshots | `FederatedRunner._record_capability_matrix()` | `capability_matrix.jsonl`, `capability_evolution.csv`, `capability.csv` | Federated settings only. Setting1 uses library-size proxy in CSV. |
| Library Size | `SkillLibrary.snapshot().skill_count` | runner before/after apply | `round_XX_summary.json.snapshots[].library_size_after`, `families[family].library_sizes`, `skill_growth.csv` | Real per-worker values are in snapshots. |
| Skill Growth | `library_size_after - before` | `FederatedMetrics.skill_growth()` / `paper_export` | `metrics.csv.mean_skill_growth`, `capability.csv.mean_skill_growth`, `skill_growth.csv` | CSV supports mean and per-worker rows. |
| Compression Ratio | `patch_tokens`, `trajectory_tokens` | `FederatedMetrics.compression_ratio()` | `round_XX_summary.json.metrics.compression_ratio`, `privacy.csv` | Token proxy, not direct byte transmission. |
| Privacy Gain | `trajectory_tokens`, `patch_tokens` | `FederatedMetrics.privacy_gain()` | `privacy.csv.privacy_gain_proxy` | Same numeric formula as compression proxy. |
| SELR | trajectory text, patch text | `evaluation.selr.compute_selr_from_texts()` in runners | `snapshots[].selr`, `privacy.csv.mean_selr` | Approximation, not official semantic judge. |
| Cost | `Trajectory.cost_usd`, `BackboneCallResult.cost_usd` | `CostAccountant` + snapshot cost | `cost_ledger.jsonl`, `round metrics total_cost_usd`, `cost.csv` | Federated has component breakdown: client execution, distill, stage1, stage2. |
| Communication bytes | serialized patch/library snapshot | `CommunicationAuditor` | `communication_audit.jsonl`, `cost.csv.communication_bytes` | Federated only when auditor attached. |
| Paper Table 1 family row | final per-family result | `paper_export.export_family_loop_csvs()` | `table1.csv` | Post-run export step, not automatic in family-loop. |
| Paper Table 1/2 cross-setting | multiple experiment summaries | `ResultReporter.export_paper_table1/2()` | user-chosen CSV path | Requires caller to supply setting dirs. |

## JSON Sources

- Per round: `<family_output_dir>/round_XX_summary.json`
- Per task checkpoint: `<family_output_dir>/workers/<worker>/tasks/round_XXX_<task>/{trajectory,reward,patch,task_status}.json`
- Per family-loop summary: `<output_dir>/experiment_summary.json`
- Single-family materialized summary mirror: `<run_root>/<family>/metrics/experiment_summary.json`

## CSV Generation Status

| CSV / table | Function | Automatic? | Status |
|---|---|---:|---|
| `metrics.csv` | `paper_export.export_setting_csvs/export_family_loop_csvs` | No, post-step | Implemented |
| `privacy.csv` | same | No | Implemented |
| `cost.csv` | same | No | Implemented |
| `capability.csv` | same | No | Implemented |
| `skill_growth.csv` | same | No | Implemented |
| `success_rate_detail.csv` | same | No | Implemented |
| `table1.csv` per setting | `paper_export.export_family_loop_csvs` | No | Implemented |
| cross-setting Table1/2 | `ResultReporter.export_paper_table1/2` | No | Implemented |
| official comparison | `scripts/compare_with_paper.py` | No | Implemented |

## Metric Consistency Risks

| Risk | Severity | Explanation |
|---|---|---|
| Family-loop top-level SR differs from final round SR | Low/Expected | Family summary uses task checkpoint stats across whole family; final round SR is only last round. |
| Cost can be zero in API path if provider/litellm returns no cost | Medium | Tokens may exist while `cost_usd` remains 0 depending on provider cost metadata. Cost ledger should be inspected. |
| Paper CSVs are not auto-generated after run | Medium | JSON contains data; user must run exporter/reporter to produce CSVs. |
| SELR is approximate | Medium | Uses regex/text scan, not official semantic judge. |

## Verdict

The reward-to-metric dataflow is connected and auditable. The main operational gap is that paper CSV generation is a separate post-processing step; the run itself produces JSON/checkpoint artifacts, not all paper tables automatically.
