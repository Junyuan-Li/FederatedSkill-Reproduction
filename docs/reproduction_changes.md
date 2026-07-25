# Reproduction Changes Log

> 记录 Phase0-10 每一步的具体改动，供审计追溯。格式：Phase → 改了什么文件 → 为什么 → 验证方式。

## Phase 0：冻结基线

- 创建分支 `paper-faithful-final-fix`（仓库根 `D:/pythonlesson`，从 `master` 切出）
- 保存 `docs/audit_report_v1.md`（Step1 审计结果）、`docs/paper_mapping.md`（公式↔代码对应表）
- 状态：完成

## Phase 1：修复 P0 —— HeterogeneousSampler 路由

- 文件：`experiments/run_experiment.py::_build_sampler()`
- 改动：新增 `"heterogeneous"` 分支，复用已有 `benchmark/sampler.py::HeterogeneousSampler`；`workers` 缺失 `task_categories` 时立即 `raise ValueError`（fail-loud，不再 silent fallback）；未知 `sampler` 值也改为 `raise`
- 额外发现并修复：真实 `benchmark/families/*.json`（20 个官方 family）里 `Task.category` 字段全为空（只有 `family_id`），若不处理会导致异构分桶全部落到同一个空字符串桶——已在 `_build_sampler` 内为空 `category` 回填 `t.category = t.family_id`
- 验证：`tests/test_sampler_fidelity.py`（6 项全过）+ 用真实 `benchmark/families/` 数据端到端跑 Setting3/4 配置，确认 u0/u1/u2 分到不同 family（如 Financial-Statement-Rolling / OCR-Data-Extraction / DMAIC-Quality-Analysis）
- 状态：完成

## Phase 2：补齐 Setting3/4 配置的 task_categories

- 文件：`experiments/configs/setting_hetero_backbone.yaml`、`experiments/configs/setting_full_hetero.yaml`
- 改动：给每个 worker（u0/u1/u2）按主题分组补 `task_categories` 字段（金融类/文档类/运营类，各 9/8/8 个 family_id），Setting3 与 Setting4 使用完全相同的分组以便直接对比
- 状态：完成（已用 read_file 复核两份 YAML 内容）

## Phase 3：新增 sampler fidelity 单元测试

- 文件：`tests/test_sampler_fidelity.py`（新建）
- 覆盖：Setting1 随机采样、Setting3 异构分桶正确路由、`_build_sampler` 缺配置/未知 sampler 时 fail-loud
- 状态：完成（`pytest tests/test_sampler_fidelity.py` 6 passed）

## Phase 4：修复 P1 —— Low-level Memory 语义

- 文件：`core/datatypes.py`（`LowLevelMemory` 新增 `profile_key`/`shared_worker_ids` 两个字段，便于审计追溯分桶归属）、`server/memory.py`（`EvolutionMemoryStore._low` 分桶键改为 `WorkerProfile.profile_hash`，即 ρ_i 等价类；复用已有的 `profile_hash` 计算字段，未新增 `memory_key()` 方法，避免重复实现同一语义）
- 保留：`DecisionLog.worker_id`（审计需要，未改动）
- 验证：`tests/test_memory_fidelity.py`（新建，5 项全过）——确认 Setting2 风格（3 个同 profile worker）只产生 1 个记忆桶且互相可见；Setting3 风格（3 个不同 profile worker）产生 3 个独立桶且互不可见；`server/planner.py` 中直接构造 `LowLevelMemory(...)` 的调用点（约 186/189/246/249 行）因新增字段均带默认值，无需改动即可兼容
- 状态：完成

## Phase 5：清理实验入口混乱

- 文件：`main_trainer.py`（顶部 docstring 新增 LEGACY ENTRY POINT 警示，说明其内部另有一套平行的 `_build_sampler()` 实现且不是官方复现入口，指引统一使用 `python run.py --setting N`；未删除文件本身）
- 状态：完成

## Phase 6：处理 tasks_path 死配置

- 文件：7 份 `experiments/configs/*.yaml`（删除死字段 `tasks_path`）、`experiments/run_experiment.py::run_experiment()`（加载配置后若仍存在 `tasks_path` 则打印 deprecation warning，不再静默忽略）
- 复核：全仓库 `grep tasks_path` 仅剩 `run_experiment.py` 里的警告逻辑本身，无遗漏；`main_trainer.py` 走的是自己独立的配置加载路径，未使用 `tasks_path` 字段，无需同样处理
- 状态：完成

## Phase 7：论文实验运行矩阵

- 文件：`experiments/reproduction_matrix.yaml`（新建）——把论文 Setting1-4 显式映射到配置文件名，与 `experiments/runner.py::SETTING_CONFIG_MAP` 逐一核对一致；同时列出 3 个消融配置（a1/a2/a3）供 Table/Ablation 部分使用
- 状态：完成

## Phase 8：最小闭环 smoke test

- 状态：待办

## Phase 9：真实实验（Setting1→2→3→4 顺序执行）

- 状态：待办

## Phase 10：最终论文一致性复核

- 状态：待办
