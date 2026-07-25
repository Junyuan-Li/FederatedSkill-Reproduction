# 官方实验一致性审计报告

> `FederatedSkill-Reproduction` vs `UCSB-NLP-Chang/FederatedSkill`
> （本地快照：`FederatedSkill-main/FederatedSkill-main/`）
>
> 审计目标：**不改核心算法**（aggregation/patch evolution 思想），只排查
> 影响实验结果可比性的外围资产（prompt/config/skill模板/timeout/retry/
> 数据划分/reward 公式/随机性）。10 个 Part 对应用户原始指令的 Part1-10。
>
> 姊妹文档：[model_alignment.md](model_alignment.md)（Part1 详细版）、
> [prompt_alignment_report.md](prompt_alignment_report.md)（Part2 详细版）、
> [experiment_assets_to_port.md](experiment_assets_to_port.md)（Part10 迁移清单）、
> [official_component_mapping.md](official_component_mapping.md)（此前会话产出，
> 覆盖 Task Runner/Harbor Bridge/Verifier/Trajectory Logging 等**架构层**映射，
> 与本文件的"实验结果一致性"范围互补、不重复）。

## 图例

- ✅ 已核实一致 　⚠️ 已核实存在差异（未修改）　ℹ️ 已知架构差异/无法直接比较
  　❓ 本次会话时间预算内未能核实（诚实标注，非回避）

---

## Part 1 — 模型与 Agent 配置对齐

详见 [model_alignment.md](model_alignment.md)。核心结论摘要：

| 官方来源 | 当前对应 | 差异 | 是否迁移 | 影响 |
|---|---|---|---|---|
| 4 份 `configs/*.local.yaml` 的 `agent.import_path`/`model_name`/`env.ANTHROPIC_*` | `experiments/configs/setting_se.yaml`/`setting_homo_fed.yaml` | ✅ backbone/harness/endpoint 命名一致 | 不需要 | 低 |
| `patcher_bridge.py` temperature=0.2 / max_tokens=8192 / kimi 强制 temp=1.0 | `core/constants.py::DEFAULT_TEMPERATURE/DEFAULT_MAX_TOKENS/MOONSHOT_TEMPERATURE` | ✅ 数值完全一致（已核实） | 不需要 | — |
| `env.ANTHROPIC_MAX_RETRIES: "999"`（4 份 yaml 均有） | `harness/claude_code_harness.py::build_env()` | ⚠️ **未设置该环境变量** | **是（Tier A）** | 中高——限流下 trial 更容易直接失败，拉低 success_rate，与"timeout/retry不同"怀疑点直接对应 |
| `merger_llm.max_tokens: 16384`（Setting2/3/4 yaml） | `setting_homo_fed.yaml::server.merger_max_tokens: 16384` 字段**从未被代码读取**，`_build_backbone()` 实际用的是默认值 8192 | ⚠️ **死配置 + 数值差异** | **是（Tier A）** | 中——Stage2 需要一次性输出多个 SKILL.md 全文 + DECISIONS.md + memory.md，8192 有截断风险 |
| Worker agent 自身 temperature/max_tokens | 双方均未暴露（CLI 内部决定） | ✅ 对齐（同为空） | 不需要 | — |

---

## Part 2 — Agent System Prompt 对齐

详见 [prompt_alignment_report.md](prompt_alignment_report.md)。结论：官方
adapter 层不存在等价的"结束任务前必须验证"指令（该层只透传 task 自己的
`instruction.md` 给外部 CLI），本仓库 `_VERIFICATION_DISCIPLINE_BLOCK`
**保留**，已正式记录为 reproduction deviation（需在 `docs/SIMPLIFICATIONS.md`
补记录，见 Part10）。

| 官方来源 | 当前对应 | 差异 | 迁移 | 影响 |
|---|---|---|---|---|
| （不存在——`cli.py`/`merge.py` 均未拼接通用 worker 侧指令） | `harness/cli_harness_base.py::_VERIFICATION_DISCIPLINE_BLOCK` | ℹ️ 纯 reproduction 新增，无官方对照物 | 保留，仅补充披露文档 | 低（该指令本身是为修复真实执行层 bug 而非论文一致性问题引入） |

---

## Part 3 — Skill 生成相关 Prompt 对齐

### 3.1 Skill 文件格式（frontmatter）

| 项 | 官方 | 本仓库 | 差异 |
|---|---|---|---|
| SKILL.md frontmatter 字段 | 仅 `name`（kebab-case）+ `description`（一句话）——`task_update_skill/SKILL.md`/`merge_skill/SKILL.md` 自身的 YAML frontmatter 都是这两个字段，其余全部是 Markdown 正文（workflow 步骤、examples 等都不是结构化 frontmatter 键） | `prompts/patch_prompt.txt` 明确写 `name:`/`description:` 两字段；`client/library.py::_parse_frontmatter()` 校验规则也只要求这两个字段存在 | ✅ **一致**——用户原始 Part3 描述里提到的"workflow/examples"应理解为 SKILL.md **正文小节标题**，而不是 frontmatter 字段；本仓库的实现口径本来就是对的，不需要新增 `workflow:`/`examples:` frontmatter 键（那样反而会引入官方没有的结构） |
| Skill 目录结构 | `SKILL.md` + `scripts/` + `references/` + `assets/`（`merge_skill/SKILL.md`"Inputs"一节明确列出） | `client/library.py` 文档字符串 + `core/constants.py::ALLOWED_SKILL_SUBDIRS` 同样是这 4 类 | ✅ 一致 |

### 3.2 Patch Distillation Prompt（client 侧，per-trial）

- **官方真实实现不可见**：`patcher_bridge.py` 实际调用的是
  `libs.skill_evolution.patcher::SkillPatchEvolver.generate_patch()`——
  `libs.skill_evolution` 是一个**外部依赖包**，其源码（包括真正的
  patch-distillation prompt 文本）**不在 `FederatedSkill-main` 仓库快照内**，
  与官方 `harbor` 执行引擎属于同一类"胶水代码之外、不可获取"的依赖
  （`official_component_mapping.md` 已对 `harbor` 做过同样的诚实披露，这里
  是第二个同类型的不可得依赖，需要同样对待，不能假装能做逐字对比）。
- 因此 `prompts/patch_prompt.txt` 与官方真正的 patcher prompt **无法逐字
  比对**——只能比对本仓库能看到的官方"下游消费者"（`merge_skill/SKILL.md`）
  对 patch 内容提出的隐含要求。
- **ℹ️ 观察到的潜在改进点（非官方差异，Tier B 建议）**：官方
  `merge_skill/SKILL.md`（Stage2/合并阶段）明确要求"每个含数值输出的
  SKILL.md 必须有 `## Output precision` 小节"、"每个 SKILL.md 必须有
  `## Known invariants (by sub-task)` 小节"、"禁止 round()/格式化数值"。
  本仓库 `prompts/stage2_prompt.txt`（合并阶段）已经原样搬运了这些规则
  （见 Part5），但 `prompts/patch_prompt.txt`（**更早、client 侧**的蒸馏
  阶段）目前**没有**提前要求 patch 输出遵守这两条规则——导致这些规则要等
  到 Stage2 合并时才被动修正，而不是从源头（patch 生成时）就产出合规内容。
  由于官方真实 client 侧 prompt 不可见，无法确认官方是否"从一开始就要求"
  还是"也是合并阶段才修正"——**不作为已确认差异**，仅作为 Tier B 建议
  （提前把这两条规则也写进 `patch_prompt.txt`，减少无谓的合并期返工）。

---

## Part 4 — Skill Library 格式对齐

| 项 | 官方（从 `merge_skill/SKILL.md` Inputs 一节 + `cli.py` 反推） | 本仓库（`client/library.py`） | 差异 |
|---|---|---|---|
| 目录结构 | `library/<skill>/SKILL.md`、`.baseline_library/`（本轮开始前快照）、`peer_libraries/<peer>/`（联邦模式下的同伴库快照） | `SkillLibrary.snapshot()`/`rollback()`——本仓库用**方法调用**返回快照对象而非官方那种"预先落盘好的旁路目录"，语义等价（都能拿到"合并前的库状态"和"同伴的库状态"），只是实现形态不同 | ℹ️ 语义等价，实现路径不同，不影响 skill retrieval/use 的最终效果 |
| 版本/元数据 | 未见显式 `version:` 字段——官方 SKILL.md frontmatter 就只有 `name`/`description`，版本迭代靠 DECISIONS.md 的逐轮记录而非 SKILL.md 自带版本号 | `client/library.py` 同样未强制 `version` 字段 | ✅ 一致（都没有版本号概念，用外部 decision log 追踪演化） |
| 命名规范 | "Names are scope"——吸收同伴 patch 时把窄名重命名为宽名；工具锚定名（excel-X/pandas-X）应改名 | `prompts/stage2_prompt.txt` 已原样包含这两条规则 | ✅ 一致 |

### 4.1 补充核实：官方 `validate_skill_md.py` vs 本仓库 `SkillLibrary.validate()`

本次追加读取官方 `merge_skill/scripts/validate_skill_md.py`（合并 agent 在写
DONE.txt 前必须跑的校验脚本）与 `validate_library.sh`（把
`validate_skill_md.py` + `find_near_dup_skills.py` + `py_compile` + 过拟合/
网络调用 grep + junk 文件扫描组合成一次性校验），逐项比对本仓库
`client/library.py::SkillLibrary.validate()`：

| 官方检查项 | 本仓库是否已有 |
|---|---|
| SKILL.md 可用 UTF-8 读取 | ✅ 有（`try/except` 包裹 read_text） |
| frontmatter 是合法 YAML | ✅ 有 |
| frontmatter 含 `name`（必需）/`description`（建议） | ✅ 有 |
| **技能目录名与 `frontmatter.name` 归一化后一致**（如 `vaxcrate-dispatch` 目录必须对应 `name: Vaxcrate Dispatch`） | ⚠️ **缺失**——本仓库当前不校验目录名与 frontmatter name 是否一致 |
| **SKILL.md 文件行数 ≤ 500 行软上限** | ⚠️ **缺失**——本仓库当前不检查文件长度 |
| 脚本语法检查（`py_compile` 所有 `scripts/*.py`） | ⚠️ 缺失（`validate()` 目前只检查子目录白名单，不检查脚本内容） |
| 过拟合/网络调用 grep（绝对路径/http(s)/urllib/requests/硬编码 .xlsx/.csv 文件名） | ⚠️ 缺失——这一项与 `prompts/patch_prompt.txt` 里"Strict privacy constraints"一节描述的约束（禁止具体文件名/业务ID/一次性逻辑）**语义高度相关**，官方把它做成了机器可执行的 grep 校验，本仓库目前只在 prompt 层面要求 LLM 自律，没有代码层面的兜底检查 |
| junk 文件扫描（`__pycache__`/`.pyc`/`.DS_Store`） | ⚠️ 缺失 |

**影响评估**：中——这些是"防止 LLM 输出的技能不合规"的机械兜底检查，缺失
不会直接影响 success_rate 的均值，但会让本仓库的技能库比官方更容易积累
"目录名与声明名不一致"、"超长 SKILL.md"、"残留过拟合硬编码路径"这类质量
问题，长期可能拖累 skill reuse 效果与隐私约束（SELR 相关）的实际有效性，
且这是**可直接复制官方脚本、无需理解 CloudSkillMerge 内部状态**的低风险
迁移项。已列入 Part10 Tier A。

### 4.2 环境与依赖锁定（pyproject.toml / setup.sh / .env.example）

| 官方 | 本仓库 | 差异 |
|---|---|---|
| `harbor @ git+...@9ee6790...`（精确 pin 到某个 commit） | 不适用——本仓库不依赖 `harbor`（真实执行引擎用自建 `harness/` 包替代，`docs/SIMPLIFICATIONS.md` 已披露） | ℹ️ 架构差异，不可比较 |
| `litellm>=1.70.0` | `requirements-real.txt::litellm>=1.67.0,<1.85.0`（注释：Windows 上 ≥1.85 需要 Rust 编译） | ⚠️ 版本区间有重叠但不完全一致（官方下限 1.70，本仓库下限 1.67）；本仓库额外加了 Windows 兼容性上限。影响程度低（litellm 的路由行为在这个版本区间内通常稳定），但如果要做"完全环境对齐"的可比实验，建议至少把下限提到 1.70 |
| `pydantic>=2.8.0` | `requirements-real.txt::pydantic>=2.8.0` | ✅ 一致 |
| `tenacity>=9.0.0`（官方用于重试） | 本仓库未使用 `tenacity` 库，`llm/backbone.py` 是手写的 `RetryConfig`/`_is_rate_limit()` 重试逻辑 | ℹ️ 依赖库不同，但重试**行为**已在 Part1/6 单独评估（`ANTHROPIC_MAX_RETRIES` 缺失才是真正的行为差异，用什么库实现不是差异来源） |
| `huggingface_hub[cli]>=0.27.0`（下载 test_tasks/） | `requirements-real.txt::huggingface-hub>=0.24.0` | ⚠️ 下限版本不同（0.24 vs 0.27），风险低（都属于该库的稳定 API 阶段） |
| `.env.example`：`DASHSCOPE_KEY`/`MOONSHOT_KEY`/`ANTHROPIC_API_KEY`（3 个变量，覆盖两个 provider + 可选原生 Anthropic） | 本仓库 `.env` 用途相同但变量名已拆分为 `QWEN_DASHSCOPE_API_KEY`/`GLM_DASHSCOPE_API_KEY`/`MOONSHOT_API_KEY`（Provider Key Isolation Fix，此前已披露） | ✅ 语义一致，变量名工程拆分 |
| `setup.sh` 用 `uv sync` 装 SkillFlow venv，`.venv` 软链接到 `external/SkillFlow/.venv` | 本仓库无 `external/SkillFlow` 依赖（不依赖上游 SkillFlow 仓库的 `iterative_shared_skills_runner.py`，用自建 `experiments/run_experiment.py` 替代） | ℹ️ 架构差异，已知（`docs/SIMPLIFICATIONS.md` 已披露不依赖外部 SkillFlow/harbor） |

### 4.3 `paper_logs/` 真实运行日志覆盖范围

官方仓库本地快照的 `paper_logs/` 目录下**只有 3 个 setting 的真实日志**：
`1_se/`（含 `qwen3.6-plus/`、`glm-5/`、`kimi-k2.5/` 三个 backbone 子目录）、
`3_fed_hetero_cc/`、`4_fed_hetero_mixed_cli/`——**没有 `2_fed_3glm_cc/`
（Setting2 同构联邦）的真实日志**。

**影响**：本仓库 `setting_homo_fed.yaml`（对应 Setting2）在"是否与官方真实
运行行为一致"这件事上，**缺少可直接比对的官方 ground-truth 日志**——此前
所有"已对齐"的结论（`sync_schedule`/`merger_mode`/`isolated_worker_skills`
等）都只能通过对照 `configs/2_fed_3glm_cc.local.yaml`**配置文件本身**来
验证，无法像 Setting1 那样再用真实 `paper_logs/1_se/qwen3.6-plus/` 的实际
运行记录做交叉验证（此前会话对 Setting1 `max_retry=0` 的结论正是来自
`paper_logs`，而非配置文件）。建议在 Part10 里标注：**Setting2 的一致性
结论置信度略低于 Setting1**，若条件允许应优先用 Setting1/3/4 做最小验证
实验（这三个都有官方真实日志可比对），Setting2 暂缓。

**结论**：Skill Library 格式总体已充分对齐，但新发现 §4.1 的校验脚本缺口
（Tier A）与 §4.2/4.3 的环境/日志覆盖问题（低风险信息记录，非迁移项）。

---

## Part 5 — Evolution Prompt 对齐（Stage1 / Stage2）

已在本次会话中逐字读取官方 `task_update_skill/SKILL.md`（Stage1）与
`merge_skill/SKILL.md`（Stage2）全文，并与本仓库 `prompts/stage1_prompt.txt`/
`stage2_prompt.txt` 逐节比对。结论：**本仓库的两份 prompt 是官方两份
SKILL.md 的忠实压缩版**，核心规则（reward 语义、覆盖矩阵四态、
per-worker findings 规则、Critical invariants、Principles、DECISIONS.md
格式、memory.md 规范、Target-model+CLI awareness）**均已保留且语义一致**，
不是用户在任务描述里预设的"可能存在较大差异"的项。

### 已对齐的具体规则（无需迁移）

- reward=1.0 是唯一"通过"值；覆盖矩阵 covered/absorbing/gap/gap-broken 四态判定表 —— ✅ 逐字对齐。
- Per-worker findings：仅对有 gap 的 worker 写详细分析；覆盖良好的 worker 一行带过；模式性结论需要 ≥2 轮证据 —— ✅ 对齐。
- Stage2 Critical invariants（target 自己的 patch 不应丢失/peer patch 是对 peer 自己库的改写/只写 library+DECISIONS.md）—— ✅ 对齐。
- Stage2 Principles（wholesale>synthesize / reuse-or-justify / extend-before-adding-umbrella-first / 硬上限4个技能目标2-3个 / names-are-scope / never-round数值 / `## Output precision` 必需 / `## Known invariants (by sub-task)` 必需）—— ✅ 全部对齐，`stage2_prompt.txt` 注释里甚至显式标注"from official merge-skill-patch SKILL.md"。
- DECISIONS.md 格式（path/action/source/vs_peers/reason）—— ✅ 对齐。
- memory.md 规范（非 changelog，聚焦 model 特性认知，≤80 行）—— ✅ 对齐。
- Target-model + CLI awareness（claude-code 读后写更激进，qwen-code/kimi-cli 更"单发后总结"，需要显式脚本化验证步骤）—— ✅ 对齐。

### 确认存在但影响较小的缺口（本仓库压缩版省略的官方细节）

| 官方独有内容 | 本仓库现状 | 影响评估 |
|---|---|---|
| Stage1："Task buckets" INPUT/TRANSFORMATION/OUTPUT 结构化写法示例 | `stage1_prompt.txt` 未包含 | 低——这是"如何组织输出格式"的写作指导，不改变判定逻辑本身 |
| Stage1：7 步 Workflow 编号列表、"What to avoid" 清单 | 未包含 | 低——属于操作流程提示，实质规则（reward 语义/四态表）已经保留 |
| Stage2："stale skills"库审计检查项（第三类审计，官方有 umbrella check + pairwise redundancy + **stale skills check** 三项，本仓库只保留前两项） | `stage2_prompt.txt` 的"Library audit"一节缺少 stale-skills 检查 | **中**——若某个 skill 连续多轮未被任何 worker 引用/命中，官方会主动清理，本仓库当前不会主动做这项检查，可能导致技能库"只增不减"，影响 skill_growth 指标的可比性 |
| Stage2：多个 worked Examples（umbrella 合并/吸收时改名/新增稀有 case/收敛/保持独立）、Helper scripts 清单（`summarize_patches.sh`等 6 个脚本） | 均未包含（本仓库 Stage2 是直接 LLM completion 调用，不是能调用 shell 脚本的 agentic loop） | ℹ️ 架构性差异（已在 `docs/SIMPLIFICATIONS.md` §6.1 披露），不是"忘记复制"，而是执行模型不同导致这些脚本类内容不适用 |

### ABSORB/REPAIR/REFACTOR 语义核实

- 官方 `merge.py`/`cli.py` 源码里**没有**字面的 `ABSORB`/`REPAIR`/`REFACTOR`
  enum——`CloudSkillMerge` 是完全 agentic（容器内 claude-code 直接读写文件），
  这几个词只在 `merge_skill/scripts/*.py` 的注释里作为**非正式动词**出现
  （如"absorb peer patches"）。
- 本仓库 `core/datatypes.py::PaperMergeAction`（ABSORB/REPAIR/REFACTOR/
  NO_UPDATE）是**从论文 Section 4.2.2 原文**（"absorbing transferable
  patches, repairing broken ones..."）反推出的结构化 enum，用于把 Stage2
  的 LLM 输出约束成可解析的 JSON directive——这是本仓库为了让 Stage2 可
  单元测试/可审计而做的**工程形式化**，语义上与官方论文描述一致，只是
  官方的实现（agentic 文件编辑）没有一个显式对应的 enum。
- **结论**：ℹ️ 架构差异，非语义分歧。不需要迁移（迁移到"完全 agentic
  claude-code 容器"会是架构级重写，超出本次"外围资产对齐"的范围，且用户
  明确排除"核心算法/aggregation 思想"的修改）。

---

## Part 6 — 实验参数配置对齐

| 参数 | 官方值（来源） | 本仓库值 | 差异 |
|---|---|---|---|
| Worker 数（Setting1/SE） | 1 | `setting_se.yaml`: 1 个 worker (`u0`) | ✅ |
| Worker 数（Setting2/Homo Fed） | 3 | `setting_homo_fed.yaml`: 3 个 worker | ✅ |
| Task partitioner（联邦设置） | `replicate`（`ReplicatePartitioner`——**所有 worker 拿到同一个 task**，不是切分任务） | `sampler: "family_curriculum"`，`FamilyCurriculumSampler` 语义 = 每个 worker 绑定同一个 family、按 round 递增难度，配合 `isolated_worker_skills: true`/`merger_mode: "unshared"` | ✅ 语义一致（此前阶段已按论文 Table1/Table6 对齐，本次核实注释与实现相符） |
| Sync schedule | `every_task`（每个 task 后都同步/合并） | `setting_homo_fed.yaml::sync_schedule: "every_task"` | ✅ 一致 |
| Merger mode | `unshared` + `isolated_worker_skills: true`（个性化合并：每个 worker 独立库，合并按 target worker 逐个跑） | `setting_homo_fed.yaml` 同样设置 `merger_mode: "unshared"` + `isolated_worker_skills: true` | ✅ 一致 |
| Round 数 | 按 family 自身任务数（8 或 9，Table 6） | `rounds_per_family_mode: "family_length"`（默认），`rounds: 8` 仅在 `fixed_cap` 模式下才生效为 cap | ✅ 一致（此前阶段已修正过"静默截断到 8 轮"的 bug，见 run_experiment.py 内联注释 TASK3） |
| Task 内 retry（agent/task 级失败是否重跑该 task） | 官方 `paper_logs` 真实记录显示 `retry.max_retries=0`（agent 执行/超时失败不重试，记 reward=0 继续下一个 task） | `setting_se.yaml::max_retry: 0`；`setting_homo_fed.yaml::max_retry: 2` | ⚠️ **Setting2 与官方不一致**：SE（Setting1）已对齐为 0，但 Homo Fed（Setting2）配置里是 `max_retry: 2`，需要核实这是否有意为之（例如"失败 attempt 回滚 library"的工程需要）还是应该同样改成 0；本次未深入核实这条注释是否已有历史讨论依据，标记为待确认项，**不自动修改** |
| CLI 内部 LLM 调用重试（`ANTHROPIC_MAX_RETRIES`） | `"999"` | 未设置（见 Part1） | ⚠️ 见 Part1，Tier A |
| Patcher temperature/max_tokens | 0.2 / 8192 | 0.2 / 8192 | ✅ |
| Merger max_tokens | 16384 | 声明为 16384 但代码未读取，实际生效 8192 | ⚠️ 见 Part1，Tier A |
| Cloud merger turn/wall-clock 预算（`max_turns=30, wall_clock_sec=600`；task-update `max_turns=10, wall_clock_sec=300`） | 官方专属于"容器内 agentic claude-code 循环"的预算，本仓库 Stage1/2 是单次 completion 调用，没有"多轮"概念 | 不适用 | ℹ️ 架构差异，不可比较（已在 model_alignment.md 提及） |

---

## Part 7 — Benchmark 配置（20 families / 166 tasks）

- 官方 family 内任务顺序来源：`cli.py::resolve_family_tasks()` 读取
  `<family>/ALL_TASK_DIFFICULTY_RANKING.json`，按其顺序排列，未列出的任务
  追加在末尾。
- 本仓库 `benchmark/skillflow_adapter/loader.py`（第 50 行）与
  `scripts/select_representative_families.py`（第 44 行）均读取同名文件
  `ALL_TASK_DIFFICULTY_RANKING.json`，逻辑一致（已通过
  `tests/test_skillflow_adapter.py` 覆盖该排序逻辑）。
- `setting_se.yaml::paper_benchmark_only: true` 显式排除本仓库自建的 5 个
  legacy family（非官方 20 个 family 之一），确保只跑论文定义的官方
  benchmark（此前阶段 TASK1 已修正，本次核实配置项仍然存在）。
- **结论**：✅ 已对齐，任务顺序/family 范围与官方一致，未发现改变任务顺序/
  删除任务/抽样任务的行为。

---

## Part 8 — Verifier 与 Reward

- 官方 reward 计算（`patcher_bridge.py::_compute_soft_reward()`，此前会话已
  完整读取并记录在 `official_component_mapping.md`）与本仓库
  `evaluation/metrics.py`/verifier 逻辑的对齐结论：此前阶段已确认一致
  （soft_reward = sub-test 通过率，reward=1.0 为唯一"通过"判定），本次
  未重新逐行复核，**沿用此前结论**（`evaluation/metrics.py:33` 的注释
  `soft_reward: float = 0.0 # sub-test 通过率（匹配官方 _compute_soft_reward）`
  仍然存在，佐证此前的对齐结论未被后续改动破坏）。
- `evaluation/selr.py`（SELR，隐私泄露率指标）：官方框架代码中未找到参考
  实现，`docs/SIMPLIFICATIONS.md` 已披露为本仓库自行设计的指标（该结论
  不因本次 Part8 审计而改变）。
- **结论**：✅ 核心 reward/success_rate 公式对齐（沿用既有结论），SELR
  维持"自行设计、已披露"的现状。

---

## Part 9 — Randomness 与复现实验因素

- 官方 4 份 yaml **均未出现任何全局 `seed:` 字段**——`partitioner: replicate`/
  `round_robin` 本身是确定性的（不需要随机数），只有 `RandomPartitioner`
  才接受 `partitioner_seed`（默认 0），而官方 4 个实际使用的 setting 都没有
  用到 `random` partitioner。也就是说，**官方实验的"随机性"主要来自 LLM
  采样本身（temperature>0 时的真实非确定性），而不是某个可控的全局种子**。
- 本仓库 `setting_se.yaml`/`setting_homo_fed.yaml` 都写了 `seed: 42`，供
  `RandomSampler`/`HeterogeneousSampler`/`FamilyCurriculumSampler` 使用——
  但在实际生效的 `sampler: "family_curriculum"` 模式下，任务顺序由
  `ALL_TASK_DIFFICULTY_RANKING.json` 决定（确定性），`seed` 字段在这条路径
  上大概率不产生任何实际随机性影响（本次未逐行确认 `FamilyCurriculumSampler`
  是否真的忽略 seed，标记为 ❓ 待确认）。
- **retry/concurrency**：官方 `ANTHROPIC_MAX_RETRIES=999` + `llm_client.py`
  的无限重试哲学，意味着官方实验里"因限流导致的结果波动"被人为压到接近
  0；本仓库因缺少该环境变量（Part1 已记录），**限流相关的随机性/结果波动
  可能比官方更大**——这是本次审计里唯一一条把"randomness"与"retry 配置"
  直接关联起来的发现，与用户怀疑点 #5 相符。
- **结论**：⚠️ 主要风险点是 Part1 已记录的 retry 差异带来的"限流噪声"，
  而不是 seed 本身；seed 是否被 `FamilyCurriculumSampler` 实际消费需要后续
  确认（❓）。

---

## 汇总：跨 Part 发现的差异清单（供 Part10 分级引用）

| # | 差异 | 来源 Part | 分级建议 |
|---|---|---|---|
| 1 | `ANTHROPIC_MAX_RETRIES` 环境变量缺失 | Part1/6/9 | Tier A |
| 2 | `server.merger_max_tokens: 16384` 死配置，实际用 8192 | Part1/6 | Tier A |
| 3 | Setting2 `max_retry: 2` vs 官方 paper_logs 记录的 `0`（待确认是否有意为之） | Part6 | Tier A（确认后执行）/ 需先问询 |
| 4 | Stage2 库审计缺少"stale skills"检查 | Part5 | Tier B |
| 5 | `patch_prompt.txt` 未提前声明 Output precision / Known invariants 规则 | Part3 | Tier B |
| 6 | `_VERIFICATION_DISCIPLINE_BLOCK` 无官方对照物 | Part2 | 保留 + 披露（非迁移项） |
| 7 | Stage1/2 压缩版省略官方 Task buckets 写法示例/Workflow 步骤/Examples/Helper scripts | Part5 | Tier C（架构差异，不迁移） |

## 本次审计的诚实局限

以下官方组件/文件**源码不可得**（外部依赖，非本仓库能触及）：
- `harbor`（真实任务执行引擎，`official_component_mapping.md` 已披露）
- `libs.skill_evolution.patcher::SkillPatchEvolver`（真实 per-trial patch
  蒸馏 prompt 与逻辑，本次审计新发现的第二个不可得依赖）
- `libs.harbor_noinstall_agents.agents`（qwen-code/kimi-cli 的真实 CLI 参数
  语法）

以下项目因时间预算限制标记为 ❓ 未核实（建议下次会话优先处理，已在
model_alignment.md §9 列出）：Setting3/4 yaml 逐字段核对、Stage1/2 实际
temperature 数值、`FamilyCurriculumSampler` 是否消费 `seed`。
