# Phase 6 Federation Chain

Object audited: `experiments.federated.FederatedRunner`, `server.evolution.FederatedServer`, `server.planner.EvolutionPlanner`, `server.merge.EvolutionExecutor`, memory/capability/trace recorders.

## Settings Covered

Federation starts when config has `federated: true` and `run_experiment.py` constructs:

- worker `FederatedClient` objects
- server backbone via `_build_backbone(server_cfg, role='server')`
- `FederatedServer.create()`
- `FederatedRunner(...)`

This applies to Setting 2-4, not Setting 1 SE.

## Real Federated Round Order

```text
FederatedRunner._run_round(round_idx)
  -> sampler.sample_batch(worker_ids, round_idx)
  -> for each client:
       _run_client_phase_with_retry
         -> executor.run(task, library, profile, round_idx)
         -> TaskCheckpointStore.save_trajectory_reward
         -> client.distill_patch(trajectory)
         -> TaskCheckpointStore.save_success
       collect WorkerPatch and LibrarySnapshot
  -> server.run_round(round_idx, patches, library_snapshots, task_assignments)
       -> _build_library_digests(library_snapshots)
       -> capability.init_from_patches(patches, round_idx)
       -> planner.plan(... patches, digests, capability, memory ...)
       -> for each worker:
            _execute_worker_directives(... all directives ...)
              -> merge.execute_for_worker(...)
              -> Stage2 LLM call if directive exists and not NO_UPDATE
       -> RoundRecord appended
  -> client.apply_update(merged_patch)
  -> build TrialSnapshot
  -> evaluator.record_round
```

## Planner

`EvolutionPlanner.plan()` receives:

- `round_idx`
- `family_name`
- `{worker_id: WorkerPatch}`
- `{worker_id: [LibraryDigest]}`
- `CapabilityTracker`
- `EvolutionMemoryStore`
- `{worker_id: WorkerProfile}`

It builds Stage1 prompt using `Stage1PromptBuilder`, calls server LLM through `LLMBackbone.call_json`, parses:

- capability matrix
- high-level memory
- low-level memories
- directives

Fallback behavior: if LLM call or parse fails, it returns a fallback plan with no directives. That keeps experiment running but weakens federation behavior for that round.

## Merge / Evolution

`FederatedServer._execute_worker_directives()` executes **all** directives for each worker. For each directive:

- calls `EvolutionExecutor.execute_for_worker()`
- supplies current snapshot, peer patches, peer profiles, peer library digests, capability tracker, memory store
- gets `MergedPatch` + `DecisionLog`
- applies each directive result to an in-memory working snapshot before processing the next directive
- combines all directive outputs into one final `MergedPatch` per worker

`EvolutionExecutor.execute_for_worker()`:

- returns empty patch when directive is missing or `NO_UPDATE`
- otherwise builds Stage2 prompt and calls server LLM
- parses `upsert_files/delete_paths/decision_log/updated_low_level_memory`
- writes decision/audit/fusion/transfer/memory traces when recorders are attached
- returns `MergedPatch` and `DecisionLog`

## Memory

Confirmed:

- `EvolutionMemoryStore` is created per family in `FederatedServer.create()`.
- Stage1 reads high-level memory and applies plan memory updates.
- Stage2 reads low-level worker memory and can update it via `updated_low_level_memory`.
- `FederatedRunner.run()` flushes `DECISIONS.md`, `memory.md`, memory access traces when output directory is set.

## Capability

Confirmed:

- `CapabilityTracker` is created per family.
- `init_from_patches()` runs each server round.
- Planner can update from plan dict.
- `FederatedRunner._record_capability_matrix()` records capability history and matrix JSONL.
- Capability evolution CSV export is wired at run end.

## Aggregation

Federated round aggregation uses:

- per-round `TrialSnapshot` list -> `ExperimentEvaluator.record_round()`
- family-loop top-level `task_metrics_by_family` -> `collect_task_checkpoint_stats()`
- post-run cross-family aggregation -> `experiments.aggregation.aggregate()`

## Main Federation Risks

| Risk | Severity | Explanation |
|---|---|---|
| Stage1 fallback silently yields no directives | Medium | Failure is logged and trace can mark fallback, but federated behavior degrades to no evolution. |
| Stage2 LLM failure returns empty patch | Medium | Avoids crash, but can hide merge failure in metrics unless trace/cost logs are inspected. |
| Default task execution is API mode | High for official fidelity | Federation logic can run, but worker agent harness is not official CLI unless `--execution-mode cli`. |
| Official sync schedule differs | Medium | Official runner has `sync_schedule.should_sync`; current code runs server Stage1/Stage2 every round in federated settings. |

## Verdict

Federation chain is genuinely called in Settings 2-4 and includes worker execution, server planning, merge, memory, capability, and client apply. It is a compatible reimplementation, not a direct official implementation. Its main paper-fidelity gaps are execution harness default (`api`) and sync schedule differences from official `FedRunner`.
