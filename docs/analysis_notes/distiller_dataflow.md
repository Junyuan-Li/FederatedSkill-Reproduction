# Phase 4 Patch Distiller Dataflow

Object audited: `client.distiller.PatchDistiller`, `llm.prompt_builder.DistillerPromptBuilder`, `client.federated_client.FederatedClient`.

## Real Call Chain

```text
SelfEvolutionRunner/FederatedRunner
  -> FederatedClient.distill_patch(trajectory)
  -> PatchDistiller.distill(trajectory, library, profile)
  -> _step1_compress(trajectory)
  -> _step2_snapshot(library, round_idx)
  -> _step3_outcome(trajectory)
  -> _step4_prompt(compacted, snapshot, outcome, profile)
  -> _step5_call_llm(worker_id, system_prompt, user_prompt)
  -> _step6_validate(raw_dict, profile, round_idx)
  -> _step7_build_patch(...)
  -> WorkerPatch
```

## Inputs Actually Used

| Input | Source variable | Used in distiller? | Evidence |
|---|---|---:|---|
| Compacted trajectory | `TrajectoryCompressor.compress(trajectory)` | Yes | `_step1_compress()` |
| Library snapshot | `library.snapshot(round_idx)` | Yes | `_step2_snapshot()` |
| Reward | `trajectory.reward` | Yes | `_step3_outcome()` -> `TrialOutcome.reward` -> `WorkerPatch.reward` |
| Verifier feedback | `trajectory.verifier_output[:1500]` | Yes | `_step3_outcome()` includes `verifier_feedback` |
| Verifier subtest failures | `trajectory.verifier_subtest_failures` | Yes | `_step3_outcome()` -> `verification_failures` |
| Failure reason | `trajectory.failure_reason` | Yes | `_step3_outcome()` -> `failure_reason` |
| Execution logs | `Trajectory.execution_logs` | Indirect/partial | Only if included in compressed trajectory steps or prompt builder consumes compacted fields; not a distinct TrialOutcome field |
| Final agent message | `trajectory.final_message` | Yes | `_step3_outcome()` |
| Tokens/cost | `trajectory.total_tokens`, `trajectory.cost_usd` | Yes | `_step3_outcome()` |
| Worker profile | `WorkerProfile` | Yes | `_step4_prompt()` |

## Output Validation

`_step6_validate()` performs:

- field normalization: `upsert_files` / `upserts`, `delete_paths` / `deletions`
- path safety through `validate_safe_rel_path()`
- empty/binary content filtering
- privacy audit heuristics

`_step7_build_patch()` emits `WorkerPatch` containing:

- `worker_id`
- `round_idx`
- `upserts`
- `deletions`
- `reward`
- `summary`
- LLM metadata in patch metadata where available

## Cost Dataflow

- `PatchDistiller._step5_call_llm()` returns `(raw_dict, BackboneCallResult)`.
- If a `CostAccountant` is attached by runner, distiller records `component='patch_distiller'` with prompt/output tokens and cost.
- SE attaches this via `FederatedClient.set_cost_recorder()` in `SelfEvolutionRunner.__init__` when `output_dir` exists.
- Federated attaches the same in `FederatedRunner.__init__`.

## Audit Findings

| Finding | Severity | Notes |
|---|---|---|
| Verifier feedback and failure reason do reach the distiller | Good | This satisfies the audit's main concern: not just prompt presence, actual data path exists. |
| Full execution logs are not explicitly modeled as a separate distiller input | Medium | CLI path can place forced execution logs in `Trajectory.execution_logs`; whether prompt builder renders them depends on compacted trajectory content, not a dedicated field. |
| Distiller fail-loud behavior exists | Good | LLM failure raises `PatchDistillationFailure`; strict mode aborts family, audit mode can continue. |
| Worker patch is the server-uploaded artifact, not raw trajectory | Good | Federated server receives `WorkerPatch`, not `Trajectory`. |

## Verdict

The distiller dataflow is functionally connected and uses reward, verifier feedback, and failure reason. Its main traceability gap is that execution logs are not a first-class prompt section in the audited code path; they may be included only as trajectory step content depending on executor mode.
