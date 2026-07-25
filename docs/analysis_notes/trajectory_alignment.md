# Phase 3 Trajectory Alignment

Object audited: `core.datatypes.Trajectory` and producers in `executor.skillflow_executor`, `harness.cli_harness_base`, `executor.trajectory`.

## Required Fields vs Current Availability

| Required by audit | Current field/source | Status |
|---|---|---|
| Agent prompt | `Trajectory.steps[1].content` in API path, truncated to first 300 chars; CLI path includes richer step records from stream-json where available | Partial |
| Agent response | `Trajectory.steps` assistant step and `final_message` | Confirmed |
| Tool calls | computed `Trajectory.tool_calls` from step tool calls; CLI path can populate from stream-json; API path usually empty | Conditional / partial |
| Execution logs | `Trajectory.execution_logs`; CLI forced execution fills it; API path records run-solution as a `TrajectoryStep`, not `execution_logs` | Partial |
| Verifier feedback | `verifier_output`, `stdout`, `stderr`, `verification` derived field | Confirmed |
| Reward | `reward` | Confirmed |
| Failure reason | `failure_reason` derived from exception/verifier output/subtests | Confirmed |
| Skill library snapshot | Not stored directly in `Trajectory`; distiller separately calls `library.snapshot()` | Missing from Trajectory, available in distiller path |
| Workspace snapshot | `generated_files`; API path detects modified files; CLI path has generated files from workspace diff | Partial; no full workspace snapshot serialized |
| Prompt tokens | `Trajectory.total_tokens` only; `BackboneCallResult.prompt_tokens` not stored separately in Trajectory | Partial |
| Completion tokens | Not stored separately in Trajectory | Missing |
| Cost | `cost_usd` | Confirmed |

## Producer Differences

### API Path: `SkillFlowTaskExecutor`

`SkillFlowTaskExecutor.run()` records:

- workspace creation step
- truncated prompt step
- raw LLM response up to 2000 chars
- generated code step
- solution execution stdout/stderr step
- verifier result step
- `total_tokens` and `cost_usd` from `LLMBackbone.call()`
- `generated_files` via workspace diff

Limitations:

- agent prompt is truncated to 300 chars in trajectory step, so full prompt is not reconstructable from `trajectory.json`.
- prompt/completion token split is not stored, only total.
- no real CLI tool-call trace because this path does not spawn a CLI harness.

### CLI Path: `CLIAgentHarnessBase`

`CLIAgentHarnessBase.collect_trajectory()` records:

- skill retrieval action
- cli invocation action
- generated files
- stream-json derived steps when present
- CLI event records
- forced solution execution logs
- verifier step
- token/cost parsed from CLI result event when available

Limitations:

- Qwen/Kimi may not provide reliable structured success markers or complete token/cost stream-json usage.
- CLI availability and local binary configuration are external prerequisites.

## Alignment Judgment

Trajectory is sufficient to trace reward, verifier output, generated files, and cost at a coarse level. It is not fully sufficient for paper-grade reconstruction of every agent prompt and token split in default API mode. For strongest traceability use `--execution-mode cli`, but even then token/cost availability depends on the CLI output format.

## Actionable Audit Note

No code changes were made. If later code changes are allowed, the most direct traceability improvement would be to persist full prompt and prompt/completion token split into trajectory or a sidecar audit log. That is outside this audit's no-modification constraint.
