# FederatedSkill Small-Scale Faithful Family Selection Report

## Scope

This is a **small-scale faithful reproduction**, not a full benchmark reproduction.
The full SkillFlow benchmark contains **20 families and 166 sequential tasks**. The
subset reduces only the number of families; every selected family retains every task,
the official `ALL_TASK_DIFFICULTY_RANKING.json` order, and the complete difficulty
sequence.

## Deterministic Selection Rule

The selector reads `[metadata].difficulty` from every official `task.toml` and maps
the ordered labels as `easy=1`, `medium=2`, `medium-hard=2.5`, `hard=3`, and
`expert=4`. A family score is the arithmetic mean across all tasks in that complete
family.

- **Easy**: lowest family mean; ties are resolved by family ID.
- **Medium**: family closest to the median of all 20 family means, excluding the
  already selected endpoints; ties are resolved by family ID.
- **Hard**: highest family mean; ties are resolved by family ID.

This is metadata-driven and reproducible. It does not sample tasks, inspect model
outcomes, or choose families based on expected success.

Task order uses the repository's existing official-loader rule: listed tasks follow
`ALL_TASK_DIFFICULTY_RANKING.json`; any task directory omitted by that file is retained
and appended by task ID. This preserves all 166 tasks rather than silently deleting
unranked dataset entries. Ranking references absent from the downloaded dataset are
skipped, matching the loader. The machine-readable selection file records every append
and missing reference.

## Phase 0 Selection

Phase 0 uses only the complete Easy family below with the paper's original Setting1:
one Qwen3.6-plus worker using Claude Code. It verifies the full pipeline and then stops.

| Tier | Family | Tasks | Mean score | Official difficulty labels |
|---|---|---:|---:|---|
| Easy | `Cross-Format-Data-Reconciliation` | 8 | 2.000 | easy=2, medium=4, hard=2 |
| Medium | `PPT-Formatting-Optimization` | 8 | 2.250 | medium=6, hard=2 |

Phase 0 task count: **8**. The Medium row is reserved for Phase 1 and is not executed
during Phase 0.

## Phase 1 Selection

Phase 1 uses the complete Easy and Medium families listed above, for **16 tasks per
worker**. Setting1 preserves the official single Qwen3.6-plus / Claude Code client.
Setting2 preserves the official three GLM-5 / Claude Code clients, GLM-5 merger,
replicate partitioning, every-task synchronization, and personalized unshared merge.

## Full Metadata Inventory

| Family | Tasks | Mean score | Difficulty counts | Selected |
|---|---:|---:|---|---|
| Cross-Format-Data-Reconciliation | 8 | 2.000 | easy=2, medium=4, hard=2 | 是 |
| DMAIC-Quality-Analysis | 9 | 2.000 | medium=9 | 否 |
| Embedded-Data-Repair | 8 | 2.000 | medium=8 | 否 |
| HWPX-Document-Automation | 8 | 2.000 | medium=8 | 否 |
| Healthcare-Cost-Benefit-Analysis | 9 | 2.000 | medium=9 | 否 |
| Industry-Correlation-Analysis | 8 | 2.000 | medium=8 | 否 |
| Inventory-&-Finance-Integration | 8 | 2.000 | medium=8 | 否 |
| Medical-Data-Standardization | 9 | 2.000 | medium=9 | 否 |
| Production-Capacity-Planning | 9 | 2.000 | medium=9 | 否 |
| Weighted-Risk-Assessment | 8 | 2.125 | easy=2, medium=3, hard=3 | 否 |
| PPT-Formatting-Optimization | 8 | 2.250 | medium=6, hard=2 | 是 |
| Sales-Pivot-Analysis | 8 | 2.438 | medium=3, medium-hard=3, hard=2 | 否 |
| Financial-Statement-Rolling | 9 | 2.444 | medium=5, hard=4 | 否 |
| Supply-Chain-Replenishment | 9 | 2.444 | medium=5, hard=4 | 否 |
| Operational-Recovery-Planning | 8 | 2.625 | medium=3, hard=5 | 否 |
| Distribution-Center-Auditing | 8 | 2.750 | medium=3, hard=4, expert=1 | 否 |
| OCR-Data-Extraction | 8 | 2.750 | medium=2, hard=6 | 否 |
| Compensation-Scenario-Modeling | 8 | 3.000 | hard=8 | 否 |
| Document-Fraud-Detection | 8 | 3.000 | hard=8 | 否 |
| SEC-13F-Financial-Analysis | 8 | 3.000 | hard=8 | 否 |

## Representativeness and Limits

The Phase 1 subset spans the observed lower endpoint and central tendency of official
task-level difficulty metadata. It tests whether the core Self Evolution versus
Federated Skill Evolution trend appears across two complete sequential curricula while
preserving the paper's within-family learning process.

It does **not** estimate the full-benchmark aggregate with the same statistical
coverage as all 20 families. Domain coverage, rare tool dependencies, and the exact
paper-wide success rate remain outside this subset. Full reproduction remains the
20-family, 166-task extension.

## Protocol Guardrails

- No task sampling.
- No task removed from a selected family.
- No task order or difficulty sequence changed.
- Every family starts with an empty skill library.
- Phase 0 runs only Setting1 on the Easy family and then stops.
- Phase 1 Setting1 and Setting2 use the same Easy/Medium family list.
- Setting1 and Setting2 retain their paper-original client counts, models, and harnesses.
- Setting3 and Setting4 are not part of this phase.
