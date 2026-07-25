# 简化点清单（Known Simplifications）

本文件是对整个 FederatedSkill 复现代码库的**全面审计结果**，按模块列出所有相对论文
《FederatedSkill: Federated Learning for Agentic Skill Evolution》（arXiv:2606.03143）
"简化"或"不完全一致"的地方。与 [README.md](../README.md) §七"已知局限"不同，本文件
**不局限于 6 条**，而是覆盖全仓库、按模块组织，供撰写复现报告 / 课程答辩时逐条核对。

> 图例：✅ 已修正（本轮代码修正已解决）／⚠️ 仍是简化（已知，未修正，原因见说明）／
> ℹ️ 设计选择（不是 bug，是有意的架构决策，附理由）

---

## 目录

1. [评估指标类简化](#1-评估指标类简化)
2. [Benchmark / 任务集类简化](#2-benchmark--任务集类简化)
3. [服务器演化流程类简化](#3-服务器演化流程类简化)
4. [客户端蒸馏类简化](#4-客户端蒸馏类简化)
5. [LLM 路由与 Provider 类问题（本轮已修正）](#5-llm-路由与-provider-类问题本轮已修正)
6. [与官方实现的架构性差异](#6-与官方实现的架构性差异)
7. [测试基础设施相关的设计选择（非简化）](#7-测试基础设施相关的设计选择非简化)
8. [尚未覆盖 / 明确标注为 TODO 的部分](#8-尚未覆盖--明确标注为-todo-的部分)

---

## 1. 评估指标类简化

### 1.1 ⚠️ `privacy_gain()` 不是论文的真正 SELR（Appendix E, Eq.5）

- **位置**：`evaluation/metrics.py::FederatedMetrics.privacy_gain()`
- **论文定义**：SELR(C) = 敏感实体泄漏比例（Eq.5），需要 LLM/NER 实体抽取 + 三级敏感度打标签
  + strict-AND（子串匹配 或 双 LLM 语义裁判）判定。
- **本仓库实现**：`(trajectory_tokens - patch_tokens) / trajectory_tokens`，与
  `compression_ratio()` 代数上完全等价，是 token 数量比例，**不涉及内容语义**。
- **完整对照表和 Table 8 复现数据**：见 [README.md §7.1](../README.md#71-privacy_gain-与论文真正的-selr-指标appendix-e-eq5--详细差异对照)（已有详尽推导，此处不重复）。
- **✅ 本轮部分修正**：新增 `FederatedMetrics.sensitive_entity_leakage_rate(source_text, target_text)`，
  用正则实体抽取（task_id/IP/邮箱/电话/凭据/绝对路径/敏感标记词）+ 精确子串匹配，
  给出一个**实体级**（而非 token 级）的 SELR 近似值。
- **仍然存在的差距**（该方法 docstring 内已注明）：
  1. 固定正则模式 ≠ 论文的 LLM 实体抽取 + `sensitive`/`task_necessary`/`neutral` 三级分类；
  2. 精确子串匹配 ≠ 论文的 strict-AND 语义裁判，会**低估**改写/同义替换类语义泄漏；
  3. 未实现 Wilson 置信区间、未做跨家族负对照、未做 PII 金丝雀注入测试（论文 Table 9）。
- **结论**：两个方法（`privacy_gain()` 与 `sensitive_entity_leakage_rate()`）的输出都**不能**
  直接与论文 Table 8 / Figure 5 / Figure 6 / Table 9 的百分比做定量对比，只能用于同一仓库内的
  相对比较（例如不同净化策略下泄漏程度的相对变化）。
- **✅ 已修正（最终论文一致性收口 Priority 3，[ENGINEERING] 文档修正）**：
  `evaluation/results_exporter.py::_write_privacy_csv()`（实际导出 `privacy.csv` /
  对应论文 Table 8 的那份文件）此前的 docstring 遗留一句过期说明"本实现用
  compression_ratio 代理"——但代码实际读取的是 `RoundRecord.selr` 字段，来自
  `evaluation/selr.py::compute_selr_from_texts()`（论文 Eq.(5) 的真实实体级实现，
  已在 `experiments/federated.py`/`baseline.py` 每轮结束时计算写入），**不是**
  `privacy_gain()` 的 token 压缩比代理。该 docstring 已更正，避免"代码其实是对的，
  文档却说反了"造成的 naming/理解混淆；`privacy_gain()` 本身的定位（仅供内部参考，
  从未写入 privacy.csv）不变，`compute_all()` 汇总字典里两者继续并存
  （`"privacy_gain"` 明确标注"论文 Appendix E（代理）"，`"mean_selr"` 才是真实值），
  未做任何接口改动。

### 1.2 ⚠️ 无统计显著性检验（对照论文 Appendix D）

- **位置**：`evaluation/evaluator.py`, `evaluation/metrics.py`
- **论文**：Appendix D 对 5 个代表性 family 做了多次独立 SE 基线运行（Qwen×4、GLM×3、
  Kimi×2），给出 95% bootstrap 置信区间（Pooled +14.35pp, CI [+9.17, +19.62], W/T/L=12/3/0，
  Table 7）。
- **本仓库**：`ExperimentEvaluator` 只聚合单次运行的指标，没有多次重复运行的采样、
  bootstrap 重采样或置信区间计算逻辑。
- **影响**：报告中给出的 success rate 提升数字是**单次运行的点估计**，不能像论文一样
  声称"统计显著"，写报告时应避免与论文表格做逐点显著性对比，只能做数量级/趋势对比。
- **未修正原因**：需要新增独立的多次重复实验编排（重复 N 次 × 记录种子）与 bootstrap/
  Wilson 区间计算模块，工作量超出"修 bug"范畴，属于新功能开发，留待后续迭代。

### 1.3 ℹ️ `family_success_curve()` 是本复现新增的非论文公式

- **位置**：`evaluation/metrics.py::FederatedMetrics.family_success_curve()`
- **说明**：代码 docstring 已明确标注"这不是论文原文定义的公式"，是为验证 SkillFlow
  风格 lifelong family benchmark（`benchmark/family.py`, `curriculum.py`）新增的辅助
  可视化指标，独立于 `compute_all()` / `ExperimentEvaluator`，不影响论文对照指标。

---

## 2. Benchmark / 任务集类简化

### 2.1 ✅ 20 个真实 SkillFlow family 的数据缺口已解决（真实数据→Verifier→Reward 全链路打通）

- **位置**：`scripts/download_skillflow_dataset.py`（未修改，仅执行）、
  `benchmark/skillflow_adapter/{downloader,parser,converter,loader}.py`、
  `benchmark/families/*.json`（20 个真实 family）、`benchmark/family.py::load_family()`、
  `executor/router_executor.py`、`executor/skillflow_executor.py`、
  `benchmark/verifier.py::SkillFlowScriptVerifier`
- **✅ 管道 bug 修正（历史遗留，先于本轮完成）**：
  1. `benchmark/family.py::load_family()` 此前把整份 JSON task 字典原样传给
     `Task(**item)`——但 `Task`/`VerificationSpec` 模型里根本没有 `tests`/
     `environment`/`name`/顶层 `timeout_seconds` 这些字段名，Pydantic v2 默认
     `extra="ignore"` 会**静默丢弃**它们。现已新增 `_wire_real_verification()`
     显式把 `tests`→`verification.test_script`、`environment`→`Task.files` 映射进去。
  2. `main_trainer.py::_build_executor()` / `experiments/run_experiment.py::
     run_experiment()` 此前始终构造固定的 `client.executor.TaskExecutor`，从未选用
     `executor/skillflow_executor.py::SkillFlowTaskExecutor`。现已新增
     `executor/router_executor.py::VerificationAwareExecutor`（纯组合/薄封装，
     不修改 `TaskExecutor`/`SkillFlowTaskExecutor` 任何一行代码），按
     `task.verification.type` 正确分派，并接入两个执行入口。
- **✅ 本轮新增：真实数据获取与转换闭环（[ENGINEERING]，均已执行并验证）**：
  1. 运行 `scripts/download_skillflow_dataset.py`（下载逻辑未改动），从
     HuggingFace `zhang-ziao/SkillFlow-Task` 下载真实数据集到
     `benchmark/cache/SkillFlow-Task/`（1.67GB，2656 个文件，20 个 family，
     实际目录结构为 `<repo>/test_tasks/<family_id>/<task_id>/{task.toml,
     instruction.md, environment/, tests/, solution/}`，`downloader.py`
     docstring 里未写 `test_tasks/` 这一层前缀，属于既有文档小误差，未修改
     下载逻辑本身，仅在 `loader.py` 调用处的参数说明里澄清）。
  2. `benchmark/skillflow_adapter/converter.py` 新增 `convert_environment()`/
     `convert_tests()`（从 `to_task()` 中提炼出来，行为不变，产出仍是
     `VerificationSpec(type="skillflow_script", test_script=...)` + `Task.files`）。
  3. `benchmark/skillflow_adapter/loader.py` 新增
     `sync_families_to_benchmark()`：完整闭环 raw dataset → `parser.parse_task_dir()`
     → `converter.to_task()` → 写入 `benchmark/families/<family_id>.json`
     （复用 `family.py::load_family()` 的标准 schema）。已在真实下载数据上完整
     执行：**20 个真实 family、166 个真实任务**全部写入，每个任务的
     `verification.type == "skillflow_script"`、`test_script`/`files` 均为
     数据集里真实存在的内容，不含任何伪造字段。
  4. `benchmark/skillflow_adapter/parser.py` 针对真实数据暴露出的两个问题做了
     针对性修正（同样是"修 bug"而非新算法/新特性）：
     - 每个真实任务的 `environment/` 下都有一个**没有扩展名**的 `Dockerfile`，
       此前用扩展名白名单判定文本/二进制会把它错误归为二进制，导致
       `Task.files` 丢失 Dockerfile 内容。改为"尝试 UTF-8 解码，失败才归为
       二进制"，用真实的可解码性代替不完整的扩展名白名单。
     - `SEC-13F-Financial-Analysis` family 的 `environment/` 下有多份
       300MB+ 的 `.tsv` 原始数据文件，若整份读入内存塞进 `Task.files`
       （最终会被 `json.dump` 进 family JSON）会导致同步耗时暴涨、
       `benchmark/families/*.json` 写入损坏（曾实测触发 JSON 文件写坏/
       进程长时间无响应）。新增 `_MAX_INLINE_TEXT_BYTES = 1MB` 上限，
       超限文件只记录相对路径到 `binary_files`，不内联内容——`Task.files`
       仍保留该任务 `environment/` 下体积较小的文本文件（如 Dockerfile），
       满足"每个真实任务的 environment 非空"这一要求。
     - `loader.py::load_skillflow_family()` 同时修正为跳过真实数据集里
       混入的 2 个非任务子目录（`Compensation-Scenario-Modeling/jobs/`、
       `Supply-Chain-Replenishment/jobs/`，只含官方 Harbor 测试运行留下的
       时间戳日志，没有 `task.toml`），跳过时打印 warning，不中断整个
       family 的解析。
  5. 新增 `tests/test_official_skillflow_dataset.py` 对真实数据做端到端完整性
     校验（family 数 == 20、任务总数 == 166、每个任务的 instruction/
     environment/tests 均非空、verification.type 均为 `"skillflow_script"`），
     全部通过。若真实数据不存在则显式 `SkipTest`（不会伪装成"通过"）。
  6. 新增 `pytest.ini`（`testpaths = tests`）：下载进 `benchmark/cache/` 的真实
     数据集自带 `tests/test_output.py` 等脚本，会被 pytest 默认递归发现规则
     误当作项目单测收集，导致 `pytest -q` 报出上百条无关 collection error。
     显式收窄 `testpaths` 后恢复正常。
- **现状**：这 20 个真实 family 目前会正确地落到 `verification.type ==
  "skillflow_script"` → `SkillFlowScriptVerifier` → 真实执行 `test_script`
  产出的二值 reward（而不再是 §2.1 历史版本记录的 `"none"` → `reward=0.0`
  诚实兜底）。全量 `pytest -q` 回归套件（169 passed / 2 skipped，跳过的 2
  个测试需要真实 LLM API Key，与本节改动无关）验证了这一改动没有破坏既有
  行为。
- **§2.2 的 5 个手写 family 不受影响**：它们的 `"verification"` 字段本来就是
  直接合法的 Task 字段，此前和现在都能正确工作。
- **仍存在的、诚实记录的差异（不在本轮修复范围）**：`SkillFlowScriptVerifier`
  用子进程执行 `test_script`，不是官方论文/Harbor 的每任务独立 Docker
  容器隔离执行（`docker` 类型验证需要 `executor/harbor_adapter.py::
  HarborExecutor` + 真实 Docker 环境，本仓库未接入真实 Docker 依赖）。

### 2.2 ℹ️ 5 个手写 `function_test` 家族是规模简化

- **位置**：`benchmark/families/{data_cleaning, financial_analysis, document_processing,
  data_transformation, report_generation}.json`
- **说明**：这 5 个是手写的纯本地可执行验证家族（`verification.type = "python_test"`
  或 `"output_match"`），主要用于快速本地回归测试（`_test_e2e.py`, `tests/test_benchmark.py`），
  不依赖网络/Docker，覆盖规模远小于论文的 20 个真实 family。真实规模由 §2.1 提到的
  20 个下载 family 补充，合计 25 个，其中 20 个真实 family 的验证链路已打通
  （见 §2.1），接近论文规模。
- **论文实际 20 个 family 名称**（已核实）：Cross-Format-Data-Reconciliation、
  Distribution-Center-Auditing、Document-Fraud-Detection、Embedded-Data-Repair、
  HWPX-Document-Automation、Healthcare-Cost-Benefit-Analysis、Industry-Correlation-Analysis、
  Inventory-&-Finance-Integration、Medical-Data-Standardization、OCR-Data-Extraction、
  Operational-Recovery-Planning、Production-Capacity-Planning、SEC-13F-Financial-Analysis、
  Supply-Chain-Replenishment、PPT-Formatting-Optimization、Compensation-Scenario-Modeling、
  DMAIC-Quality-Analysis、Financial-Statement-Rolling、Sales-Pivot-Analysis、
  Weighted-Risk-Assessment。

### 2.3 ℹ️ `DifficultyAwareSampler` 的难度调度阈值是本复现自定的经验值

- **位置**：`benchmark/sampler.py::DifficultyAwareSampler._DIFFICULTY_SCHEDULE`
- **说明**：`rounds<3→difficulty≤2, rounds<6→difficulty≤3, 其余→difficulty≤5` 这组阈值
  是本复现为了模拟"技能演化后 agent 能处理更难任务"这一课程学习（curriculum）语义
  自行设定的，论文正文没有给出逐轮难度调度的具体阈值公式，因此这组数字不是从论文
  精确复现的，而是符合论文描述精神的合理近似。`eligible = self.tasks`（无满足难度
  条件的任务时的兜底）是纯防御性代码，不影响正常配置下的行为。
- **Official Implementation Alignment Audit（本轮新增）**：官方 `TaskPartitioner`
  系列没有"随轮次动态收紧难度上限"的机制。已将 `DifficultyAwareSampler` 显式标注为
  `ABLATION_ONLY = True`，并在 `main_trainer.py::_build_sampler()` 中加入运行时守卫——
  只有配置显式声明 `ablation: true` 时才允许使用该采样器，否则自动降级为 `random`
  并打印警告。确认 `experiments/configs/*.yaml` 下所有 Setting1-4 主实验配置均未
  使用 `difficulty_aware`，因此这个守卫不影响任何已产出的主实验结果，只是把
  "约定俗成的仅供 ablation 使用"升级为"代码强制"。主实验请使用新增的
  `benchmark.curriculum.SkillFlowFamilySampler`（`FamilyCurriculumSampler` 的别名）。

### 2.4 ⚠️ `FamilyAwareSampler` 的依赖图/掌握度/巩固循环不是论文或官方要求（Paper Fidelity Audit 发现）

- **位置**：`benchmark/family_sampler.py::FamilyAwareSampler`、`benchmark/task.py::Task.dependencies`。
- **背景**：早期会话（Phase12）在 `docs/paper_mapping.md` 中把这套机制标注为
  "对应论文'同一技能递增难度序列需要前置任务先被掌握'"，用户在本轮审计中直接
  质疑这一说法的真实性——**质疑成立**。
- **审计证据**：
  1. 论文 Section 5.1 原文仅为 "20 diverse task families, each containing a
     sequence of tasks of increasing difficulty that all require the SAME
     underlying skill to be progressively evolved"——只描述 benchmark **数据结构**
     （family 内任务难度递增、共享同一技能），完全没有提出"依赖图 + reward>=1.0
     解锁 + 全部解决后重置巩固"这一具体采样算法。
  2. 官方实现 `skillfl/skillflow_adapter/partitioning.py` 的任务分配逻辑是
     `TaskPartitioner`（`RoundRobinPartitioner`/`BlockPartitioner`/
     `ReplicatePartitioner`/`RandomPartitioner`），均为无状态的
     `(tasks, n_workers) -> list[list[task]]` 静态划分函数，默认
     `RoundRobinPartitioner`（见 `config.py` 第 49 行），**没有任何依赖判定、
     掌握度回写或巩固循环的痕迹**。
- **实际影响**：`FamilyAwareSampler` 从未被 `experiments/run_experiment.py::_build_sampler()`
  或 `main_trainer.py::_build_sampler()` 引用（两者只支持
  `random`/`curriculum`/`replicate`/`heterogeneous`/`family_curriculum`/`skillflow_family`），
  因此**不影响任何已产出的实验结果**，只是一个孤立存在、且文档曾经夸大宣传的模块。
- **本轮处理**（遵循"不删除实验性组件，只做隔离标注"原则）：
  - 保留代码和 `tests/test_family_sampler.py` 回归测试（未删除任何测试）。
  - 更正 `Task.dependencies` 和 `FamilyAwareSampler` 的 docstring，明确标注为
    "Phase12 自建实验性扩展，非论文/官方要求"。
  - `docs/paper_mapping.md` 已把这一条从主映射表移到独立的
    "非论文/官方要求的自建实验性扩展" 章节。
  - **Official Implementation Alignment Audit（本轮新增）**：把 `FamilyAwareSampler`
    内部原本内联管理的依赖判定逻辑拆分为独立的纯函数模块 `benchmark/dependencies/`
    （`is_unlocked()`/`eligible_tasks()`），把掌握度集合与巩固循环计数拆分为独立的
    状态容器 `benchmark/curriculum_state/`（`CurriculumState` 类）。`FamilyAwareSampler`
    的公开 API（`sample()`/`record_result()`/`solved_tasks()`/`cycles_completed()`）
    保持不变（`tests/test_family_sampler.py` 未做任何修改即全部通过），
    只是让"采样策略"与"依赖图判定"/"状态管理"三者在代码结构上解耦，
    便于分别审计、分别替换，机制本身仍标注为实验性、非论文/官方要求。
  - 新增 `scripts/paper_fidelity_check.py`，程序化校验
    `core/` 不依赖 `benchmark/`/`experiments/`，且列出所有已知的非核心自建扩展。
- **如果需要更贴近论文/官方语义的采样器**：使用
  `benchmark/curriculum.py::FamilyCurriculumSampler`（纯 `round_idx` 前进，
  无额外发明的依赖/掌握度语义），或官方 `TaskPartitioner` 系列的等价实现。



## 3. 服务器演化流程类简化

### 3.1 ✅ `CapabilityTracker.init_from_patches()` 占位 workflow key（本轮已改进）

- **位置**：`server/capability.py`
- **原问题**：`init_from_patches()` 用 `worker_id` 字面量作为占位 workflow 行 key，
  且这些占位行在 Stage1 成功后不会被自动清理，可能永久残留在矩阵里，污染
  `is_workflow_retired()` 等按 workflow 名称聚合的统计。
- **本轮修正**：新增 `_prune_placeholder_rows()`，在 `update_from_plan_dict()`
  （即 Stage1 成功返回真实 `capability_matrix` 时）被调用后，自动删除所有仍以
  `worker_id` 字面量命名、且未出现在本轮 `plan_matrix` 中的矩阵行。
- **⚠️ 仍然存在的简化**：`_fallback_plan()` 降级路径（Stage1 LLM 调用失败）下，
  矩阵不会被更新为真实 workflow 名，会继续保留占位行直到某一轮 Stage1 成功。
  这是论文设计本身要求 LLM 推断 workflow 语义、无法用本地规则绕过的固有局限，
  不属于可以简单修补的 bug。

### 3.2 ℹ️ `EvolutionPlanner._fallback_plan()` 的降级语义

- **位置**：`server/planner.py`
- **说明**：Stage1 LLM 调用失败或解析失败时，`_fallback_plan()` 返回"矩阵和记忆不变、
  无新 directive"的空规划，不中断整轮实验。这是有意的容错设计（避免单次 LLM 抖动
  导致整个联邦实验崩溃），但代价是：当 LLM 持续失败时，能力矩阵会长期停滞，
  worker 也不会收到新的 Stage2 指令去改进库——这个权衡在 README §7.2 和本文件
  §3.1 中都已如实记录。

### 3.3 ✅ `DecisionLog` 是结构化对象，不是论文 Appendix B 描述的 `DECISIONS.md` 文件（本轮已接线）

- **位置**：`server/merge.py::EvolutionExecutor._parse_merge_output()` /
  `core.datatypes.DecisionLog`（结构不变）；`server/logging.py::DecisionLogger`/
  `DecisionEntry`；`experiments/federated.py::FederatedRunner`
- **说明**：论文官方 `merge-skill-patch` SKILL.md 要求 Stage2 每轮向技能库写入一份
  人类可读的 `DECISIONS.md` 审计日志文件；本仓库把同等信息（action/source_worker_id/
  affected_files/reason/timestamp）建模为 Pydantic `DecisionLog` 结构化对象返回
  （`server/evolution.py::FederatedServer.round_records[i].decision_logs`）。
- **⚠ 原问题（本轮发现）**：负责把 `DecisionLog` 落盘为 `DECISIONS.md`/`memory.md`
  文件的 `server/logging.py::DecisionLogger` 早已完整实现（含单测未覆盖但逻辑完整
  的 `log()`/`log_merge_result()`/`set_worker_memory()`/`flush_all()`），但在本轮
  排查前**从未被任何真实调用方实例化过**——`experiments/federated.py::
  FederatedRunner` 从不知道它的存在，导致论文要求的可审计决策日志实际上从未
  产出过任何文件，只停留在内存里的 `DecisionLog` 对象。
- **✅ 本轮已修正（最终论文一致性收口 Priority 2，[ENGINEERING] 纯接线，
  `DecisionLog` 内部结构完全不变）**：
  1. `FederatedRunner.__init__` 新增可选参数 `output_dir`；提供时创建
     `DecisionLogger(output_dir)`，为 `None`（默认）时跳过，向后兼容旧调用方/
     已有测试。
  2. 新增模块级纯函数 `_decision_log_to_entries(log: DecisionLog) ->
     list[DecisionEntry]`，把 `PaperMergeAction`（ABSORB/REPAIR/REFACTOR）/
     `SkipUpdate.NO_UPDATE` 映射成 `DecisionEntry.action`（replace/modify/keep），
     只影响审计文本展示，不反向影响任何合并决策逻辑。
  3. `_run_round()` 每轮把 `server.round_records[-1].decision_logs` 转换后写入
     `DecisionLogger`；`run()` 结束时把 `server.memory_store.get_worker_memory_text()`
     写成每个 worker 的 `memory.md`，再统一 `flush_all()`。
  4. `main_trainer.py`/`experiments/run_experiment.py` 的两处 `FederatedRunner`
     构造调用已补上 `output_dir=output_dir`，因此真实实验运行会在
     `<output_dir>/decisions/<worker_id>/{DECISIONS.md,memory.md}` 产出文件。
  5. 回归测试：`tests/test_decision_logger_wiring.py`（`DecisionLogger` 本身的
     落盘行为、`_decision_log_to_entries()` 的映射正确性、`FederatedRunner`
     按 `output_dir` 是否创建 `DecisionLogger`），此前 `DecisionLogger` 完全没有
     测试覆盖。
- **产物形式仍与论文略有差异（如实记录）**：论文描述的是"库内"（技能库目录下）的
  `DECISIONS.md`；本仓库写在 `<output_dir>/decisions/<worker_id>/` 独立目录下，
  内容语义等价（同样的 path/action/source/reason 字段），只是物理位置不同，
  便于与技能库文件本身解耦、避免污染 `client.library.SkillLibrary` 的文件快照。

### 3.4 ✅ Stage1/Stage2 提示词中的中文内容（本轮新发现并修正）

- **位置**：`server/prompt_builder.py`，提示词文本现已迁至 `prompts/stage1_prompt.txt`、
  `prompts/stage2_prompt.txt`
- **原问题**：`Stage1PromptBuilder`/`Stage2PromptBuilder` 的 `_build_system()` 是英文
  （official experimental prompts are retained as configuration —— 保留自官方
  `task_update_skill/SKILL.md`、`merge-skill-patch/SKILL.md` 的实验性 Prompt），
  但几乎全部 `_section_*()` 方法（构建 user prompt 的部分）都直接把中文文本拼接进
  f-string 中——包括 `_STAGE1_SCHEMA`/`_STAGE2_SCHEMA` 的中文占位符、各段标题
  （"## 本轮 Worker Patches"、"## 演化记忆"等）、字段标签（"奖励："、"摘要："等）、
  `_HARNESS_STYLE_HINTS` 风格提示、`action_guide` 动作说明等，这些内容会被发送给
  服务器 backbone（GLM-5），与已修正的 client 侧 ASCII 编码崩溃 bug（`llm/backbone.py::
  resolve_litellm_model()` + `llm/prompt_builder.py` 翻译，见修正 #14）是完全相同的
  风险类别，但服务器侧此前未被覆盖。
- **本轮修正**：已将上述所有拼接进提示词正文的中文字符串全部翻译为英文
  （模块级 docstring/注释保留中文说明，不影响实际发送的提示词内容）。

---

## 4. 客户端蒸馏类简化

### 4.1 ✅ `PatchDistiller.distill()` 的 `profile=None` 降级路径（本轮已修正）

- **位置**：`client/distiller.py`, `llm/router.py`
- **原问题**：`distill()` 的 docstring 声称"`profile=None` → 从 router 中查
  `trajectory.worker_id`"，但实现里直接 `raise ValueError`，两者不一致。根本原因是
  `BackboneRouter` 当时只存储 `worker_id → LLMBackbone` 的映射，不保留
  `WorkerProfile` 对象。
- **本轮修正**：`BackboneRouter` 新增 `_profiles: dict[str, WorkerProfile]` 存储、
  `register_profile()` / `get_profile()` 方法；`from_profiles()` 现在会在为每个
  worker 注册 backbone 的同时自动注册其 profile。`distill(profile=None)` 现在会先
  尝试 `self._router.get_profile(trajectory.worker_id)`，仍查不到才 `raise ValueError`
  （错误信息已更新为明确指导：显式传入 `profile`，或先调用 `router.register_profile()`）。
  docstring 与实现现已一致。

### 4.2 ℹ️ `_audit_privacy()` 是启发式正则扫描，仅用于告警

- **位置**：`client/distiller.py::PatchDistiller._audit_privacy()`
- **说明**：用固定正则模式（任务 ID 格式、IP 地址、疑似凭据、敏感标记词、绝对文件
  路径）扫描待上传的 upserts 内容，命中时只记录 `logger.warning`，**不拒绝上传**
  （代码注释已明确说明"如需强制执行，可将 logger.warning 改为 raise
  PatchValidationError"）。这不是论文 Appendix E 描述的完整隐私审计流程（论文用
  LLM 抽取器 + 三级敏感度分类 + strict-AND 判定），只是防止明显泄露的轻量级护栏。
  本轮新增的 `FederatedMetrics.sensitive_entity_leakage_rate()`（见 §1.1）复用了
  这里的正则检测思路，扩展为可计数的实体级指标，但依然是同一档次的近似，
  不是论文级别的严格实现。

### 4.3 ℹ️ 失败降级：LLM 调用失败时返回空 patch dict

- **位置**：`client/distiller.py::PatchDistiller.distill()` 的 `_step5_call_llm()` 之后
- **说明**：捕获 `LLMCallError` 后降级返回
  `{"upsert_files": {}, "delete_paths": [], "summary": "[LLM 调用失败: ...]"}`，
  不会让单个 worker 的失败中断整轮实验。这是有意的容错设计，权衡是：该 worker
  本轮不会产生任何真实的技能更新，需要在分析结果时注意区分"reward 低因为任务难"
  和"reward 低因为 LLM 调用失败"两种情况（两者在当前实现里都体现为空/低质量 patch）。

---

## 5. LLM 路由与 Provider 类问题（本轮已修正）

以下问题都属于同一类根因——**DashScope 的 Anthropic 兼容端点在 litellm 内部走
Anthropic SDK 路径处理非 ASCII（中文）system prompt 时会抛出
`UnicodeEncodeError: 'ascii' codec can't encode characters`**——的不同表现形式。
client 侧的这个 bug 在更早的会话中已修正（`llm/backbone.py::resolve_litellm_model()`
的路由推断 + `llm/prompt_builder.py` 翻译），本轮进一步排查并修正了以下残留风险点：

| 位置 | 原问题 | 本轮修正 |
|---|---|---|
| `llm/providers.py::resolve_provider_for_model()` | 关键词兜底分支 `"qwen"/"glm"` 仍返回 `PROVIDERS["dashscope_anthropic"]`，函数末尾的通用默认兜底同样如此 | 均改为 `PROVIDERS["dashscope_openai"]` |
| `llm/providers.py::make_worker_profile()` | `MODEL_TO_PROVIDER.get(model, "dashscope_anthropic")` 默认值同样指向风险端点 | 默认值改为 `"dashscope_openai"` |
| `llm/providers.py::MODEL_TO_PROVIDER["glm-4"]` | 精确匹配表里 `"glm-4"` 这一项未跟随 `MODEL_GLM`（`"glm-5"`）一起被修正，仍指向 `dashscope_anthropic`，与其余条目不一致 | 改为 `"dashscope_openai"` |
| `llm/providers.py::DEFAULT_SERVER_PROVIDER` | GLM-5 server backbone 的默认 provider 仍是 `dashscope_anthropic`；结合 §3.4 发现的 server 侧中文提示词内容，这是本轮排查中风险等级最高的一处 | 改为 `dashscope_openai`；与 §3.4 的提示词翻译共同构成"翻译 + 路由"双重防护 |
| `server/prompt_builder.py` | 见 §3.4 | 见 §3.4 |

**为什么即使翻译了提示词模板，仍要同时修正 provider 路由？** 因为 benchmark family
任务描述本身可能含非 ASCII 内容（例如 `HWPX-Document-Automation` family 的任务描述
里包含韩文标签），这些内容会通过 `WorkerPatch.summary` / `upserts` 等字段间接
流入 Stage1/Stage2 的 user prompt，即使提示词模板本身已全英文，也无法保证运行时
拼接进去的任务相关内容一定是纯 ASCII。因此"翻译模板 + 路由到 OpenAI 兼容端点"
是双重防御，缺一都有残余风险。

---

## 6. 与官方实现的架构性差异

### 6.1 ℹ️ 未采用官方 markdown-driven agent skill 机制（Prompt 保留，算法独立实现）

- **论文 Appendix B**：官方实现用 `task-update` / `merge-skill-patch` 两个
  claude-code SKILL.md 驱动的系统级技能来实现蒸馏和合并逻辑。
- **本仓库**：直接用 Python 类实现同等语义——`PatchDistiller` ≈ `task-update`，
  `EvolutionExecutor`（`server/merge.py`）≈ `merge-skill-patch`。
  **official experimental prompts are retained as configuration, while
  algorithmic implementation is independently reproduced.**
  具体而言：Stage1 evolution prompt、Stage2 merge prompt 属于实验配置
  （agent instruction / experimental condition），不属于算法源码，因此其文本内容
  直接保留自官方 `task_update_skill/SKILL.md`、`merge-skill-patch/SKILL.md`，
  存放于 `prompts/stage1_prompt.txt`、`prompts/stage2_prompt.txt`（不再声称为
  “clean-room 还原”——之前的该提法不准确，实际上是保留了大量官方原文表述，
  而非重新改写）。算法实现层（`server/planner.py` 的 Stage1 规划流程、
  `server/merge.py` 的 Stage2 合并执行流程、`server/capability.py` 的能力矩阵维护、
  `server/memory.py` 的两级记忆机制）均为本项目独立实现，不复制官方任何
  `.py` 源码。
- **原因**：课程复现要求不直接复制官方仓库/Harbor 机制的**算法源码**，
  用等价的 Python 实现更便于审计和修改；Prompt 文本本身属于实验条件/Agent
  配置，保留其官方原文有助于保持与论文实验一致。

### 6.2 ℹ️ 联邦调度逻辑比官方 `skillfl/skillflow_adapter` 简单

- **位置**：`experiments/federated.py`
- **说明**：官方 `skillfl/skillflow_adapter/config.py` 的 `FedJobConfig` 支持
  `partitioner_name`、`sync_schedule_name`、`merger_name` 等可配置项；本仓库固定
  用 round-robin 分配 + 每轮同步的调度策略，未引入这些可配置项。`docs/paper_mapping.md`
  "命名对齐说明" 一节已如实说明这一点。

### 6.3 ℹ️ Verifier 协议：subprocess 沙箱 vs 官方 Harbor/Docker 容器（Official Implementation Alignment Audit 新增）

- **官方**：`skillfl/skillflow_adapter/worker_trial.py::WorkerTrialResult`
  （`worker_id, task_name, reward, verifier_passed, trial_dir: Path,
  exception_type, exception_message, extra: dict`）基于 Harbor/Docker 容器
  隔离，`trial_dir` 下有 `agent/`、`verifier/`、`result.json` 子目录。
- **本仓库**：`benchmark/verifier.py::VerificationResult`（`reward, success,
  stdout, stderr, subtest_results, subtest_failures, runtime_seconds,
  exception_info, generated_files`）基于 `subprocess` 子进程隔离，
  reward 语义（0/1 + 部分子测试软得分）与官方一致，但隔离机制不同——
  课程环境不具备 Harbor/Docker 依赖，这是架构级简化，已在
  `benchmark/verifier.py` 文档字符串中披露。

### 6.4 ℹ️ Agent 执行框架：自建 workspace executor vs 官方 claude-code CLI（Official Implementation Alignment Audit 新增）

- **官方**：`skillfl/skillflow_adapter/harbor_bridge.py` 显示官方用外部
  `claude-code` CLI 在 Harbor 容器内执行 agent 任务；框架侧只负责检测
  claude-code 自身 429 限流重试预算耗尽（读取 `trial_dir/agent/claude-code.txt`
  中的 `{"type":"result"}` 标记）并决定是否重试整个 trial，**没有找到正式的
  `AgentConfig` 配置类**。
- **本仓库**：`executor/agent_executor.py::AgentWorkspaceExecutor` 基于
  subprocess 的自定义多文件 LLM 输出型 prompt 驱动，不依赖 claude-code CLI；
  `core.datatypes.WorkerProfile.agent_harness` 字段默认值为字符串
  `"claude-code"`，仅作命名对齐/记录用途，实际执行走的是自建 workspace 执行器。
  这与 §6.1 是同一类课程环境限制（无 Harbor/Docker），非隐瞒。
- **Real CLI Harness Fidelity Fix（本次新增，opt-in，默认行为不变）**：新增
  `harness/` 包（`BaseAgentHarness` 统一接口 + `ClaudeCodeHarness`/
  `QwenCodeHarness`/`KimiCLIHarness` 三个真实 subprocess CLI Harness +
  `APIWorkspaceHarness` 对既有 `AgentWorkspaceExecutor` 的零改动委托封装）
  与 `executor/harness_executor.py::HarnessAwareExecutor`，按
  `profile.agent_harness` 真实 spawn `claude`/`qwen-code`/`kimi` 二进制
  （`claude` 的调用语法已对照同目录官方仓库
  `FederatedSkill-main/FederatedSkill-main/skillfl/skillflow_adapter/merge.py
  ::make_claude_code_subprocess_runner` 核实一致；`qwen-code`/`kimi-cli` 的
  确切 CLI flag 语法官方仓库中未包含对应实现，属于"按 claude-code 语法类推、
  未经官方验证"，已在 `harness/qwen_code_harness.py`/`harness/kimi_cli_harness.py`
  文档字符串中明确披露）。**必须显式传入 `run.py --execution-mode cli`，
  或运行不带 `--mock`/`--dry-run` 的真实实验（此时 `run.py` 会推导默认值
  为 `"cli"`）才会启用**；`run.py --mock`/`--dry-run` 场景推导默认值为
  `"api"`（此前描述的"仅作命名对齐/记录用途"），方便无 CLI 二进制的开发机
  做结构验证。`run.py` 不设单一全局默认值，而是按"是否为真实实验"分场景
  推导并在启动时打印 `execution_mode = ...`，避免"配置文件声明 strict
  但实际跑的是 api"这类复现争议（早期版本曾用单一默认值 `"api"`，已修正）。
  启动前二进制检测见
  `scripts/check_cli_harness.py`（未安装对应 CLI 时抛
  `harness.cli_utils.CLIBinaryNotFoundError`，不做静默降级）。

### 6.6 ✅ Execution-Layer Closed-Loop Fix：强制执行 + 失败原因结构化传递（本次新增）

- **触发问题**（真实 CLI 实验 trajectory 证据，见
  `results_real_family1/20260723T112332Z_Cross-Format-Data-Reconciliation_9a96b4cf/
  .../round_000_.../trajectory.json`）：真实 CLI agent（claude-code）用 Bash
  跑了几个一次性 `python3 -c "..."` 片段把正确答案打印到了 stdout，也单独
  Write 了一个 `solution.py`，但**从未真正执行过它、也从未把结果写入 verifier
  要求的确切输出路径**，就在最终消息里自称"The solution works correctly"
  结束了会话，导致 verifier 报 `AssertionError: Output file not found ...`，
  reward=0；同时 `client/distiller.py`/`llm/prompt_builder.py` 当时只把
  `verification_failures`（短列表）和 `final_agent_message`（chat 文本）喂给
  蒸馏 LLM，未展示原始 `verifier_output`，导致蒸馏器曾把失败误诊断为"退出码"
  问题而非"输出文件缺失"问题。
- **完整迁移审查与官方对照**：见
  `official_component_mapping.md`（Task Runner/Harbor Bridge/Verifier/Task
  Metadata Parser/Trajectory Logging 五张官方↔本仓库映射表；明确排除联邦
  算法/patch evolution/aggregation）。
- **修复**（均为向后兼容新增字段/步骤，未重构/未改变既有 executor 行为）：
  1. `harness/base_harness.py::BaseAgentHarness._force_execute_solution()`——
     在 `execute_task()` 之后、`_verify()` 之前，强制 subprocess 执行一次生成
     的 `.py` 主文件（cwd=workspace.path，90s 超时），并把工作区最新内容重新
     同步进 `exec_result.files`，不依赖 agent 自我声明成功。
  2. `core/datatypes.py::Trajectory.execution_logs`/`failure_reason`（后者
     由 `_derive_failure_reason()` 在 `_sync_derived_fields()` 里自动派生，
     优先级 `exception_info` > 命名子测试失败列表 > 原始 verifier 输出 >
     兜底提示，**绝不读取 `final_message`**）；`TrialOutcome.verifier_feedback`/
     `failure_reason` 由 `client/distiller.py::_step3_outcome()` 透传。
  3. `llm/prompt_builder.py::_section_trial_outcome()` 把 failure_reason/
     verifier_feedback 独立展示在 agent chat 文本**之前**，并明确提示 LLM
     "agent 可能自称成功但 verifier 不认可，不要采信 chat 文本做诊断"；
     `_section_instructions()` 失败分支要求生成的 SKILL.md 含
     `## Failure Cause`/`## Future Prevention Rule`/`## Verification Procedure`
     三段。
  4. `harness/cli_harness_base.py::CLIAgentHarnessBase._VERIFICATION_DISCIPLINE_BLOCK`
     ——在 agent prompt 里显式要求"结束前必须：写入输出文件→实际执行主脚本→
     读回校验→只有通过才能声明完成"，作为强制执行步骤之外的"第一道防线"。
- **测试**：`tests/test_official_alignment_execution_layer.py`（14 个新测试）
  + 全量 `pytest -q` 回归（332 passed，1 个既有已知无关失败，2 skipped，无
  新增回归）。

### 6.7 ⚠️ SELR：官方框架代码中未找到参考实现（Official Implementation Alignment Audit 新增，重要诚实性声明）

- **审计过程**：对官方仓库 `skillfl/` 全部框架代码（含
  `skillflow_adapter/*.py`）执行 `selr|SELR|privacy_gain|sensitive.entity|
  compression_ratio|def verify|class.*Verifier` 正则 grep，命中 20 处，
  **全部来自 `paper_logs/`**（执行日志与 agent 生成的 task 专属
  `verify_*.py` 脚本），**没有一处来自 `skillfl/` 框架级代码本身**。
- **结论**：`evaluation/selr.py::compute_selr()` 是根据论文 Appendix E
  公式（Eq. 5）文本独立实现的，**不能声称"对齐了官方 SELR 实现"**（因为
  官方压根没有框架级参考代码），只能声称"根据论文文本正确实现，且未与
  官方任何代码冲突"。`evaluation/metrics.py::FederatedMetrics.privacy_gain()`
  已在文档字符串中明确降级为"衍生的通信压缩代理指标"，`compute_selr()`
  才是衡量隐私泄露的主指标——这满足了本轮审计对"SELR 是主指标，
  privacy_gain 仅作辅助"的要求。
- **完整的 6 方面对比报告**：见 `scripts/compare_official_protocol.py`
  （运行 `python scripts/compare_official_protocol.py` 输出结构化的
  matched / simplified / experimental extension 三段式报告）。

---

## 7. 测试基础设施相关的设计选择（非简化）

以下几项经审查确认是**有意的接口设计**，不是需要修正的简化或 bug：

- **`executor/mock_executor.py::MockExecutor`**：不调用真实 LLM/subprocess，直接返回
  预设 reward 的 Trajectory，专用于单测/CI 快速验证管线连通性（`_test_e2e.py`,
  `tests/test_executor.py`），不参与真实实验流程，命名和文档都已清晰标注"Mock"。
- **`benchmark/verifier.py::BaseVerifier.verify()` / `SkillFlowScriptVerifier.verify()`
  抛出 `NotImplementedError`**：前者是抽象基类方法（要求子类必须实现），后者是
  故意设计的"接口不匹配"保护——`SkillFlowScriptVerifier` 需要工作区目录而非单段
  生成代码，调用通用接口会得到明确的错误提示，指导调用方改用
  `verify_in_workspace()`。这是清晰的 fail-fast 设计，不是遗漏实现。
- **`tests/test_real_llm_pipeline.py` 等测试默认跑 mock 模式**：通过环境变量控制
  是否启用真实 API 调用（`USE_REAL_API`），默认关闭以避免测试依赖网络/产生费用，
  是标准的测试隔离实践。

---

## 8. 尚未覆盖 / 明确标注为 TODO 的部分

- **`docs/paper_mapping.md`** 中已如实标注：Appendix B 的审计决策日志对应的
  `server/logging.py` 持久化模块（把 `DecisionLog` 序列化为库内 `DECISIONS.md`
  文件，见本文件 §3.3）"Sprint 3 新增（⏳ 待实现）"，本轮修正未涉及此项。
- **`docs/experiment_settings.md`** 中已如实标注：当前配置用 mock/占位 backbone
  跑通闭环（`_test_e2e.py`），真实多 backbone（Qwen/GLM/Kimi）端到端联邦实验的
  完整运行记录（Sprint 2 目标）依赖真实 LLM API Key（本仓库 `.env` 未配置），
  §2.1 提到的 20 个真实 family 验证链路本身已打通，目前受限于 API Key 而非
  数据/验证管道。
- **统计显著性检验模块**（见 §1.2）——需要新增多次重复实验 + bootstrap/Wilson
  置信区间计算，属于新功能，未在本轮修正范围内完成。
- **完整 LLM/NER 驱动的 SELR 审计模块**（见 §1.1）——需要新增独立的实体抽取 +
  三级分类 + strict-AND 语义裁判模块，属于新功能，未在本轮修正范围内完成。

---

## 修正历史（本轮）

| 文件 | 修正内容 |
|---|---|
| `llm/router.py` | 新增 `_profiles` 存储、`register_profile()` / `get_profile()` 方法 |
| `client/distiller.py` | `distill(profile=None)` 现在会尝试从 router 反查 profile，docstring 与实现一致 |
| `llm/providers.py` | `resolve_provider_for_model()` 关键词兜底与默认兜底、`make_worker_profile()` 默认值、`MODEL_TO_PROVIDER["glm-4"]`、`DEFAULT_SERVER_PROVIDER` 均从 `dashscope_anthropic` 改为 `dashscope_openai` |
| `server/capability.py` | 新增 `_prune_placeholder_rows()`，清理 Stage1 成功后残留的 worker_id 占位行 |
| `server/prompt_builder.py` | Stage1/Stage2 所有拼接进提示词正文的中文内容翻译为英文 |
| `evaluation/metrics.py` | 新增 `FederatedMetrics.sensitive_entity_leakage_rate()` 实体级 SELR 近似估算 |
| `README.md` | §7 更新为反映上述修正状态，并新增指向本文件的链接 |
