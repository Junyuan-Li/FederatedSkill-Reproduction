# Phase 0 — Paper Protocol Audit

对应任务书：FederatedSkill: Federated Learning for Agentic Skill Evolution
(arXiv:2606.03143) Section 5 Experimental Setup / Section 5 Evaluation / Appendix。

**范围声明**：本文件只做只读审计（读论文 Section 5/Appendix + 官方仓库
`FederatedSkill-main/FederatedSkill-main/` 的 configs/skillfl/skillflow_adapter
+ 本仓库 `experiments/configs/*.yaml`、`experiments/run_experiment.py`、
`experiments/federated.py`、`evaluation/metrics.py`），**未运行任何实验**、
**未修改任何算法/harness/benchmark/evaluation 代码**。凡是论文原文未明确、
需要从代码回答的问题，均标注具体文件+行号来源，不做主观推测。

---

## Benchmark

| 项目 | 确认结果 | 依据 |
|---|---|---|
| Benchmark | SkillFlow（HuggingFace `zhang-ziao/SkillFlow-Task`，通过 `benchmark/families/*.json` 落地为 20 个官方 family） | `experiments/configs/setting_*.yaml` 均设 `paper_benchmark_only: true`；`experiments/run_experiment.py:159-196` 按此过滤，日志显式打印"加载论文官方 N 个 family，排除 M 个非官方 family" |
| Task families 数量 | **20**（另有 5 个本项目自建的非论文 legacy family，`paper_benchmark_only: true` 时被显式排除，不参与任何 Setting1-4 主实验） | `run_experiment.py:181` 断言 `paper_benchmark_only=true` 时过滤后必须恰好剩 20 个官方 family，否则 fail-loud；`benchmark/family.py` docstring 标注 5 个 legacy family 为 `[EXTENSION]`（非官方数据） |
| 每 family 任务数 | 8 或 9（14 个 8-task family + 6 个 9-task family，共 166 个官方任务） | 已在此前会话用程序化脚本核对（见仓库记忆 `experimental_fidelity_plan.md` Phase0 章节），本次未重新计数 |
| Task order | **sequential**（按 family 内部固定的 difficulty 升序排列，非随机） | `benchmark/family.py::TaskFamily.__init__` 按 difficulty 升序排序；`FamilyCurriculumSampler.sample()` 按 `round_idx+1` 严格递增取任务，无跳跃/无重排 |
| Difficulty | **increasing difficulty**（family 内任务难度单调递增） | 同上，`TaskFamily` 排序逻辑保证 |
| Task shuffle | **disabled** | `FamilyCurriculumSampler` 无任何 shuffle/random.sample 调用；4 个主实验 yaml 的 `sampler: "family_curriculum"` 均不含 `shuffle` 字段 |

---

## Agent Initialization

| 项目 | 确认结果 | 依据 |
|---|---|---|
| Initial skill library | **empty**（每个 family 从空库开始） | `experiments/run_experiment.py:911` 运行时断言 `server.current_capability.to_dict() == {}`（family 循环开始前 capability matrix 必须为空，否则 `AssertionError` 中断实验） |
| Client state reset | **required**（每个 family 独立重置：capability matrix、两级 memory、每个 worker 的 `SkillLibrary` 目录） | `run_experiment.py:916` 断言 `_mem_snapshot["high_level"]["last_updated_round"] == -1`；`run_experiment.py:922` 断言其余 memory 字段处于初始态；`_run_family_loop()` 为每个 family 单独构造全新的 server/client/library 实例（非跨 family 复用） |
| Cross experiment skill contamination | **disabled**（有运行时强制断言，比论文文字要求更严格） | `run_experiment.py:837` 断言 `set(sampler._families.keys()) == {family_id}`（采样器一次只能看到当前 family）；`run_experiment.py:873` 断言 `not leftover`（`family_output_dir`/`library_root` 若有残留文件直接 `AssertionError`，阻止上一个 family 的技能泄漏进下一个 family） |

---

## Evolution Protocol

论文 Section 4 描述的流水线，代码逐段对应关系：

```
trajectory collection        → executor.run(task, library, profile, round_idx)
                                 产出 core.datatypes.Trajectory
        ↓
reward evaluation             → benchmark/verifier.py（function_test /
                                 skillflow_script 二值 reward），写回
                                 Trajectory.reward
        ↓
skill discovery + patch生成    → client/distiller.py::PatchDistiller.distill()
                                 （7 步管线：读 trajectory+reward+library
                                 → LLM 生成候选 patch → 路径/内容安全校验
                                 → 组装 WorkerPatch，upserts/deletions 均为
                                 LLM 判定内容，无规则替代）
        ↓
skill update                  → 联邦：server/planner.py（Stage1 EvolutionPlanner，
                                 决定 ABSORB/REPAIR/REFACTOR/NO_UPDATE）→
                                 server/merge.py（Stage2 EvolutionExecutor，
                                 LLM 生成 MergedPatch）→
                                 client/library.py::SkillLibrary.apply_patch()
                                 （纯机械文件写入，无独立决策）
                                 非联邦（Setting1 SE）：worker 直接
                                 apply_patch(own WorkerPatch)，无 Stage1/2
```

---

## Federation Protocol

| 项目 | Setting1 (SE) | Setting2 (Homo) | Setting3 (Hetero Backbone) | Setting4 (Full Hetero) | 依据 |
|---|---|---|---|---|---|
| number of clients | 1（u0） | 3（u0/u1/u2） | 3（u0/u1/u2） | 3（u0/u1/u2） | 对应 yaml `workers:` 列表长度 |
| federated | false | true | true | true | 对应 yaml `federated:` 字段 |
| number of rounds | = 该 family 的任务数（8 或 9） | 同左 | 同左 | 同左 | 4 个 yaml 均设 `rounds_per_family_mode: "family_length"`（默认值，显式声明）；`run_experiment.py:771` docstring："`family_rounds = len(family.tasks)`，与论文一致"。yaml 里 `rounds: 8` 只在 `rounds_per_family_mode: "fixed_cap"` 时才作为硬上限生效，当前 4 个主配置均未启用该模式 |
| round definition | 1 round = 1 task（task-level == round-level，非 family 级批量更新） | 同左 | 同左 | 同左 | `experiments/federated.py::_run_round(round_idx)` 每次调用只处理 `self._sampler.sample_batch(worker_ids, round_idx)` 返回的**单个** round 的任务分配 |
| aggregation frequency | N/A（无服务器聚合） | 每 round（每个 task 之后） | 同左 | 同左 | `federated.py:502` `self._server.run_round(...)`（内部即 Stage1+Stage2）在 `_run_round()` 内**无条件**每轮调用一次，不做任何跨轮批量缓冲 |
| server update frequency | N/A | 与 aggregation frequency 相同（每 round） | 同左 | 同左 | 同上 |

**⚠️ 一处需要如实披露的字段/代码不一致（本次审计新发现，未修改代码）**：
4 个 yaml 均声明 `sync_schedule: "every_task"`，字面意思与代码实际行为
（每轮无条件聚合）恰好一致，但程序化核实发现 **`sync_schedule` 这个字段
本身从未被任何 Python 代码读取**（全仓 grep 只命中 yaml 注释和这条审计
记录本身，`experiments/run_experiment.py`/`federated.py` 均不引用
`cfg.get("sync_schedule", ...)`）。同理 `merger_mode: "unshared"` 也未被
代码读取。真正被代码读取并影响行为的字段只有 `isolated_worker_skills`
（`run_experiment.py:1195`，控制 partitioner 是否为 replicate 语义）。
**结论**：当前"每轮都聚合"是代码的唯一实现路径（硬编码行为，非可配置项），
`sync_schedule`/`merger_mode` 目前是纯文档性字段，尚未接入任何分支逻辑；
如果未来需要支持论文可能存在的"非每轮聚合"变体，需要新增代码读取这两个
字段并接线，而不是仅凭 yaml 声明。这不影响 Setting1-4 当前实验结果的
有效性（因为 4 个配置声明的语义与代码硬编码行为恰好一致），但如实记录
避免误以为"改这个 yaml 字段就能改变聚合频率"。

---

## Evaluation

| 指标 | 论文对应 | 计算方式 | 代码位置 |
|---|---|---|---|
| Success Rate | Table 1 / Figure 2 | `SR = N_success / N_total`，`reward >= 1.0` 视为成功（二值 reward，来自 verifier） | `evaluation/metrics.py::FederatedMetrics.success_rate()` |
| Capability Improvement | Section 4.2.1 Capability Matrix / Eq.3 Weighted Global Score | `weighted_global_score()`（默认按 worker 等权重聚合各 worker 的能力/成功指标），逐轮写入 `round_result.metrics["weighted_global_score"]`；capability matrix 本身经 `server/capability.py::CapabilityTracker.update_from_plan_dict()` 由 Stage1 LLM 输出增量维护，逐 cell（family × worker）状态落盘至 `capability_matrix.jsonl` | `evaluation/federated_score.py::weighted_global_score()` + `evaluation/evaluator.py::ExperimentEvaluator.record_round()`；`evaluation/integrity_logs.py::CapabilityMatrixRecorder` |
| Skill Evolution | Figure 3（Evolution Dynamics） | `ΔSkill = |L_i^{t+1}| − |L_i^t|`（skill_growth，逐轮/逐 worker 库大小变化），配合 `capability_matrix.jsonl`/`evolution_trace.jsonl` 观察 ABSORB/REPAIR/REFACTOR 的真实发生频率 | `evaluation/metrics.py::FederatedMetrics.skill_growth()`；`evaluation/audit_trace.py::AuditTraceRecorder`（evolution_trace.jsonl） |
| （辅助，非论文主指标，已在代码 docstring 声明） | Appendix C 压缩率 / Appendix E SELR | `compression_ratio()`/`privacy_gain()`（两者数值公式完全相同）非 Appendix E 真实 SELR（按敏感实体计数）；真实 SELR 计算见 `evaluation/selr.py::compute_selr_from_texts()` | `evaluation/metrics.py`；`evaluation/selr.py` |

---

## Environment Version

| 项目 | 值 | 来源 |
|---|---|---|
| Claude Code CLI version | 2.1.214 (Claude Code) | 本次会话 `results/cli_validation_report.json::claude-code.cli_version`（真实 subprocess `--version` 探测） |
| Qwen Code CLI version | 0.19.11 | 同上 `qwen-code.cli_version` |
| Kimi CLI version | 0.28.1（`@moonshot-ai/kimi-code`） | 同上 `kimi-cli.cli_version` |
| Model name（worker） | `qwen3.6-plus` / `glm-5` / `kimi-k2.5` | `experiments/configs/setting_*.yaml::workers[].backbone_model` |
| Model name（server/merger） | `glm-5` | `experiments/configs/setting_homo_fed.yaml`/`setting_hetero_backbone.yaml`/`setting_full_hetero.yaml::server.backbone_model` |
| Provider | `dashscope`（三个 backbone 统一走 DashScope OpenAI-compatible 端点） | `experiments/configs/setting_*.yaml::model_provider` |
| API endpoint | `https://dashscope.aliyuncs.com/compatible-mode/v1`（LLM 调用/patch distillation/Stage1-2 走此端点；Claude Code CLI 子进程内部由 `ClaudeCodeHarness.build_env()` 转换为对应 CLI 所需的鉴权环境变量，不改变实际路由目标） | `experiments/configs/setting_*.yaml::api_base`；仓库记忆 `federatedskill-reproduction.md` "Phase 1 真实运行路由修复" 章节 |
| CLI Harness 真实可执行性 | Claude Code / Qwen Code / Kimi CLI 三者本次会话均已用真实 subprocess 端到端验证通过（`returncode=0`, 真实 `Write` 工具调用, 目标文件内容精确匹配） | `results/cli_validation_report.json`（本次会话生成） |
| execution_mode 默认值 | 真实实验（未加 `--mock`/`--dry-run`）默认 `"cli"`；开发/结构验证场景默认 `"api"` | `run.py:16-89` |

`experiment_summary.json` 已同步生成（结构化版本，供后续 Phase 脚本读取）。

---

## Phase 0 结论

以上各项均已从论文 Section 5/Appendix 描述 + 本仓库/官方仓库代码交叉核实，
**未发现需要用户拍板决定的歧义项**——4 个主实验 yaml 与 Table 1 描述的
Setting1-4 client 组合完全一致，family-loop 执行模型（初始化/重置/隔离/
终止条件）已有运行时断言强制保证，唯一新发现的"文档字段未接线"问题
（`sync_schedule`/`merger_mode`）不影响当前实验的有效性，已如实记录。

Phase 0 完成。等待用户确认后再进入 Phase 1（3-family Sanity Validation，
Setting1 配置，Qwen3.6-Plus + Claude Code，federated=OFF）。
