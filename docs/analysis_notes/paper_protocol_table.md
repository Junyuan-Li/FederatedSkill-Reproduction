# PART 0 — Paper Experimental Protocol Freeze

对应：论文 arXiv:2606.03143 Section 5.1 Experiment Setup + SkillFlow benchmark
定义。**只读审计**，未运行任何实验（后台仍有一个此前已经过用户确认的
Phase1 sanity 实验在独立跑，与本文件的产出无关，本文件本身不触发/不依赖
任何实验执行）。

---

## Benchmark

| 项目 | 值 |
|---|---|
| Benchmark | SkillFlow |
| Scale | **20 task families**，**166 tasks** |
| Task organization | Family-local sequential task sequence（同一 family 内的任务按固定顺序执行，不跨 family 混排） |
| Difficulty | tasks ordered by increasing difficulty（family 内任务难度单调递增） |
| Task execution | 同一 family 内的任务必须严格顺序执行（不可乱序/不可跳过） |
| Initialization | empty skill library（每个 family 从空技能库开始） |
| After each round | agent 用 trajectory + reward signal 更新技能库 |
| Evolution operations | discovering skills / patching skills / transferring skills / maintaining skills |

**代码合规性核查**（逐项核实，非假设）：

| Benchmark 要求 | 当前实现 | 核查依据 | 合规？ |
|---|---|---|---|
| 20 families / 166 tasks | `benchmark/families/*.json` 恰好 20 个官方 family（14×8-task + 6×9-task=166），另 5 个自建 legacy family 已被 `paper_benchmark_only: true` 显式排除 | `experiments/run_experiment.py:159-196`（过滤+断言恰好剩 20 个） | ✅ |
| Family-local sequential | `FamilyCurriculumSampler.sample()` 按 `round_idx+1` 严格递增取任务，无跨 family 混排 | `benchmark/curriculum.py::FamilyCurriculumSampler` | ✅ |
| Increasing difficulty | `TaskFamily.__init__` 按 difficulty 升序排序 | `benchmark/family.py::TaskFamily.__init__` | ✅ |
| Sequential execution，禁止跳过/乱序 | 采样器无 shuffle/random，严格按索引推进 | 同上 | ✅ |
| Empty skill library 初始化 | `_run_family_loop()` 每个 family 独立构造全新 `SkillLibrary`；运行时 `assert server.current_capability.to_dict() == {}` 强制校验 | `experiments/run_experiment.py:911` | ✅ |
| Round 后用 trajectory+reward 更新技能库 | `client/distiller.py::PatchDistiller.distill(trajectory, reward, library)` → `SkillLibrary.apply_patch()` | `client/distiller.py` | ✅ |
| discovering/patching/transferring/maintaining | discover+patch = Distiller 生成 upsert/deletion；transfer = 联邦场景下 `server/merge.py` 跨 client 合并（`source_worker_id != target_worker_id` 时即为 transfer）；maintaining = `SkillLibrary` 持续写入/裁剪同一路径下的 SKILL.md | `client/distiller.py` / `server/merge.py` / `client/library.py` | ✅ |

**结论：Benchmark 部分未发现不合规项，无需修改代码。**

---

## Round Definition

**定义**：one round = one task execution + evaluation + skill evolution

```
For each family:

Round 1:  Task1  →  skill update
Round 2:  Task2  →  skill update
...
until family completion
```

**代码合规性核查**：

| 要求 | 当前实现 | 核查依据 | 合规？ |
|---|---|---|---|
| 1 round = 1 task | `_run_round(round_idx)` 每次调用只处理 `sampler.sample_batch(worker_ids, round_idx)` 返回的单个 round 的任务分配（task-level == round-level，无批量任务打包进一个 round） | `experiments/federated.py::_run_round` / `experiments/baseline.py::SelfEvolutionRunner._run_round` | ✅ |
| Round 内含 execution+evaluation+skill update | executor.run()（execution）→ verifier 赋 reward（evaluation）→ distiller.distill()+apply_patch()（skill update）三步都在同一个 round 内顺序发生 | `experiments/federated.py:425-566` | ✅ |
| Round 数 = family 任务数（8或9），非固定 8 | 4 个主配置 yaml 均设 `rounds_per_family_mode: "family_length"`（默认值），`_run_family_loop()` 用 `len(family.tasks)` 作为该 family 的实际 round 数，yaml 里字面的 `rounds: 8` 只在 `"fixed_cap"` 模式下才作为硬上限（当前未启用） | `experiments/run_experiment.py:771-816` | ✅ |
| 顺序直到 family 完成 | family 循环内单调递增 `round_idx`，无提前终止/无跳过剩余任务的逻辑（除非任务/CLI 抛出未捕获异常导致整个 family 标记失败并转移到下一个 family，这是失败处理机制而非违反顺序执行的规则） | `experiments/run_experiment.py::_run_family_loop` | ✅ |

**结论：Round Definition 未发现不合规项，无需修改代码。**

---

## Evaluation

| 项目 | 值 |
|---|---|
| Main metric | average task completion rate（即 success rate，逐 family/逐 worker 平均） |
| Additional recorded metrics | capability improvement / skill evolution / cost |

**代码合规性核查**：

| 指标 | 当前实现 | 核查依据 | 合规？ |
|---|---|---|---|
| average task completion rate | `FederatedMetrics.success_rate()`：`SR = N_success/N_total`，`reward>=1.0` 记为成功；`table1.csv::final_success_rate` 做跨 family 宏平均 | `evaluation/metrics.py` / `evaluation/paper_export.py` | ✅ |
| capability improvement | `weighted_global_score()`（论文 Eq.3）逐轮聚合 + `CapabilityTracker` 维护逐 cell（family×worker）能力矩阵，落盘 `capability_matrix.jsonl`/`capability.csv` | `evaluation/federated_score.py` / `server/capability.py` | ✅ |
| skill evolution | `FederatedMetrics.skill_growth()`（Δskill）+ `evolution_trace.jsonl`（记录每次 ABSORB/REPAIR/REFACTOR/NO_UPDATE directive） | `evaluation/metrics.py` / `evaluation/audit_trace.py` | ✅ |
| cost | `CostAccountant`（client/server 分组核算，4 个 LLM 调用环节：client_execution/patch_distiller/stage1_planner/stage2_merge 全部计入，非只算一部分） | `evaluation/cost_accounting.py` | ✅ |

**结论：Evaluation 部分未发现不合规项，无需修改代码。**

---

## 总体结论

Benchmark / Round Definition / Evaluation 三部分逐项核实，**当前实现与本
PART 0 冻结的协议表完全一致，未发现需要修改代码的不合规项**（本次审计
过程中也未做任何代码改动，符合"先不运行实验、只读审计"的要求）。

若后续 PART 1-8 的核查中发现任何不合规项，将在对应文档
（`model_harness_report.md` / `execution_budget_audit.md` / 等）里单独
列出，并且只修复 **实现/配置层面** 的偏差（例如 provider 路由、超时数值、
文档措辞），**不会修改** 算法层代码（skill patch mechanism / aggregation
logic / capability model / benchmark task / evaluation metric / model
backbone / agent harness 均在禁止修改清单内，逐字遵守）。

PART 0 完成。按你的指示 STOP，等待你确认后再继续 PART 1（Model and
Harness Verification）。
