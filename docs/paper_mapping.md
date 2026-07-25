# 论文符号 ↔ 代码映射（Paper Mapping）

本文档把论文《FederatedSkill》中的公式/符号，逐条映射到本复现代码中的具体类/
函数，用于向讲师证明"每个核心概念都有对应实现"，同时保持实现独立于官方仓库。

| 论文符号/概念 | 含义 | 本复现代码 |
|---|---|---|
| $x \in \mathcal{X}$ | 任务空间 | `benchmark.task.Task` |
| $\rho_i$ | Worker i 的 profile（backbone + agent harness） | `core.datatypes.WorkerProfile` |
| $L_i^t$ | Worker i 在第 t 轮的技能库 | `client.library.SkillLibrary` |
| $\tau_i \sim \pi_i(\cdot \mid L_i^t, \rho_i)$ | 客户端 agent 基于技能库+profile 执行任务，生成轨迹 | `client.executor.TaskExecutor.run()` / `executor.skillflow_executor.SkillFlowTaskExecutor.run()` |
| $R_{i,x}(\tau)$ | 验证器对轨迹的执行奖励 | `benchmark.verifier.VerificationResult.reward` |
| $\delta_i$ | Worker i 蒸馏出的补丁（patch） | `core.datatypes.WorkerPatch` |
| $\delta_i = \text{Distill}(\tau_i)$ | 轨迹 -> patch 蒸馏 | `client.distiller.PatchDistiller` |
| Capability Matrix $C^t$ | 服务器侧技能覆盖状态矩阵 | `server.capability.CapabilityTracker` |
| Evolution Plan $P^t$ | 服务器 Stage1 规划输出 | `core.datatypes.EvolutionPlan` / `server.planner.EvolutionPlanner` |
| Stage 2（合并执行） | 按 Evolution Plan 合并多个补丁 | `server.merge.EvolutionExecutor` |
| $L_i^{t+1}$ | 合并后回传给 worker 的新技能库 | `core.datatypes.MergedPatch` + `client.library.SkillLibrary` 应用更新 |
| 联邦轮 t | 一轮完整的 client 执行 + server 规划/合并 | `server.evolution.FederatedServer` / `experiments.federated.FederatedRunner` |
| 通信压缩比（Communication Compression） | patch tokens / trajectory tokens | `evaluation.metrics.FederatedMetrics.compression_ratio()` |
| 隐私增益（Privacy Gain，token 压缩代理，仅作衍生/辅助指标） | 1 − patch tokens / trajectory tokens | `evaluation.metrics.FederatedMetrics.privacy_gain()`（⚠ 代码内文档已声明这是 token 压缩代理指标，非论文 Appendix E 的真实 SELR，报告中不应称其为"隐私增益"） |
| SELR（Appendix E Eq.5，Sensitive Entity Leakage Rate，主指标） | leaked entities / sensitive entities | `evaluation.selr.compute_selr()`（+`extract_sensitive_entities()`/`audit_patch_leakage()`；官方框架代码无参考实现，本函数依据论文文本独立实现，见 [`docs/SIMPLIFICATIONS.md` §6.5](SIMPLIFICATIONS.md#65)） |
| Skill Growth | 技能数量/复用率/成功率随轮次变化 | `evaluation.metrics.FederatedMetrics.skill_growth()` |
| 20 diverse task families（SkillFlow benchmark） | 每 family 内递增难度任务序列 | 自建：`benchmark.family.TaskFamily` + `benchmark/families/*.json`（`benchmark/families/__init__.py` 为薄封装转发层）；真实数据集：`benchmark.skillflow_adapter.loader.load_skillflow_benchmark()` |
| family 内递增难度采样（主实验推荐） | "同一技能递增难度序列"的采样策略 | `benchmark.curriculum.SkillFlowFamilySampler`（`FamilyCurriculumSampler` 别名，纯按 round_idx 前进，语义上对应官方 `TaskPartitioner` 的静态划分精神） |
| Appendix B：`task-update` / `merge-skill-patch` 系统技能 | 官方用 claude-code SKILL.md 驱动的两个系统级技能 | **official experimental prompts are retained as configuration, while algorithmic implementation is independently reproduced.** Prompt 文本（实验条件/Agent instruction）直接保留自官方 [`prompts/stage1_prompt.txt`](../prompts/stage1_prompt.txt)（源自 `task_update_skill/SKILL.md`）、[`prompts/stage2_prompt.txt`](../prompts/stage2_prompt.txt)（源自 `merge_skill/SKILL.md`）；算法实现层（`server.planner.EvolutionPlanner`、`server.merge.EvolutionExecutor`）独立编写，不复制官方 `.py` 源码。`prompts/patch_prompt.txt`（`client.distiller.PatchDistiller` 使用）不属于保留官方原文——官方 patcher 依赖不可见的外部库 `SkillPatchEvolver`，本项目从未接触其提示词原文，因此该文件为自研内容。 |
| 审计决策日志（Appendix B, `DECISIONS.md`） | Stage2 合并时的可审计决策记录 | `server/logging.py::DecisionLogger`（✅ 已实现，写入 `results/<setting>/decisions/<worker_id>/DECISIONS.md`） |
| $\tau_i \sim \pi_i(\cdot \mid L_i^t, \rho_i)$（真实 agent workspace 模式） | Agent Harness 架构：Model -> Agent Framework -> Skill Retrieval -> Tool Calling -> Environment -> Test | Phase12 新增 `executor.agent_executor.AgentWorkspaceExecutor`（组合 `executor.environment.WorkspaceManager` + `executor.runner.CommandRunner` + `executor.trajectory.TrajectoryCollector`） |
| Trajectory 字段：actions / tool_calls / generated_files / exceptions / verification / token_usage | Agent 执行过程的完整记录 | Phase12 新增 `core.datatypes.Trajectory` 附加字段（向后兼容，旧版 executor 产出的 Trajectory 也自动满足） |
| $\rho_i$（agent_harness 字段，claude-code/qwen-code/kimi-cli） | Worker 使用哪种 Agent CLI 框架驱动 | Real CLI Harness Fidelity Fix 新增 `harness.claude_code_harness.ClaudeCodeHarness` / `harness.qwen_code_harness.QwenCodeHarness` / `harness.kimi_cli_harness.KimiCLIHarness`（真实 subprocess 调用对应 CLI 二进制），统一实现 `harness.base_harness.BaseAgentHarness` 接口；`core.datatypes.WorkerProfile.agent_harness` 字段值不变，仅新增按该字段真实路由执行后端的能力（opt-in，默认仍走 `AgentWorkspaceExecutor`） |
| 论文默认的"strict reproduction"执行后端 vs. 调试用 API 直连 | Agent Harness 二选一：真实 CLI subprocess / API 回退 | `harness.factory.get_harness(agent_harness, mode)`：`mode="strict"` -> 具体 CLI Harness；`mode="debug"` -> `harness.api_workspace_harness.APIWorkspaceHarness`（零改动委托给既有 `AgentWorkspaceExecutor`）；`executor.harness_executor.HarnessAwareExecutor` 是新增的 opt-in 路由执行器，需在 `run.py` 传 `--execution-mode cli` 才会启用，默认 `--execution-mode api` 行为与新增前完全一致 |

## 命名对齐说明

真实数据集/官方适配层里的一些命名（如 `partitioner_name`、`sync_schedule_name`、
`merger_name`）来自官方 `skillfl/skillflow_adapter/config.py` 的 `FedJobConfig`。
本复现的联邦调度逻辑更简单（固定 round-robin + 每轮同步），未引入这些可配置项，
因此未在本项目中出现同名字段——如实说明，而非假装已对齐。

## 非论文/官方要求的自建实验性扩展（Paper Fidelity Audit 更正）

以下内容**曾经**在本文档中被错误地列为"论文对应实现"，经 Paper Fidelity Audit
核实后确认：论文原文和官方实现均未要求这些机制，属于早期会话（Phase12）自行
发挥的实验性扩展。为避免继续误导，改列于此单独章节，并已从上方"论文符号 ↔ 代码
映射"主表中移除。详细审计证据见 [`docs/SIMPLIFICATIONS.md` §2.4](SIMPLIFICATIONS.md#24)。

| 自建机制 | 曾经声称对应的论文概念 | 审计结论 |
|---|---|---|
| `benchmark.task.Task.dependencies` + `benchmark.family_sampler.FamilyAwareSampler`（依赖图 + 掌握度门控 + 巩固循环） | "同一技能的递增难度序列需要前置任务先被掌握" | ❌ 论文 Section 5.1 只描述 family 数据结构（难度递增+共享技能），未规定采样算法；官方 `skillfl/skillflow_adapter/partitioning.py` 只有无状态的 `TaskPartitioner`（RoundRobin/Block/Replicate/Random），无依赖判定/掌握度/巩固循环。**未被任何真实实验入口引用，不影响实验结果**，保留在 `benchmark/` 层作为隔离的实验性扩展，不建议在正式复现报告中声称其"复现了论文机制"。Official Implementation Alignment Audit 本轮已把内部依赖判定拆分为 `benchmark/dependencies/`、掌握度/循环状态拆分为 `benchmark/curriculum_state/`，公开 API 不变，仅做代码结构解耦。 |
| `benchmark.sampler.DifficultyAwareSampler`（随轮次动态收紧难度上限） | "课程学习式难度递进" | ❌ 官方 `TaskPartitioner` 无此机制，调度阈值为本复现经验值。确认所有 Setting1-4 主实验配置均未使用；Official Implementation Alignment Audit 本轮加上 `ABLATION_ONLY=True` 标记与 `main_trainer.py` 运行时守卫（需显式 `ablation: true`），未声明时自动降级为 random。 |

完整的 6 方面（benchmark 构建 / family 组织 / 采样器 / verifier 协议 / 评估指标 /
agent harness）证据导向对比报告，见 `scripts/compare_official_protocol.py`
（运行 `python scripts/compare_official_protocol.py`）。

：真实实验阶段新增映射

| 论文符号/概念 | 含义 | 本复现代码 |
|---|---|---|
| SkillFlow benchmark 官方数据集接入 | task.toml / instruction.md / environment / tests 目录 -> Task/TaskFamily | `benchmark.skillflow_adapter.download`（文件名别名，转发 `downloader.py` 已测试实现，不重复造轮子）+ 既有 `parser.py`/`converter.py`/`loader.py` |
| synthetic benchmark fallback | 官方数据集不可用时仍可跑通实验 | `benchmark.family.load_all_families()`（自建 25 个 family，独立于 skillflow_adapter，见 `tests/test_real_skillflow_loader.py::TestSyntheticFallbackPreserved`） |
| 统一 LLM 调用接口 `generate(model, prompt, json_mode)` | Section 4.1.2：Worker backbone 可插拔（Qwen/GLM/Kimi/Claude 等异构 backbone） | `llm.generate.generate()` + `GenerateResult`（薄封装，路由复用 `llm.providers.resolve_provider_for_model()`，实际调用复用 `llm.backbone.LLMBackbone`，不重构任一已测试模块） |
| token 统计（input/output tokens、cost、latency） | Table 1/相关成本指标的原始来源 | `llm.generate.GenerateResult`（`input_tokens`/`output_tokens`/`cost`/`latency` 字段，来自 `BackboneCallResult.prompt_tokens/completion_tokens/cost_usd` + 本模块内 `time.monotonic()` 测量的 wall-clock latency） |
| Setting 1-4 统一实验入口（Algorithm 1 对应的实验协议） | Section 5：Self-Evolve / Homogeneous Fed / Heterogeneous Backbone / Full Heterogeneity | `experiments.runner.ExperimentRunner`（门面类，`for_setting(1..4)` 直接映射到既有 `experiments/configs/setting_se.yaml` 等 4 个文件，不新建重复配置；核心执行逻辑仍由已测试的 `experiments.run_experiment.run_experiment()` 承担） |
| Capability Matrix $C^t$ 跨轮演化 | Section 4.2.1：covered/absorbing/broken/gap 状态随轮次演化，对应论文能力覆盖率曲线 | `evaluation.capability_tracker.CapabilityEvolutionTracker`（历史记录器，输入为 `server.capability.CapabilityTracker.to_capability_matrix()` 每轮快照，输出 `to_csv()`/`per_worker_to_csv()` capability evolution CSV；与 `server.capability.CapabilityTracker`——单轮当前状态追踪器——分工明确，互不重复） |
| Appendix C 统一算力成本核算（client_execution/patch_distiller/stage1_planner/stage2_merge 四个 LLM 调用环节） | Appendix C：论文成本曲线需覆盖 client + server 两侧全部 LLM 调用，而非只算 client 执行任务这一个环节 | `evaluation.cost_accounting.CostAccountant`（`LLMCallCostRecord` 按 component 分组求和 client_cost/server_cost/total_cost，接入 `client.distiller.PatchDistiller`/`server.planner.EvolutionPlanner`/`server.merge.EvolutionExecutor`，与 `DecisionLogger`/`AuditTraceRecorder` 同构的可选旁路记录点，`FederatedRunner` 落盘 `cost_ledger.jsonl` + 注入 `paper_export.py` cost.csv 的 `client_cost_usd`/`server_cost_usd`/`total_cost_usd_unified` 三列） |
| Appendix C/E 通信字节审计（patch bytes / library snapshot bytes / trajectory bytes 隐私确认） | Appendix C 通信成本 + Appendix E "上传 patch 而非轨迹以保护隐私" | `evaluation.cost_accounting.CommunicationAuditor`（`measure_patch_bytes`/`measure_snapshot_bytes` 测量真实跨 client→server 边界传输的两个对象；`trajectory_bytes` 在 `CommunicationAuditRecord` 中硬编码为 0——结构性隐私保证，因为 `Trajectory` 从未作为参数传给 `FederatedServer.run_round()`；`trajectory_bytes_if_transmitted` 为可选假设性参考值，落盘 `communication_audit.jsonl`） |

### Phase13 未实现的建议项（剩余差距，非 5 项硬性任务范围）

以下为用户消息中提及但未被列为 5 项编号任务的建议性内容，本轮**有意未实现**，
留作后续工作，如实列出而非静默省略：

- `benchmark/adaptive_sampler.py`（基于 Capability Matrix 动态调整采样难度/顺序）
- `core.datatypes.Trajectory` 新增 `used_skills` / `environment_snapshot` 字段
- `results/main_results.csv` / `ablation_results.csv` / `cost_results.csv` / `privacy_results.csv` 等论文级汇总表（当前 `evaluation.results_exporter.ResultsExporter` 已支持导出单次实验的 CSV/图表，但尚未有一个跨多个 setting/ablation 的汇总脚本）

已完成（从本节移除）：~~Stage1/Stage2 prompt 格式的专项校验测试~~——
见 `tests/test_prompts.py`（Official Prompt Retention 重构会话新增，验证
`prompts/{stage1,stage2,patch}_prompt.txt` 占位符完整性 + 三个 Builder 加载后
仍能正常 format）。

## Phase15：Reproduction Fidelity Audit（新增，交叉引用）

本轮"paper-faithful-final-fix"分支的完整审计结论、P0/P1 清单和评分见
[`docs/audit_report_v1.md`](audit_report_v1.md)；逐 Phase 的具体代码改动
（谁改了什么、为什么、如何验证）见 [`docs/reproduction_changes.md`](reproduction_changes.md)。
本节仅补充两条新发现的符号↔代码映射，不改动上方历史表格：

| 论文符号/概念 | 含义 | 本复现代码 | 审计状态 |
|---|---|---|---|
| D_i（client i 的任务分布，需 D1≠D2≠D3 才能验证 cross-client skill transfer） | Setting3/4 各 client 应分到不同任务子集 | `experiments.run_experiment._build_sampler()` + `benchmark.sampler.HeterogeneousSampler` | ✅ 已修复（`reproduction_changes.md` Phase1-3）：`sampler: "heterogeneous"` 已识别并 fail-loud 校验；已用真实 `benchmark/families/` 数据端到端验证 Setting3/4 三个 worker 分到不同 family（D1≠D2≠D3） |
| M_low^t 按 ρ_i 分桶（同 backbone+harness 的 client 共享低层记忆） | Section 4.2.1 | `core.datatypes.WorkerProfile.profile_hash`（复用已有 computed field 作为 ρ_i 等价类键，未新增 `memory_key()`） + `server.memory.EvolutionMemoryStore` | ✅ 已修复（`reproduction_changes.md` Phase4）：`_low` 按 `profile_hash` 分桶，`tests/test_memory_fidelity.py` 验证 Setting2 风格 3 worker→1 共享桶，Setting3 风格 3 worker→3 独立桶 |
