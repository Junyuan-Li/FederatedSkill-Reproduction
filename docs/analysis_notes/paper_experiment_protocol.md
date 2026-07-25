# paper_experiment_protocol.md — Phase 0：论文实验协议提取

对应任务："Reduced-Scale Reproduction" Phase 0 — Paper Protocol Extraction。
**只读提取 + 与仓库现状核对，未修改任何算法代码，未运行任何实验。**

---

## 0. 证据来源与局限（如实声明）

- **论文正文（PDF）本环境无法直接读取**：本会话环境没有本地论文副本，
  `fetch_webpage` 抓取 `arxiv.org/abs/2606.03143` / `arxiv.org/pdf/2606.03143`
  此前两次均失败（"Failed to extract meaningful content"）。因此本文档
  **不冒充直接引用论文原文字句**，凡涉及"论文 Section 5.x 说了什么"的结论，
  一律标注实际证据来源（官方仓库代码/README/真实运行记录/本仓库既有审计
  文档），不做无依据的转述。
- 可核实的一手证据：
  1. `FederatedSkill-main/FederatedSkill-main/`（官方仓库，含 README.md、
     `configs/1_se_qwen.local.yaml`~`4_fed_hetero_mixed_cli.local.yaml`、
     `skillfl/skillflow_adapter/`、`paper_logs/`——真实已发表实验的运行记录）。
  2. 本仓库既有审计文档（均为之前会话真实核验产出，非本次编造）：
     `docs/reproduction_protocol.md`、`experiment_environment_report.md`
     （含"补充审计"表格，逐项核对 20 family/任务顺序/跨 family 隔离/
     round 粒度/空库初始化）、`docs/paper_mapping.md`。
  3. `benchmark/families/*.json`（25 个 family，20 个真实 SkillFlow +
     5 个本项目自建 legacy family，已用
     `evaluation.paper_export.LEGACY_ENGINEERING_FAMILY_IDS` 标记区分）。
  4. `experiments/configs/setting_{se,homo_fed,hetero_backbone,full_hetero}.yaml`
     （本仓库 Setting1-4 主配置，均已在此前会话逐条核对与官方
     `configs/*.local.yaml` 一致，见各文件顶部 docstring）。

---

## 1. Benchmark

| 项 | 值 | 证据 |
|---|---|---|
| Benchmark 名称 | SkillFlow | 官方 README.md："we release (1) a 20-family task benchmark with verifier scripts" |
| 原始规模 | **20 task families**，共 **166 个任务** | 官方 README.md 明确"20 families used in the paper"；本仓库 `benchmark/families/*.json` 抽样统计确认 14 个 family 各 8 任务 + 6 个 family 各 9 任务 = 166（见 `experiment_environment_report.md` §7 补充审计表） |
| 20 个官方 family 列表 | Compensation-Scenario-Modeling / Cross-Format-Data-Reconciliation / Distribution-Center-Auditing / DMAIC-Quality-Analysis / Document-Fraud-Detection / Embedded-Data-Repair / Financial-Statement-Rolling / Healthcare-Cost-Benefit-Analysis / HWPX-Document-Automation / Industry-Correlation-Analysis / Inventory-&-Finance-Integration / Medical-Data-Standardization / OCR-Data-Extraction / Operational-Recovery-Planning / PPT-Formatting-Optimization / Production-Capacity-Planning / Sales-Pivot-Analysis / SEC-13F-Financial-Analysis / Supply-Chain-Replenishment / Weighted-Risk-Assessment | 本仓库 `benchmark/families/` 目录实际文件列表（`list_dir` 核实，共 25 个 json，減去 5 个自建 legacy：`data_cleaning`/`data_transformation`/`document_processing`/`financial_analysis`/`report_generation`，恰好剩 20 个） |
| 每个 family 内部结构 | `task.toml`（元数据+超时）/ `instruction.md`（agent prompt）/ `environment/`（挂载文件）/ `solution/`（参考解，**不下发给 agent**）/ `tests/`（verifier） | 官方 README.md 目录树示例（`test_tasks/<family>/<task>/...`） |
| 任务顺序来源 | family 内任务按难度升序排列（`task_id` 前缀数字序号，如 `01_orchestra_foundation_model`），本仓库 `TaskFamily.__init__` 按 `difficulty` 字段固定升序存储 | 官方 README："`ALL_TASK_DIFFICULTY_RANKING.json` // round order"（官方每个 family 目录下的任务顺序文件）；本仓库 `experiment_environment_report.md` §7 表格确认"Family 内任务顺序：sequential，禁止 shuffle" |
| **本次缩减规模** | **3 个官方 family**（166 个任务中的 25 个） | 用户本次任务书明确要求："20 SkillFlow families → 3 selected official SkillFlow families" |
| 缩减选择规则 | **只减少 family 数量，不改变任务顺序**；3 个 family 用与仓库既有
  "family 顺序" 机制（`sorted(family_names)`，见 Issue6/`experiment_summary.json::family_order`）**相同的确定性排序**取前 3 个，而非人工挑选/随机抽样 | 用户要求"Only reduce family count. Do not alter task sequence."；本仓库 `experiments/run_experiment.py::_run_family_loop()` 已用 `sorted(families.keys())`；官方 README 复现全部 20 family 的 for 循环用 `for fam in $(ls test_tasks/); do ...`（等价的字典序遍历，两边选择的排序原则一致） |
| **选定的 3 个 family** | 1. `Compensation-Scenario-Modeling`（8 任务）<br>2. `Cross-Format-Data-Reconciliation`（8 任务）<br>3. `DMAIC-Quality-Analysis`（9 任务）<br>共 **25 个任务**（每 setting 每 worker） | 对 20 个官方 family_id 做 Python `sorted()`（区分大小写的 ASCII 序，`DMAIC`的大写`M`排在`Distribution`的小写`i`之前）取前 3 个，此前会话已用真实代码验证过这一顺序（见 `/memories/session/phase1_sanity_rerun.md`） |

---

## 2. Task Execution

| 要求 | 论文/官方协议 | 本仓库现状 |
|---|---|---|
| 每个 family 内任务执行顺序 | **顺序执行，禁止 shuffle** | `FamilyCurriculumSampler.sample()` 按 `round_idx + 1` 递增取任务，无随机逻辑（`experiment_environment_report.md` §7 已核实并标记 ✅） |
| 每个 family 是否完整跑完整个任务序列 | 是 | `rounds_per_family_mode: "family_length"`（4 个主配置文件默认值）：round 数 = 该 family 自身任务数（8 或 9），不做静默截断 |
| 跨 family 是否允许混合/打平 | **不允许** | `_run_family_loop()` 每个 family 单独构造只含该 family 的 sampler，并有运行时断言 `assert set(sampler._families.keys()) == {family_id}`（`experiment_environment_report.md` §7 已核实 ✅） |

---

## 3. Initialization

| 要求 | 论文/官方协议 | 本仓库现状 |
|---|---|---|
| 每个 family 开始前的技能库 | **空**（empty skill library） | `SkillLibrary.__init__(root, worker_id)` 只 `mkdir`，不预置任何技能文件；`_run_family_loop()` 为每个 family 重新构造 library root |
| Client 状态 | 每个 family 开始前重置 | 新 family 循环时重新构造 `FederatedClient`/`SelfEvolutionRunner` 状态（非跨 family 复用同一个对象内部状态） |
| Workspace | 每个 family 开始前干净 | `AgentWorkspaceExecutor`/CLI harness 每个 task 用独立临时工作区（`WorkspaceManager`），不残留上一个 family 的文件 |
| 跨 family 信息泄漏防护 | 不允许残留 | `_run_family_loop()` 有运行时断言：family 开始前若 `library_root` 有残留文件，直接 `AssertionError`（比"约定"更强的主动防御，`experiment_environment_report.md` §7 标注"更严格"）；本次 Issue5（Runtime Protocol Alignment）新增的 `family_failure_cleanup()` 进一步保证异常中断时也会清理残留 |

---

## 4. Round Definition

| 要求 | 论文/官方协议 | 本仓库现状 |
|---|---|---|
| 1 round 对应什么 | **一次任务交互**（task solve → trajectory → reward → skill update） | `baseline.py`/`federated.py::_run_round(round_idx)`：每轮采样 1 个 task → 执行 → `distill_patch()` → 立即 apply（或送 server merge）。task-level == round-level，非 family 级批量更新（`experiment_environment_report.md` §7 已核实 ✅） |
| 总 round 数 | = family 自身任务数（不缩减） | `rounds_per_family_mode: "family_length"`；本次缩减实验：Compensation=8 / Cross-Format=8 / DMAIC=9，与原始 20-family 实验里这 3 个 family 单独的 round 数**完全一致**，缩减只发生在"跑几个 family"，不发生在"每个 family 跑几轮" |

---

## 5. Federation Protocol

| Setting | 联邦 | 依据 |
|---|---|---|
| Setting1（SE） | **关闭**（`federated: false`），单 worker 独立进化 | 官方 README Table："Self-Evolve baseline. Single qwen3.6-plus worker, no federation." |
| Setting2-4 | **开启**，`merger_mode: unshared`（每 worker 私有库，服务端按目标 worker 单独合并）+ `isolated_worker_skills: true` | 官方 `configs/2_fed_3glm_cc.local.yaml` 等文件字段原样；本仓库 `setting_homo_fed.yaml`/`setting_hetero_backbone.yaml`/`setting_full_hetero.yaml` 已逐条核对一致 |
| 聚合频率（aggregation frequency） | `sync_schedule: every_task`（每个任务后同步，peer 在下一轮开始时能看到本轮 patch） | 官方 `configs/2_fed_3glm_cc.local.yaml`："Sync every task so peers see each round's patches at the next round's start."；本仓库 3 个联邦配置文件同字段值一致 |
| Server 更新方式 | Stage1 Planner（生成 evolution directive：repair/absorb/refactor/no_update）→ Stage2 Merger（据 directive 执行合并）；`merger_model: glm-5` | 官方 README Abstract + `configs/*.local.yaml::merger_model: glm-5`；本仓库 `server/planner.py`/`server/merge.py` + 全部 Setting2-4 yaml 的 `server.backbone_model: "glm-5"` 一致 |
| 本次缩减是否改变以上任何一项 | **否** | 用户任务书："federation algorithm/aggregation mechanism ... MUST NOT change"；本仓库 Setting1-4 配置文件的 federated/merger_mode/sync_schedule/server 字段本次**未做任何修改** |

---

## 6. Client / Server 配置（Setting1-4，逐项核对官方一致，本次未改动）

| Setting | u0 | u1 | u2 | Server |
|---|---|---|---|---|
| 1 (SE) | Qwen3.6-Plus + Claude Code | — | — | 关闭 |
| 2 (Homo Fed) | GLM-5 + Claude Code | GLM-5 + Claude Code | GLM-5 + Claude Code | GLM-5 |
| 3 (Hetero Backbone) | Qwen3.6-Plus + Claude Code | GLM-5 + Claude Code | Kimi-K2.5 + Claude Code | GLM-5 |
| 4 (Full Hetero) | Qwen3.6-Plus + Qwen Code | GLM-5 + Claude Code | Kimi-K2.5 + Kimi CLI | GLM-5 |

对应配置文件：`experiments/configs/{setting_se, setting_homo_fed,
setting_hetero_backbone, setting_full_hetero}.yaml`，与用户本次任务书里
Experiment 1-4 的 Configuration 完全一致，**本次缩减实验直接复用这 4 个
文件，不新建配置、不改动其中任何 client/server/harness/federation 字段**
（唯一新增字段是下一节的"family 子集限定"，纯 additive，不影响其余字段）。

---

## 7. Evaluation

| 论文指标 | 本仓库实现 | 输出文件 |
|---|---|---|
| Success Rate | `evaluation/metrics.py::PaperMetrics.success_rate()`（按 reward 序列计算） | `metrics.csv`、`success_rate_detail.csv`、`table1.csv` |
| Capability Improvement | `server/capability.py::CapabilityTracker`（真实 C^t 矩阵）+ `evaluation/capability_tracker.py::CapabilityEvolutionTracker`（coverage_ratio 历史） | `capability.csv`、`capability_matrix.jsonl` |
| Skill Evolution | `evaluation/metrics.py::skill_growth()`（library_size before→after）+ `evaluation/audit_trace.py::AuditTraceRecorder`（`evolution_trace.jsonl`，逐条 before/after hash+diff+reason） | `skill_evolution.csv`、`skill_growth.csv`、`evolution_trace.jsonl` |
| （附加，非论文主指标，不引入新指标口径，只是现有旁路记录） | Cost / Privacy | `cost.csv`、`privacy.csv` |
| 本次是否新增指标 | **否** | 用户要求"Keep original metrics ... Do not introduce new metrics"；以上全部是本仓库既有实现，本次未新增任何指标计算逻辑 |

---

## 8. 需要新增的最小 additive 基础设施（尚未实现，供 Reduced Experiment Plan 参考）

现状：`experiments/run_experiment.py::_apply_paper_benchmark_scope()` 目前
只支持"全部 20 个官方 family / 全部 25 个（含 legacy）"二选一，**没有
"只跑其中 N 个 family"的开关**。要满足"20→3"的缩减，需要新增一个可选
配置项（暂定名 `family_subset: [<family_id>, ...]`），在
`_apply_paper_benchmark_scope()` 过滤出 20 个官方 family **之后**，再按
该列表进一步过滤（若配置未设置该字段，行为与现在完全一致，向后兼容）。
**本次任务书要求"Do NOT run experiments... Then STOP"，因此这项代码改动
本报告只做设计说明，不在本轮实现**，待下方 Reduced Experiment Plan 得到
你的确认后再实现+测试。

---

## 结论

- 除"family 数量"这一项被用户明确批准缩减外，Benchmark/Task Execution/
  Initialization/Round Definition/Federation Protocol/Client-Server 配置/
  Evaluation 七个维度，本仓库现状与官方协议**逐项核对一致**（其中 5 项
  在更早会话已完成核实，本次只是重新汇总整理进本文档，未重新验证出新
  偏差）。
- 唯一待实现的代码改动是"family 子集过滤"开关（纯 additive，不影响
  `paper_benchmark_only=true` 时的 20-family 完整模式），将在你确认
  Reduced Experiment Plan 后再实现。
- 本文档之外，未修改任何代码，未运行任何实验。
