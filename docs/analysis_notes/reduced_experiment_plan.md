# reduced_experiment_plan.md — Phase 2 之前：缩减规模实验计划

对应任务书要求：在每个实验执行前输出 Experiment Plan 并等待你确认。
本文档一次性给出 Experiment 1-4 的整体规划，**尚未执行任何一个**；
正式启动前会针对当前要跑的那一个再单独复述一次 Plan 并等待你的
"确认执行"指令（逐个确认，不会因为本文档已发出就自动连续跑完 4 个）。

**本文档发出后即 STOP，不会自动开始执行 Experiment 1。**

---

## 0. 前置条件（执行 Experiment 1 之前必须先做，未做）

1. **新增 `family_subset` 配置开关**（纯 additive，见
   [paper_experiment_protocol.md](paper_experiment_protocol.md) §8 设计说明）：
   在 `_apply_paper_benchmark_scope()` 过滤出 20 个官方 family 之后，
   再按 `family_subset: [...]` 列表进一步过滤到 3 个。未设置该字段时
   行为与现状完全一致（不影响任何已有 20-family 完整实验路径）。
2. 该改动需要配套：
   - 1 个新增单元测试（覆盖"设置 subset 后只剩 3 个 family"+"不设置时
     行为不变"两种情况）；
   - 全量 `pytest` 回归（复用当前 286 passed / 2 skipped 基线）。
3. **API Key 现状**：`docs/real_experiment_setup.md`/`docs/reproduction_protocol.md`
   记录 `.env` 里 `DASHSCOPE_KEY`/`MOONSHOT_KEY` 此前为空，此前所有真实
   API 尝试（`results/phase1_sanity_se3_stale_20260721_1150/`、
   `results/phase1_setting4_real3/` 等）**全部因超时/CLI 崩溃而 0 个
   family 成功**，是本会话早些时候修复的 timeout=600s 根因（现已改为
   1800s）。**执行 Experiment 1 前需要你确认当前 `.env` 里的 key 仍然
   有效、且你同意这会产生真实计费调用**——本计划不会替你静默假设 key
   可用。

以上两项在你确认本计划之前均**未实现/未验证**。

---

## 1. 缩减规模总览（4 个 Experiment 共用）

| 项 | 值 |
|---|---|
| Family 数量 | 20 → **3**（Compensation-Scenario-Modeling / Cross-Format-Data-Reconciliation / DMAIC-Quality-Analysis） |
| 选择依据 | 对 20 个官方 family_id 做确定性 `sorted()` 取前 3（与仓库既有 family 顺序机制、官方 README `ls`-序复现脚本一致，非人工挑选） |
| 每 family 任务数 | 8 / 8 / 9 |
| 每 worker 每个 setting 的总任务数 | 25 |
| Round 定义 | 不变，1 round = 1 task（见 `paper_experiment_protocol.md` §4） |
| Agent timeout | 1800s（task.toml 逐任务读取，抽样恒为 1800，见 `runtime_fidelity_report.md` §1） |
| Verifier timeout | 900-1200s（逐 family 不同，task.toml 读取，见 `runtime_fidelity_report.md` §2） |
| Retry policy | `max_retry=0`（Setting1 SE 已对齐官方；Setting2-4 联邦 client-phase retry 现状仍为 2，未改动，见 `runtime_fidelity_report.md` §3） |
| 联邦算法/聚合/技能演化/评测指标 | **不变**，与 20-family 完整实验使用完全相同的代码路径，唯一区别是 family 白名单从 20 缩到 3 |

---

## 2. Experiment 1 — Setting1（Self-Evolution baseline）

| 字段 | 值 |
|---|---|
| Paper Section | Setting1 / SE baseline（对应用户任务书 Experiment 1 描述） |
| Models | Qwen3.6-Plus |
| Harness | Claude Code CLI |
| Federation | 关闭（`federated: false`） |
| Families | Compensation-Scenario-Modeling, Cross-Format-Data-Reconciliation, DMAIC-Quality-Analysis |
| Rounds | 8, 8, 9（= family_length，共 25） |
| Timeout | agent 1800s / verifier 900-1200s（task.toml 逐任务） |
| Retry | max_retry=0 |
| Config 文件 | `experiments/configs/setting_se.yaml`（+ 新增 `family_subset` 字段） |
| Expected artifact | `results/<job>/setting1/{metrics.csv, capability.csv, skill_evolution.csv, skill_growth.csv, success_rate_detail.csv, table1.csv, cost.csv, privacy.csv, families/*/evolution_trace.jsonl}` |
| Expected cost | **无法给出可信数字**——本仓库此前所有真实 API 尝试均未产出 1 次完整成功的 task（见 `docs/reproduction_protocol.md` Sprint4 状态："results/setting1..4/*.csv 目前只有表头"），没有真实 token 用量样本可供外推，若编造具体 $ 数会违反"不得编造数据"的要求。建议：先跑 Experiment 1（单 worker、最小规模）作为**成本标定跑**，用实际 `cost.csv` 输出反推 Experiment 2-4 的量级，而不是本计划阶段猜测。 |
| Expected runtime | 同理无已验证的成功样本；粗略工程量级估计（非实测）：25 个任务 × 单 worker 顺序执行，若每任务耗时集中在 3-15 分钟（介于此前观测到的单任务 46s CLI 探测 与 1800s 硬上限之间），总时长量级约 1.5-6 小时，最坏情形（多任务逼近 1800s 上限）可达 12+ 小时。**这是量级估计，不是承诺值**，实际以 Experiment 1 真实跑出的 `experiment_summary.json::elapsed_seconds` 为准。 |

---

## 3. Experiment 2 — Setting2（Homogeneous Federated）

| 字段 | 值 |
|---|---|
| Paper Section | Setting2 / Homogeneous Fed |
| Models | u0/u1/u2 均 GLM-5；Server GLM-5 |
| Harness | Claude Code CLI（全部 worker） |
| Federation | 开启，`merger_mode: unshared`、`isolated_worker_skills: true`、`sync_schedule: every_task` |
| Families | 同 Experiment 1 的 3 个 |
| Rounds | 8, 8, 9（每 worker 独立跑一遍，共 3×25=75 次 task 执行 + 每次 task 后的 server merge） |
| Timeout / Retry | 同 Experiment 1；联邦 client-phase retry 现状仍为 2（`federated.py`，本轮未改） |
| Config 文件 | `experiments/configs/setting_homo_fed.yaml`（+ `family_subset`） |
| Expected cost / runtime | 同 Experiment 1 的免责声明；量级上约为 Experiment 1 的 **3 倍 task 执行量**（3 个 worker）+ 额外 server merge 调用开销，具体倍数待 Experiment 1 标定后再估算，不在此处编造具体数字 |

---

## 4. Experiment 3 — Setting3（Heterogeneous Backbone）

| 字段 | 值 |
|---|---|
| Paper Section | Setting3 / Hetero Backbone |
| Models | u0 Qwen3.6-Plus / u1 GLM-5 / u2 Kimi-K2.5；Server GLM-5 |
| Harness | Claude Code CLI（全部 worker，backbone 异构但 harness 同构） |
| Federation | 同 Experiment 2 |
| Families / Rounds | 同上 |
| Config 文件 | `experiments/configs/setting_hetero_backbone.yaml`（+ `family_subset`） |
| Expected cost / runtime | 同上免责声明；额外注意 Kimi-K2.5 走 Moonshot 独立 endpoint，若 `MOONSHOT_KEY` 未配置会直接失败（现状见 `docs/real_experiment_setup.md` preflight 示例） |

---

## 5. Experiment 4 — Setting4（Full Heterogeneous）

| 字段 | 值 |
|---|---|
| Paper Section | Setting4 / Full Hetero |
| Models | u0 Qwen3.6-Plus / u1 GLM-5 / u2 Kimi-K2.5；Server GLM-5 |
| Harness | u0 Qwen Code / u1 Claude Code / u2 Kimi CLI（harness 也异构） |
| Federation | 同 Experiment 2 |
| Families / Rounds | 同上 |
| Config 文件 | `experiments/configs/setting_full_hetero.yaml`（+ `family_subset`） |
| Expected cost / runtime | 同上免责声明；此前 `results/phase1_setting4_real3*` 系列的 4 次真实尝试**全部因 CLI 崩溃/认证失败/超时中断**（`qwen-code` returncode 异常退出、GLM endpoint 失败、Qwen 认证失败等），说明 Setting4 是 4 个实验里环境依赖最多、最容易先失败的一个，建议放在 Experiment 1-3 都稳定跑通之后再执行 |

---

## 6. 执行顺序与确认机制

1. 先实现 §0 的 `family_subset` 开关 + 配套单元测试 + 全量回归（仍需你
   确认后才动手，本文档本身不包含代码改动）。
2. 按 Experiment 1→2→3→4 顺序执行，**每个 experiment 开始前**都会：
   - 复述该 experiment 的 Plan（内容与本文档对应小节一致，必要时更新
     Expected cost/runtime 为上一个 experiment 的实测值）；
   - 显式等待你的确认指令；
   - 执行期间不会静默跳到下一个 experiment。
3. 全部 4 个 experiment（或你中途决定终止的那些）完成后，产出最终的
   `reduced_scale_reproduction_report.md`（对比 3-family 缩减结果与
   `paper_logs/` 官方 20-family 结果趋势，不做 20 vs 3 的数值直接对标，
   只做"链路是否跑通/趋势是否合理"层面的定性核对）。

---

## STOP

本文档为 Phase 0 三项交付物的最后一项。**在你确认前：**
- 不会实现 `family_subset` 开关；
- 不会运行 Experiment 1-4 中的任何一个；
- 不会修改任何算法/联邦/评测代码。

等待你的确认或修改意见。
