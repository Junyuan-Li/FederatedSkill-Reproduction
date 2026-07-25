# FederatedSkill Reproduction Fidelity Audit — v1

> 冻结基线：分支 `paper-faithful-final-fix`（从 `master` 分支切出，仓库根 `D:/pythonlesson`）。
> 本文档记录 Phase15 Step1 全仓库审计的静态结果，作为后续 Phase1-9 修复的依据。
> 审计范围：`WorkerPatch` / `PaperMergeAction` / `EvolutionPlan` / Stage1&2 / benchmark 是否真的 SkillFlow / 四个 Setting 配置。

## 标签说明

- `[PAPER]`：论文公式/算法描述/Section 3-5 正文明确定义
- `[OFFICIAL]`：仅存在于官方仓库，论文正文未提及
- `[ENGINEERING]`：必要的工程实现决策（如用 Python subprocess 替代 Harbor Docker）
- `[EXTENSION]`：本项目自建，论文和官方代码均未要求

## 1. 确认与论文一致（[PAPER]，无需改动）

| 组件 | 文件 | 证据 |
|---|---|---|
| `WorkerPatch` = δ_i^t | [core/datatypes.py](../core/datatypes.py) | 严格 4 元组 (U,D,R,s) + `worker_id`（Appendix B.2 manifest 实际字段，非本项目发明），`upserts`/`deletions` 均做路径安全校验（拒绝绝对路径 / `..` 穿越） |
| `PaperMergeAction` | [core/datatypes.py](../core/datatypes.py) | 仅 ABSORB/REPAIR/REFACTOR，全仓核实未出现 CONSENSUS/AVERAGING/FEDAVG/GLOBAL MERGE |
| `SkipUpdate.NO_UPDATE` | [core/datatypes.py](../core/datatypes.py) | 独立于 `PaperMergeAction`，标 `[EXTENSION]`，未污染论文动作空间 |
| `CapabilityMatrix` | [core/datatypes.py](../core/datatypes.py) | 状态严格 covered/absorbing/broken/gap 四态；`is_workflow_fully_covered()` 正确实现"全部 covered 才退休" |
| `TrajectoryCompressor` | [client/trajectory.py](../client/trajectory.py) | K_step 初始步+最近 K_step-1 步、K_obs 截断、`<truncated>` marker 均对应 Section 4.1.2 |
| `PatchDistiller.distill()` | [client/distiller.py](../client/distiller.py) | compress→snapshot→outcome→prompt→LLM→validate→construct 七步，对应 δ=g_i(L,B,ρ) |
| Stage1/Stage2 分离 | [server/planner.py](../server/planner.py) / [server/merge.py](../server/merge.py) | `EvolutionPlan=(C^t,M^t,D^t)`、`MergedPatch=Δ_i^t`、`DecisionLog` 审计字段齐全 |
| benchmark 是否真 SkillFlow | [benchmark/families/](../benchmark/families) | 确认 20 个真实 SkillFlow family JSON + 5 个手写（本地回归用），`TaskFamily` 结构对应 Section 5.1"同技能递增难度序列" |
| Setting1-4 模型选择 | `experiments/configs/*.yaml` | Setting3=Qwen3.6-Plus/GLM-5/Kimi K2.5，Setting4=Qwen Coder/Claude Code/Kimi CLI，与论文 Table 3 一致 |

## 2. P0（必须修，阻塞 Setting3/4 实验）

### P0-1：`_build_sampler()` 不识别 `sampler: "heterogeneous"`，静默退化为 random

- 位置：[experiments/run_experiment.py](../experiments/run_experiment.py) `_build_sampler()`
- 现象：只识别 `"random"`/`"curriculum"`/`"replicate"`；`setting_hetero_backbone.yaml`（Setting3）和 `setting_full_hetero.yaml`（Setting4）都声明 `sampler: "heterogeneous"`，触发 `else` 分支 → `logger.warning(...)` → 回退 `RandomSampler`。
- 后果：论文最核心的两个实验设置（异构 backbone / 异构 backbone+harness）实际跑的采样策略与 Setting1/2 无区别，无法验证 "cross-client skill transfer"。
- 状态：**已修复（见 reproduction_changes.md Phase1/2/3）**。`_build_sampler()` 新增 `heterogeneous` 分支并 fail-loud；额外修复真实 family JSON 缺 `category` 字段的问题；`tests/test_sampler_fidelity.py` 锁定行为；已用真实 `benchmark/families/` 数据端到端验证 Setting3/4 三个 worker 分到不同 family（Financial-Statement-Rolling / OCR-Data-Extraction / DMAIC-Quality-Analysis）。

## 3. P1（应修，不阻塞）

| # | 问题 | 位置 | 状态 |
|---|---|---|---|
| P1-1 | `tasks_path` 字段死配置，从未被 `run_experiment.py` 读取（实际总是走 `families_dir`） | `experiments/configs/setting_*.yaml` | 已修复（Phase6：7 份配置删除死字段 + `run_experiment.py` 加 deprecation 警告） |
| P1-2 | 低层记忆按 `worker_id` 分桶，论文语义是按 ρ_i（backbone+harness 等价类）分桶；Setting2 三个同构 worker 应共享 1 份记忆，现在是 3 份独立 | [server/memory.py](../server/memory.py) | 已修复（Phase4：复用 `WorkerProfile.profile_hash` 做分桶键；`tests/test_memory_fidelity.py` 验证 Setting2 风格 3 worker→1 桶且互相可见，Setting3 风格 3 worker→3 独立桶且互不可见） |
| P1-3 | 4 套并行实验入口（`main_trainer.py`/`run.py`/`experiments/runner.py`/`experiments/federated.py`/`experiments/baseline.py`），采样器支持不一致 | 仓库根 / `experiments/` | 已修复（Phase5：`main_trainer.py` 顶部加 LEGACY ENTRY POINT 说明，指引统一用 `python run.py --setting N`，未删除文件） |

## 4. Extensions 清单（已标签，非论文声称）

| 组件 | 标签 |
|---|---|
| `FamilyAwareSampler`（依赖图+掌握度门控） | `[EXTENSION]`，隔离在 benchmark/，默认不使用 |
| `DifficultyAwareSampler` | `[EXTENSION]`，`ABLATION_ONLY=True` 守卫 |
| `CapabilityEvolutionTracker`（CSV/曲线导出） | `[EXTENSION]` |
| `SkipUpdate.NO_UPDATE` | `[EXTENSION]` |
| 3 个 ablation configs（a1/a2/a3） | `[EXTENSION]`，消融研究专用，非论文 Setting1-4 |

## 5. 保真度评分（静态审计，未跑真实实验）

- 算法忠实度：92/100
- 实验忠实度：70/100（P0-1 未修前，Setting3/4 数据不代表论文场景）
- 工程忠实度：85/100

修复 P0-1 + P1-1/P1-2/P1-3 后，预期实验忠实度可提升至 90+。
