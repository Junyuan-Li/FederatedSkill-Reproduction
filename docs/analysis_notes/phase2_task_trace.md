# Phase 2 Task Lifecycle Trace

Chosen task: `Cross-Format-Data-Reconciliation`, round 0, task `01-cloud-service-portfolio-diff`.

This is the concrete data path for one real SkillFlow task in the current codebase.

## 1. Task Metadata

Source files:

- Loader: `benchmark.family.load_all_families()` -> `load_family()`
- Task model: `benchmark.task.Task`
- Verification model: `benchmark.task.VerificationSpec`
- Family scheduler: `benchmark.curriculum.FamilyCurriculumSampler`

Data source:

- `benchmark/families/Cross-Format-Data-Reconciliation.json`
- `Task.task_id`: e.g. `01-cloud-service-portfolio-diff`
- `Task.description`: natural language prompt used by executor/harness
- `Task.files`: mapped from JSON `environment` if present
- `Task.verification`: mapped from JSON `tests` into `skillflow_script` if present
- `Task.metadata`: includes real SkillFlow fields such as source/environment paths when converter supplied them

## 2. Workspace Creation

Default API path:

- `executor.router_executor.VerificationAwareExecutor.run()` sees `verification.type in {'skillflow_script', 'docker'}`.
- It dispatches to `executor.skillflow_executor.SkillFlowTaskExecutor.run()`.
- `SkillFlowTaskExecutor` creates a temporary workspace with `tempfile.mkdtemp()`.
- It writes `task.files` to that workspace.

CLI path:

- Only if `--execution-mode cli` is supplied.
- `executor.harness_executor.HarnessAwareExecutor.run()` dispatches to `harness.factory.get_harness()`.
- `CLIAgentHarnessBase.initialize()` creates `WorkspaceManager` and either copies `metadata['source_environment_dir']` or writes `task.files`.

## 3. Agent Prompt

Default API path:

- `SkillFlowTaskExecutor` reuses `client.executor.TaskExecutor` helpers:
  - `_retrieve_skills(task, library)`
  - `_format_skills(relevant_entries)`
  - `_build_system_prompt(profile)`
  - `_build_user_prompt(task, skills_text)`
- Prompt is stored only as a truncated `TrajectoryStep(content='[Prompt] ...')`.

CLI path:

- `CLIAgentHarnessBase._build_workspace_prompt()` includes task description, input files, retrieved skills, and mandatory verification discipline.
- Claude/Qwen pass full prompt via stdin; Kimi writes full prompt into `--prompt` argument due CLI behavior.

## 4. Agent / Solution Generation

Default API path:

- `backbone = BackboneRouter.get(worker_id)`
- `backbone.call(user_prompt, system_prompt)`
- `TaskExecutor._extract_code(llm_result.text)`
- writes `solution.py` or `task.metadata['solution_filename']`
- forcibly runs `solution.py` with `subprocess.run([sys.executable, solution_filename], cwd=workspace)`

CLI path:

- `run_cli_subprocess()` spawns `claude`, `qwen`, or `kimi`.
- `BaseAgentHarness._force_execute_solution()` runs generated `solution.py` after CLI session and re-reads workspace files.

## 5. Official Verifier / Reward

Default API path:

- `SkillFlowTaskExecutor._get_verifier_for()` returns `SkillFlowScriptVerifier` for `skillflow_script`.
- `SkillFlowScriptVerifier.verify_in_workspace(task, workspace)` materializes test script and runs pytest/script.
- `VerificationResult.reward` is copied into `Trajectory.reward`.

CLI path:

- `BaseAgentHarness.run()` calls `AgentWorkspaceExecutor._verify()` after forced execution.
- Reward enters `Trajectory` through `TrajectoryCollector.finalize()`.

## 6. Trajectory Checkpoint

- `SelfEvolutionRunner._run_client_trial_attempt()` or `FederatedRunner._run_client_phase_with_retry()` receives `Trajectory`.
- `TaskCheckpointStore.save_trajectory_reward()` writes:
  - `workers/<worker>/tasks/round_XXX_<task>/trajectory.json`
  - `reward.json`
  - task-level trajectory mirror under `tasks/<task>/trajectory/<worker>/trajectory.json`

## 7. Patch

- `FederatedClient.distill_patch()` calls `PatchDistiller.distill()`.
- Distiller uses trajectory, library snapshot, trial outcome, worker profile.
- LLM output is parsed into `WorkerPatch`.
- `TaskCheckpointStore.save_success()` writes `patch.json` and `task_status.json`.

## 8. Library

SE path:

- `SelfEvolutionRunner._run_client_trial_attempt()` calls `client.library.apply_patch(patch)`.

Federated path:

- `FederatedRunner._run_round()` uploads patches and library snapshots to server.
- Server returns `MergedPatch` per worker.
- `client.apply_update(merged)` calls `SkillLibrary.apply_patch()`.

## 9. Metrics

- `TrialSnapshot` is built from trajectory reward/tokens/cost, patch tokens, library size before/after, SELR info.
- `ExperimentEvaluator.record_round()` computes round SR, library size, compression ratio, cost.
- `_save_results()` writes `round_XX_summary.json`.
- `_save_family_loop_summary()` uses `collect_task_checkpoint_stats()` to compute task-level family SR.

## Confirmed Flow vs Gaps

| Data | Flows? | Notes |
|---|---:|---|
| Task metadata -> prompt | Yes | Prompt is built from `Task.description` and skills. API path stores truncated prompt only. |
| Workspace files -> agent | Yes | API path writes `task.files`; CLI path may copy source environment tree. |
| Agent output -> solution.py | Yes | API path extracts code; CLI path collects generated workspace files. |
| solution.py -> verifier | Yes | Both paths force execution before verification. |
| Verifier reward -> trajectory | Yes | `Trajectory.reward` and `verification` derived fields populated. |
| Trajectory -> patch | Yes | `PatchDistiller` consumes compacted trajectory and trial outcome. |
| Patch -> library | Yes | SE direct apply; federated via merge apply. |
| Reward -> metrics | Yes | `TrialSnapshot.reward` and checkpoint stats. |
| Full prompt/token split | Partial | `Trajectory.total_tokens` exists; prompt/completion split is not stored in `Trajectory`. |
| CLI tool calls | Conditional | Only available in `--execution-mode cli`; default API path has no real CLI tool calls. |
