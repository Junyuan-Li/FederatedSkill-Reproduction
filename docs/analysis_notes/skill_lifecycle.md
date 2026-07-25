# Phase 5 Skill Lifecycle

Object audited: `client.library.SkillLibrary`, `client.federated_client.FederatedClient`, `experiments.baseline`, `experiments.federated`, `client.executor.TaskExecutor` skill retrieval helpers.

## Lifecycle in Setting 1 (SE)

```text
Trajectory -> PatchDistiller -> WorkerPatch
  -> SelfEvolutionRunner._run_client_trial_attempt
  -> client.library.apply_patch(patch)
  -> next round client.library.snapshot/digest/retrieve
```

Confirmed behavior:

1. Before task execution, runner records `lib_before = client.library.snapshot(round_idx).skill_count`.
2. Task executor retrieves skills through `TaskExecutor._retrieve_skills(task, library)`.
3. Distiller snapshots current library via `library.snapshot(round_idx)`.
4. After distillation, `client.library.apply_patch(patch)` writes/deletes files.
5. Next round uses the same `FederatedClient` and same `SkillLibrary` root for that family, so saved skills are visible to retrieval.

## Lifecycle in Settings 2-4 (Federated)

```text
Trajectory -> PatchDistiller -> WorkerPatch
  -> server.run_round(patches, library_snapshots)
  -> EvolutionPlanner.plan(... library_digests ...)
  -> EvolutionExecutor.execute_for_worker(... current_snapshot, peer_patches, peer_library_digests ...)
  -> MergedPatch
  -> FederatedClient.apply_update(merged)
  -> SkillLibrary.apply_patch(merged)
  -> next round retrieval
```

Confirmed behavior:

- Client takes `library.snapshot(round_idx)` before execution.
- Server sees both current `LibrarySnapshot` and `LibraryDigest`.
- Stage2 returns `MergedPatch` per worker.
- Client applies server patch with `FederatedClient.apply_update()`.
- Same per-family worker library root is reused across rounds, so next round retrieval sees applied changes.

## Storage Layout

Family-loop run output:

```text
<output_dir>/families/<family_id>/libraries/<worker_id>/...
<output_dir>/families/<family_id>/workers/<worker_id>/tasks/round_XXX_<task>/patch.json
<output_dir>/families/<family_id>/round_XX_summary.json
```

Single-family materialized layout:

```text
<run_root>/<family_id>/libraries/
<run_root>/<family_id>/patches/
<run_root>/<family_id>/trajectories/
<run_root>/<family_id>/metrics/
```

## State Isolation

Confirmed:

- Every family has its own `family_output_dir`.
- `library_root` is asserted empty before a new family starts.
- `run_family_experiment()` uses a fresh `experiment_id`.
- Failure cleanup removes only failed family library roots and preserves task logs.

## Does a Saved Skill Reach the Next Prompt?

Yes for both SE and federated paths:

- Retrieval occurs before every task via `TaskExecutor._retrieve_skills(task, library)` or CLI harness helper reuse.
- The `library` object is the same per-family object updated by `apply_patch` in the previous round.
- Therefore, if a valid `SKILL.md` was written under the worker library root, it can be retrieved and inserted into the next task prompt.

## Caveats

| Caveat | Impact |
|---|---|
| Retrieval quality depends on `TaskExecutor._retrieve_skills` matching `required_skills`/content; audit did not prove semantic relevance of retrieved skills | Medium |
| Invalid SKILL.md may be skipped by `digest()` and flagged by `validate()`, but `apply_patch()` itself writes files as given after schema validation | Medium |
| In failed family cleanup, library files are deleted to preserve isolation; patches/trajectories remain for audit | Intended |

## Verdict

Skill lifecycle is truly wired: patch is not merely saved; it is applied to the library and visible to next-round retrieval. The remaining risk is semantic quality of retrieved/generated skills, not dataflow connectivity.
