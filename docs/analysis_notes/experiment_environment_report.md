# Experiment Environment Report — Phase 0（实验环境冻结）

对应任务：FederatedSkill 论文（arXiv:2606.03143）Section 5 实验复现，
Phase 0（只读环境核查，未运行任何实验，不修改任何算法代码）。

生成时间：2026-07-20
仓库：`d:\pythonlesson\考核题目\FederatedSkill-Reproduction\`

---

## 1. Benchmark Mapping（SkillFlow / 20 Task Families）

- **Benchmark**：SkillFlow（`benchmark/family.py` + `benchmark/families/*.json`）。
- **Family 数量**：磁盘上共 25 个 family JSON，其中 **20 个是论文官方 SkillFlow
  family**，另 5 个（`data_cleaning`/`data_transformation`/
  `document_processing`/`financial_analysis`/`report_generation`）是本项目
  自建的 legacy/工程测试 family，**不属于论文 benchmark**，由
  `evaluation.paper_export.LEGACY_ENGINEERING_FAMILY_IDS` 常量标记排除。
- **过滤开关**：`experiments/run_experiment.py::_apply_paper_benchmark_scope()`
  读取配置项 `paper_benchmark_only`；4 个主实验配置文件均已设为 `true`，
  过滤后强制断言恰好剩余 20 个（数量对不上会直接 `raise ValueError`，不
  静默跑一个数量不确定的集合）。
- **实验单位**：按 family 循环（`loop_over_families: true` +
  `sampler: family_curriculum`），每个 family 独立跑一遍、从空技能库开始，
  不打平成任务池——对应论文 Table 1 每行同时列出多个 backbone 在同一批
  family 上的成功率这一结构。
- **Round 数**：`rounds_per_family_mode`：
  - `"family_length"`（4 个主配置默认值）：round 数 = 该 family 自身任务数
    （8 或 9，与论文 Table 6 一致）。
  - `"fixed_cap"`：显式工程覆盖，用于快速验证（如 Phase1 sanity check），
    会打印 WARNING 说明发生了截断，不代表论文协议。

## 2. Agent Initialization（空技能库）

- `client/library.py::SkillLibrary.__init__(root, worker_id)`：只做
  `root.mkdir(parents=True, exist_ok=True)`，**不预置任何技能文件**。
  每个 worker 在每个 family 开始时都是从空目录起步（`_run_family_loop()`
  为每个 family 重新构造 library root），符合论文"empty skill library"
  初始化要求。

## 3. Model / Provider Mapping

| 论文模型 | Provider | 环境变量（专属，已隔离） | 调用端点 |
|---|---|---|---|
| Qwen3.6-Plus | DashScope（兼容路由） | `QWEN_DASHSCOPE_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1`（OpenAI-compatible，规避 Anthropic-SDK ASCII 编码 bug） |
| GLM-5 | DashScope（兼容路由，非智谱原生端点） | `GLM_DASHSCOPE_API_KEY` | 同上兼容端点 |
| Kimi-K2.5 | Moonshot 官方 API | `MOONSHOT_API_KEY` | Moonshot 原生端点 |
| Claude Code CLI | 第三方网关（`ANTHROPIC_BASE_URL`） | `ANTHROPIC_AUTH_TOKEN` | 由 `harness/claude_code_harness.py` 子进程直接继承环境变量，不经过本仓库任何 Python API key 读取路径 |

- 已用 grep 核实 `llm/providers.py::resolve_provider_for_model()` 严格按
  模型名分流到 `dashscope_qwen_*` / `dashscope_glm_*` / `moonshot_*`，
  三者环境变量**完全独立**（Provider Key Isolation Fix，已在更早会话完成），
  未识别的模型名不再静默兜底。
- `.env`（已在 `.gitignore` 排除）已配置好上述全部变量；`.env.example`
  仅含占位符 + 详细申请/用途说明，不含真实密钥。
- 代码中未发现硬编码 key、未发现日志打印 token 的路径。

## 4. Agent Harness Mapping

| Setting | u0 | u1 | u2 | Server |
|---|---|---|---|---|
| Setting1 (SE) | Qwen3.6-Plus + claude-code | — | — | 关闭（`federated: false`） |
| Setting2 (Homo Fed) | GLM-5 + claude-code | GLM-5 + claude-code | GLM-5 + claude-code | GLM-5 |
| Setting3 (Hetero Backbone) | Qwen3.6-Plus + claude-code | GLM-5 + claude-code | Kimi-K2.5 + claude-code | GLM-5 |
| Setting4 (Full Hetero) | Qwen3.6-Plus + qwen-code | GLM-5 + claude-code | Kimi-K2.5 + kimi-cli | GLM-5 |

对应配置文件：`experiments/configs/{setting_se, setting_homo_fed,
setting_hetero_backbone, setting_full_hetero}.yaml`（已用 read_file 逐一核对
worker 列表，与上表一致）。

## 5. Metric Mapping（Evaluation）

| 论文指标 | 代码实现 |
|---|---|
| Success Rate | `evaluation/metrics.py::PaperMetrics.success_rate()`（按 reward 序列计算），`compute_all()` 汇总入 `round_*_summary.json` |
| Capability Improvement | `server/capability.py::CapabilityTracker`（真实 C^t 矩阵，由 Stage1 LLM 输出驱动）+ `evaluation/capability_tracker.py::CapabilityEvolutionTracker`（coverage_ratio/history/to_csv，可视化与 CSV 导出） |
| Skill Evolution | `evaluation/metrics.py::skill_growth()`（library_size before→after）+ `evaluation/audit_trace.py::AuditTraceRecorder`（evolution_trace.jsonl，逐条 before/after hash+diff+reason）+ `evaluation/transfer_trace.py`（跨 client transfer 记录） |
| （附加）Cost / Privacy | `evaluation/cost_accounting.py`、`evaluation/privacy.py`（论文附加指标，非 Table1 主指标） |

## 6. 结论

- Benchmark/Agent 初始化/Model-Provider 隔离/Harness 映射/评估指标 五项
  全部已在仓库现有代码中真实落地，**Phase 0 未发现需要修改代码的问题**。
- 未运行任何实验、未发起任何真实 API 调用。
- 唯一需要用户关注的问题：本次对话中用户直接粘贴的真实 API Key/Token
  明文——已拒绝写入 `.env.example`，建议尽快轮换。

## 7. 补充审计（用户提供更详细审计清单后，2026-07-20 追加核实）

| 检查项 | Paper value | Current value | Match |
|---|---|---|---|
| Task families 数量 | 20 | 20（另有 5 个自建 legacy family 已被 `LEGACY_ENGINEERING_FAMILY_IDS` 排除） | ✅ |
| Family 内任务顺序 | sequential，禁止 shuffle | `TaskFamily.__init__` 按 `difficulty` 升序固定排序存储；`FamilyCurriculumSampler.sample()` 按 `round_idx+1` 递增取任务；全仓库 grep `shuffle` 命中的都是任务数据文件业务代码字符串，采样器本身无随机/打乱逻辑 | ✅ |
| 跨 family 混合 | 禁止 | `_run_family_loop` 每个 family 单独构造只含该 family 的 sampler，并有运行时断言 `assert set(sampler._families.keys()) == {family_id}` | ✅ |
| 更新粒度 | 论文 round = 单任务 solve→trajectory→reward→skill update | `baseline.py`/`federated.py::_run_round(round_idx)`：每轮采样 1 个 task→执行→`distill_patch()`→立即 `apply_patch()`（或送 server）。task-level == round-level，非 family 级批量更新 | ✅ |
| Skill 初始化/污染 | empty library，无残留 | `SkillLibrary.__init__` 只建空目录；`_run_family_loop` 新增运行时断言，family 开始前 `library_root` 若有残留文件直接 `AssertionError`（比论文要求更严格，主动防御，非仅约定） | ✅（更严格） |
| 20 个官方 family 总任务数 | — | 14×8 + 6×9 = **166 个任务**（9 任务 family：DMAIC-Quality-Analysis / Financial-Statement-Rolling / Healthcare-Cost-Benefit-Analysis / Medical-Data-Standardization / Production-Capacity-Planning / Supply-Chain-Replenishment） | 信息性 |

**Phase 划分对齐决策（已征得用户确认）**：采纳用户最新给出的 Phase0→1(SE)→2(Homo)→3(Hetero Backbone)→4(Full Hetero)→Appendix A/B 划分，废弃早前草案中的独立"Sanity Check Phase"。作为 Phase 1 内部的分步执行策略：**先用 3 个 family 做小规模 pipeline 验证（用户已确认选择此选项，而非直接跑全部 20 个 family）**，确认 agent 完成任务/skill 产生/skill 被复用/trajectory 完整 四项无误后，再扩到全部 20 个 family 产出可与论文 Table 1 对比的正式结果。

**STOP — 等待用户确认后执行 Phase 1 第一步（3-family pipeline 验证）。**
