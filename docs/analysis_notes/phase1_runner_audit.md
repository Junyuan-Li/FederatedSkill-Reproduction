# Phase 1 Runner Audit

Scope: `experiments/run_experiment.py`, `experiments/baseline.py`, `experiments/federated.py`.

## Expected Paper Order

Paper-level order requested by the audit:

`family -> round -> worker -> task -> evaluation -> aggregation -> next round`

Current project order in family-loop mode:

1. `run_experiment()` or `run_family_experiment()` loads `.env`, YAML config, families.
2. `_apply_paper_benchmark_scope()` filters to paper families when `paper_benchmark_only: true`.
3. `_run_family_loop()` iterates `for family_id in sorted(families.keys())`.
4. For each family, creates a fresh `FamilyCurriculumSampler({family_id: family})` and assigns every worker to the same family.
5. For each round, `SelfEvolutionRunner._run_round()` or `FederatedRunner._run_round()` samples tasks by `round_idx`.
6. Each worker executes one task and produces `Trajectory`.
7. Runner distills `WorkerPatch`.
8. SE: applies patch locally. Federated: calls `FederatedServer.run_round()`, then applies `MergedPatch` locally.
9. Runner calls `ExperimentEvaluator.record_round()`.
10. `_save_results()` writes per-family round summaries.
11. After all families, `_save_family_loop_summary()` writes `experiment_summary.json`.

## Confirmed Protocol Alignment

| Check | Evidence | Status |
|---|---|---|
| Family is outer unit | `_run_family_loop()` loops `for idx, family_id` and constructs `FamilyCurriculumSampler({family_id: family})` | Confirmed |
| Family library starts empty | per-family `family_output_dir/libraries/<worker>` plus state-leak assertions before use | Confirmed |
| Family task order preserved | `TaskFamily.tasks` sorted by difficulty; `FamilyCurriculumSampler.sample()` uses `round_idx + 1` | Confirmed |
| No silent truncation by default | `rounds_per_family_mode='family_length'` uses `len(family.tasks)` | Confirmed |
| Per-task checkpoint | `TaskCheckpointStore.save_trajectory_reward()` immediately writes trajectory/reward | Confirmed |
| Failed family does not erase task logs | `family_failure_cleanup()` cleans library but preserves workers/tasks logs | Confirmed |
| Aggregation happens after loop | `_save_family_loop_summary()` called after family loop | Confirmed |
| Failed family counted | `failed_families` written; `task_checkpoint_stats` still collected | Mostly confirmed |

## Early Return / Skip Risks

| Risk | Location | Assessment |
|---|---|---|
| `dry_run=True` returns before execution | `run_experiment`, `run_family_experiment`, batch wrapper | Intended, not a bug |
| `execution_mode='api'` default bypasses CLI harness | `_build_executor()` returns `VerificationAwareExecutor` unless `cli` | Major paper-fidelity gap for official harness behavior |
| Stage1 planner fallback on LLM failure | `EvolutionPlanner.plan()` returns fallback plan with no directives on `LLMCallError` / parse error | Real run continues, but federation evolution may silently degrade to no directives except logs |
| Stage2 LLM failure returns empty merged patch | `EvolutionExecutor.execute_for_worker()` catches `LLMCallError` and returns empty patch | Real run continues, but merge/evolution signal is lost for that worker/round |
| PatchDistiller strict mode can abort family | `PatchDistiller` raises `PatchDistillationFailure`; runner strict mode re-raises | Intended integrity behavior; family marked failed in loop |
| `paper_export.py` not automatically called after family-loop run | `run_experiment()` prints result and returns; exporter is skipped in family-loop plot path | Metrics JSON exists, paper CSV generation is a post-step |

## State-Leak Checks

Confirmed guards:

- `run_family_experiment()` uses fresh `experiment_id` and `_assert_fresh_experiment_dir()`.
- `_preflight_family_run_checks()` checks library/trajectory/metrics materialized directories before single-family run.
- `_run_family_loop()` asserts sampler contains only the current family.
- `_run_family_loop()` asserts worker library root is empty before starting a family.
- Federated server is recreated per family; capability and memory are asserted initially empty.

## Phase 1 Verdict

Runner/family protocol is mostly aligned with paper Section 5 family-level independent evaluation. The main execution-fidelity caveat is that the default task executor is API-compatible, not CLI-harness-compatible. For official-like agent behavior, run with `--execution-mode cli`; otherwise the path is `LLMBackbone.call -> solution.py`, not `claude/qwen/kimi` subprocess agent.
