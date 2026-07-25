# 论文忠实度审计报告（Paper Fidelity Report）

本文件是对《FederatedSkill: Federated Learning for Agentic Skill Evolution》
复现代码库的一次专项审计结果，目标是把每个声称"对应论文"的模块，明确分类为：

本报告统一采用如下 4 标签分类体系（取代此前使用的 5 个旧标签，语义映射见文末"标签定义"）：
- **[PAPER]** —— 论文 Section 4 / Algorithm 1 明确定义、有公式/章节支持的算法组件，
  必须独立实现（不复制官方 `.py` 源码），文档里标为 **MATCH**。
- **[OFFICIAL]** —— 官方代码/资产的实现细节，但论文正文没有明确描述（提示词原文、
  SKILL.md schema、benchmark family 数据文件等），标为 **MATCH（资产保留）**，
  但明确它们是"实验性配置资产"而非"本项目自行设计的算法"。
- **[ENGINEERING]** —— 为了替代官方工程实现而自建的手段（真实子进程工作区、
  多文件 Tool Calling、快照 diff 等），状态为 **EXTENSION**（不代表"错误"，只代表
  "不是论文算法本身"）。
- **[EXTENSION]** —— 为增强实验能力/审计能力而自建的机制（benchmark 采样调度、
  评估层统计导出、无官方对应的流程动作等），状态为 **EXTENSION**。
状态列取值：
- **MATCH**：docstring/注释对论文的引用准确，且实现与引用的论文条款一致。
- **EXTENSION**：功能是本仓库新增/近似的，且已有代码内标签（`[ENGINEERING]` /
  `[EXTENSION]`）明确不是论文要求。
- **REMOVE CLAIM**：审计中发现文档/注释错误地把非论文内容包装成"论文要求"，
  本轮已修正或已确认此前审计已修正。

> 审计范围：全仓库 `grep` 命中的 44 处 "论文要求 / paper requirement / Paper Section /
> 论文原文 / 严格对应论文 / according to the paper / as required by the paper" 字样，
> 以及 `scripts/paper_fidelity_check.py` 已有的程序化校验点。
> 审计原则：**不修改任何算法行为**，只修正/补全文档标签。

---

## 一、A 类：论文算法组件（Paper algorithm components）

| 模块 | 论文引用 | 状态 |
|---|---|---|
| [core/datatypes.py](../core/datatypes.py) `WorkerPatch` | Section 4.1.2（Patch schema / Eq.4，四元组 patch 结构） | MATCH |
| [core/datatypes.py](../core/datatypes.py) `CapabilityState` / `CapabilityMatrix` | Section 4.2.1（covered / absorbing / broken / gap 四态） | MATCH |
| [core/datatypes.py](../core/datatypes.py) `PaperMergeAction`（ABSORB / REPAIR / REFACTOR） | Section 4.2.2（"absorbing transferable patches, repairing broken skills in place, or rewriting peer skills"） | MATCH（已从历史上的 `MergeAction` 拆分而来，本枚举现在**只**含论文这 3 个动作） |
| [core/datatypes.py](../core/datatypes.py) `SkipUpdate.NO_UPDATE`（原 `MergeAction.DROP`） | 无对应论文条款 | REMOVE CLAIM → **已修正**（本轮进一步核实：已比对官方 `merge_skill/SKILL.md`（"drop the rest" 合并时清理冗余技能）与 `task_update_skill/SKILL.md`（"drop tasks once covered" 任务记账）中的 "drop"，两者都不是与 ABSORB/REPAIR/REFACTOR 并列的正式 merge 决策动作，语义也与本项目原 DROP 不同；因此不再把它作为 `MergeAction` 的第 4 个成员，而是拆分为独立类型 `SkipUpdate`（`[EXTENSION]`，成员 `NO_UPDATE`），避免类型本身暗示论文有 4 种动作，见 [core/datatypes.py](../core/datatypes.py) `SkipUpdate` docstring） |
| [core/datatypes.py](../core/datatypes.py) `DecisionLog` 核心字段（worker_id/round_idx/action/source_worker_id/affected_files/reward/reason） | Section 4.2.2（"source patch + reward + justification"） | MATCH |
| [core/datatypes.py](../core/datatypes.py) `DecisionLog.before_content_preview` / `after_content_preview` | 无对应论文条款（纯审计便利字段） | REMOVE CLAIM → **已修正**（已加⚠️说明，不影响决策逻辑） |
| [core/constants.py](../core/constants.py) Patch Distillation 参数 | Section 4.1.2（compaction / patch schema 约束） | MATCH |
| [client/distiller.py](../client/distiller.py) `PatchDistiller` | Section 4.1.2（client 侧蒸馏管线） | MATCH |
| [server/planner.py](../server/planner.py) `EvolutionPlanner` | Stage1 演化规划（论文 Algorithm 1） | MATCH |
| [server/merge.py](../server/merge.py) `EvolutionExecutor` | Stage2 逐 client 演化执行 | MATCH |
| [server/capability.py](../server/capability.py) `CapabilityTracker` | Section 4.2.1（能力矩阵维护） | MATCH |
| [server/memory.py](../server/memory.py) `EvolutionMemoryStore` | 论文两级记忆机制 | MATCH |
| [server/logging.py](../server/logging.py) `DecisionLogger` | Section 4.2.2（决策日志） | MATCH |
| [llm/prompt_builder.py](../llm/prompt_builder.py) Stage1/Stage2 prompt 构造步骤 | Section 4.1.2 step 1-3 | MATCH |

---

## 二、E 类：评估扩展（Evaluation extensions）

| 模块 | 论文引用 | 状态 |
|---|---|---|
| [evaluation/capability_tracker.py](../evaluation/capability_tracker.py) `CapabilityEvolutionTracker`（covered_count/absorbing_count/broken_count/gap_count 计数、transition 曲线、CSV 导出） | Section 4.2.1 定义了四态本身，但未定义统计/导出机制 | EXTENSION（已标 `[EXTENSION]`，与 `server/capability.py::CapabilityTracker`——MATCH，单轮状态追踪器——分工明确） |

---

## 三、B 类：Benchmark 扩展（Benchmark extensions，非论文/官方要求）

| 模块 | 论文引用 | 状态 |
|---|---|---|
| [benchmark/family_sampler.py](../benchmark/family_sampler.py) `FamilyAwareSampler`（依赖图 + 掌握度门控 + 巩固循环） | Section 5.1 只描述 family 数据结构，未规定调度算法；官方 `TaskPartitioner` 也无此机制 | EXTENSION（已标 `[EXTENSION]`，本轮完成标签体系迁移） |
| [benchmark/family_sampler.py](../benchmark/family_sampler.py) `cycles_completed()` / mastery reset | 同上 | EXTENSION（同一标签覆盖） |
| [benchmark/sampler.py](../benchmark/sampler.py) `DifficultyAwareSampler`（`_DIFFICULTY_SCHEDULE`） | 论文正文未给出逐轮难度调度公式 | EXTENSION（已有 `ABLATION_ONLY = True` 运行时守卫 + 已标 `[EXTENSION]`；保留在 `benchmark/` 原位，不迁移目录，避免无谓的 import/测试/实验配置变更） |
| [benchmark/task.py](../benchmark/task.py) 第 147 行附加字段 | 无对应论文条款 | EXTENSION（此前审计已自行标注 "Phase12 自建扩展"，本轮确认无需改动） |
| [benchmark/curriculum.py](../benchmark/curriculum.py) `FamilyCurriculumSampler`（纯按 round_idx 前进） | Section 5.1 描述的"递增难度序列"最接近字面语义的调度实现 | MATCH（调度逻辑本身仍是工程实现，但语义未超出论文对 benchmark 结构的描述，未发明额外机制） |
| [benchmark/family.py](../benchmark/family.py) `TaskFamily` 数据结构 | Section 5.1（family/task 抽象结构） | MATCH（结构对应论文，类文档已拆分为 `[OFFICIAL]`/`[ENGINEERING]` 两部分说明，见下节） |

---

## 四、C 类：工程扩展（Engineering extensions，与算法无关的实现细节）

| 模块 | 说明 | 状态 |
|---|---|---|
| [executor/base.py](../executor/base.py) `BaseExecutor` | 抽象接口统一（`run()` 必须返回 `Trajectory`），对应论文 Agent Harness 架构描述，但抽象基类本身是工程手段 | EXTENSION（已标 `[ENGINEERING]`） |
| [executor/environment.py](../executor/environment.py) `WorkspaceManager` | 真实临时工作区 + 文件快照 diff | EXTENSION（本轮补充标签） |
| [executor/runner.py](../executor/runner.py) `CommandRunner` | 工作区内 subprocess 命令执行封装 | EXTENSION（本轮补充标签） |
| [executor/trajectory.py](../executor/trajectory.py) `TrajectoryCollector` | 执行阶段 Trajectory 逐步构建 | EXTENSION（本轮补充标签） |
| [executor/agent_executor.py](../executor/agent_executor.py) `AgentWorkspaceExecutor` | 组合上述四个组件的多文件执行器 | EXTENSION（本轮补充标签） |
| [evaluation/metrics.py](../evaluation/metrics.py) `sensitive_entity_leakage_rate()` | Appendix E Eq.5 的正则实体抽取近似，非 LLM 语义裁判 | EXTENSION（此前审计已加⚠️近似说明，本轮确认无需改动） |
| [evaluation/metrics.py](../evaluation/metrics.py) Family Success Curve | 无对应论文公式 | EXTENSION（此前审计已自行标注"本复现新增，非论文原文公式"） |

---

## 五、D 类：官方资产（Official artifacts，保留原文但非算法）

| 模块 | 说明 | 状态 |
|---|---|---|
| [prompts/stage1_prompt.txt](../prompts/stage1_prompt.txt) | 保留官方 `task_update_skill/SKILL.md` 原文提示词文本 | MATCH（资产保留，已标 `[OFFICIAL]`） |
| [prompts/stage2_prompt.txt](../prompts/stage2_prompt.txt) | 保留官方 `merge_skill/SKILL.md` 原文提示词文本 | MATCH（资产保留，已标 `[OFFICIAL]`） |
| [prompts/patch_prompt.txt](../prompts/patch_prompt.txt) | 官方 patcher 提示词不可获取，本项目自行用英文撰写 | EXTENSION（已标 `[ENGINEERING]`；本会话此前已修复其被误写成中文、违背"规避 ASCII 编码 bug"策略的回归） |
| [client/library.py](../client/library.py) SKILL.md 目录 schema（frontmatter + scripts/references/assets） | 沿用官方 benchmark 目录约定 | MATCH（资产保留，已标 `[OFFICIAL]`；`SkillLibrary` 类读写逻辑本身标为 `[ENGINEERING]`） |
| `benchmark/families/*.json` 中来自官方 SkillFlow-Task 数据集的 family | 官方 benchmark 数据 | MATCH（`[OFFICIAL]`，见 [benchmark/family.py](../benchmark/family.py) 顶部说明） |
| `benchmark/families/*.json` 中 5 个手写 family（本地无网络回归测试用） | 无对应官方数据 | EXTENSION（`[ENGINEERING]`） |

---

## 六、结论

- 本轮审计**未发现**新的、未被此前审计覆盖的"把扩展包装成论文要求"的情况；
  此前多轮审计（Official Implementation Alignment Audit、Official Prompt Retention
  重构等，详见 [docs/SIMPLIFICATIONS.md](SIMPLIFICATIONS.md) 与
  [docs/paper_mapping.md](paper_mapping.md)）已经把主要的"过度声称"问题修正掉。
- 本轮新增一次用户复审，纠正了两处过度模糊/潜在高估的提法，随后又追加一次结构性重命名：
  1. `MergeAction.DROP` 不应笼统地写成 "official workflow extension"；实际比对官方
     `merge_skill/SKILL.md` / `task_update_skill/SKILL.md` 后确认其中的 "drop" 用法与本项目
     `MergeAction.DROP` 语义不同，改标为更准确的 `[EXTENSION]`（原拟用的过渡标签
     `[project workflow extension]` 已并入新 4 标签体系）。
  2. `evaluation/capability_tracker.py::CapabilityEvolutionTracker` 虽依赖论文定义的四态，
     但统计/导出本身不是论文组件，标为 `[EXTENSION]`；措辞已按用户要求更正为
     "Tracks transitions of the paper-defined capability matrix. Visualization and CSV
     export are additional evaluation utilities."，不再暗示"实现了论文的能力演化机制"。
  3.（结构性重命名）原 `MergeAction`（ABSORB/REPAIR/REFACTOR/DROP 四个成员混在一个类里）
     已拆分为 `PaperMergeAction`（str, Enum，仅 ABSORB/REPAIR/REFACTOR，`[PAPER]`）和
     `SkipUpdate`（str, Enum，仅 `NO_UPDATE`，`[EXTENSION]`），使类型定义本身就能诚实反映
     "论文只有 3 种合并动作，第 4 种是工程优化"，不再需要读者额外记住"DROP 不算论文动作"这条注记。
     `Directive.action`/`DecisionLog.action` 字段类型同步改为 `PaperMergeAction | SkipUpdate`；
     `server/planner.py`、`server/merge.py`、`server/prompt_builder.py` 中所有 `MergeAction(...)`
     解析与比较逻辑同步改为 `core.datatypes.parse_merge_action()` 辅助函数 / `SkipUpdate.NO_UPDATE`
     比较；全量回归 125 passed / 2 skipped，`_test_imports.py` 61 OK，`_test_e2e.py` PASS。
- 本轮已完成标签体系迁移：全仓旧的 5 个标签 `[benchmark extension]` /
  `[engineering implementation]` / `[official artifact]` / `[project workflow extension]` /
  `[evaluation extension]` 已统一替换为新的 4 标签体系 `[PAPER]` / `[OFFICIAL]` /
  `[ENGINEERING]` / `[EXTENSION]`（映射关系见下方标签定义），
  便于以后用 `grep -R "\[PAPER\]\|\[OFFICIAL\]\|\[ENGINEERING\]\|\[EXTENSION\]"`
  一次性定位全部非核心算法模块，不需要再阅读长段中文说明。
- 未对任何算法行为（`server/planner.py`、`server/merge.py`、`client/distiller.py`、
  `core/datatypes.py` 的字段与校验逻辑）做修改；仅编辑了模块级 docstring。
- 回归验证：`pytest` 125 passed / 2 skipped；`_test_imports.py` 61 OK / 0 FAIL；
  `_test_e2e.py` 端到端冒烟测试 PASS。

## 附：本轮涉及的标签定义（供 `scripts/paper_fidelity_check.py` 及未来审计复用）

```text
[PAPER]       — 论文明确提出，有公式/章节支持（如 Eq.(4)、Section 4.2.1/4.2.2 描述的算法组件）
[OFFICIAL]    — 官方代码/资产的实现细节，但论文没有明确描述（提示词原文、SKILL.md schema、benchmark 数据文件）
[ENGINEERING] — 为了自己实现替代官方工程而自建的手段（如 Docker→subprocess 执行器、工作区隔离）
[EXTENSION]   — 为了增强实验/审计能力而自建的机制（benchmark 采样调度、评估层统计导出、SkipUpdate.NO_UPDATE 等无官方对应的流程动作）
```

旧标签 → 新标签映射（仅供历史对照，新增代码请直接使用新 4 标签）：

```text
[benchmark extension]        → [EXTENSION]
[engineering implementation] → [ENGINEERING]
[official artifact]          → [OFFICIAL]
[project workflow extension] → [EXTENSION]
[evaluation extension]       → [EXTENSION]
```
