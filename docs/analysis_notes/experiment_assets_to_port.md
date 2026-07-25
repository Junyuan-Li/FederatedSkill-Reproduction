# 实验资产迁移清单（Part10）

> 来源：[official_experiment_alignment_audit.md](official_experiment_alignment_audit.md)
> 的"跨 Part 发现的差异清单"。三级分类：
> **A** 必须直接迁移（否则结果不可比）　**B** 建议迁移　**C** 保持自主实现。

## Tier A — 必须迁移，否则结果不可比

### A1. 补上 `ANTHROPIC_MAX_RETRIES` 环境变量

- **文件**：`harness/claude_code_harness.py::build_env()`
  （以及需要核实 `qwen_code_harness.py`/`kimi_cli_harness.py` 是否有等价的
  OpenAI 协议重试环境变量——官方 4 份 yaml 里 qwen-code/kimi-cli worker 的
  `env` 块确实没有设置任何重试变量，所以这两个 harness 大概率不需要改动）。
- **官方依据**：4 份 `configs/*.local.yaml` 对每个 claude-code worker 都设置
  `ANTHROPIC_MAX_RETRIES: "999"`。
- **迁移方式**：`env.setdefault("ANTHROPIC_MAX_RETRIES", "999")`（用
  `setdefault` 而非强制覆盖，保持与该函数里其余两个 `setdefault` 调用一致
  的"不覆盖调用方显式设置"风格）。
- **不迁移的风险**：限流/瞬时错误下 trial 更容易直接失败，success_rate 被
  非算法因素拉低，无法公平对比论文数字。
- **状态**：待用户确认后实施（本次审计阶段未修改代码）。

### A2. 修复 `merger_max_tokens` 死配置

- **文件**：`experiments/run_experiment.py::_build_backbone()`
- **问题**：`setting_homo_fed.yaml::server.merger_max_tokens: 16384` 从未被
  读取，服务器 backbone 实际用 `DEFAULT_MAX_TOKENS=8192`。
- **官方依据**：`merger_llm.max_tokens: 16384`（Setting2/3/4 yaml）。
- **迁移方式**：在 `_build_backbone()` 里，当 `role == "server"` 时读取
  `cfg.get("merger_max_tokens")`（若存在），显式传给
  `LLMBackbone.from_worker_profile(profile, max_tokens=...)`。纯粹让已声明
  的配置生效，不改变任何 prompt/算法内容。
- **不迁移的风险**：Stage2 一次性输出多个 SKILL.md 全文 + DECISIONS.md +
  memory.md 时有截断风险，可能表现为 skill_growth 停滞或 JSON 解析失败。
- **状态**：待用户确认后实施。

### A3. 核实 Setting2 `max_retry: 2` 是否应改为 `0`

- **文件**：`experiments/configs/setting_homo_fed.yaml`
- **问题**：官方 `paper_logs` 真实运行记录里 `retry.max_retries=0`
  （agent 执行/超时失败不重试），本仓库 Setting1（SE）已对齐为 `0`，但
  Setting2（Homo Fed）仍是 `2`（注释称"失败 attempt 回滚 library；不改变
  算法状态"）。
- **需要澄清**：这条 `max_retry: 2` 是否有独立于"论文一致性"之外的工程
  理由（例如联邦模式下某个 worker 失败需要更宽容的重试以避免破坏其他
  worker 的同步节奏）——如果没有特殊理由，应统一改为 `0` 与官方一致。
- **状态**：**需要用户明确回答**（不属于"直接抄官方数字"就能安全操作的
  范畴，涉及联邦调度的工程权衡，遵照 Part6"不要凭经验修改"的指令）。

## Tier B — 建议迁移

### B1. `patch_prompt.txt` 提前声明 Output precision / Known invariants 规则

- **文件**：`prompts/patch_prompt.txt`
- **建议**：追加两条规则（原样借用 `stage2_prompt.txt` 已有的措辞）：
  - 数值型输出禁止 `round()`/格式化截断精度。
  - 若技能输出包含数值结果，patch 应在 SKILL.md 里包含
    `## Output precision` 小节。
- **收益**：从源头产出合规内容，减少 Stage2 合并阶段的返工。
- **注意**：官方真实 client 侧 patcher prompt 不可见（`libs.skill_evolution`
  外部依赖），无法确认官方是否也是"合并阶段才修正"——因此这是**建议性
  改进**，不是"官方已有、我们缺失"的确定性差异。

### B2. Stage2 库审计补充"stale skills"检查

- **文件**：`prompts/stage2_prompt.txt`
- **建议**：在"Library audit"一节追加第三类检查——"连续 N 轮未被任何
  worker 任务命中/引用的技能，应标记为候选删除项"，对应官方 `merge_skill/SKILL.md`
  的第三类审计（umbrella check + pairwise redundancy check + **stale
  skills check**，本仓库当前只有前两项）。
- **收益**：避免技能库"只增不减"，让 skill_growth 指标的可比性更真实。

## Tier C — 保持自主实现（不迁移）

- **核心算法**：`server/planner.py`/`server/merge.py` 的 Stage1/Stage2 决策
  逻辑、`PaperMergeAction`（ABSORB/REPAIR/REFACTOR/NO_UPDATE）enum 化实现、
  `CapabilityMatrix` covered/absorbing/broken/gap 四态判定。这些是本仓库
  依据论文 Section 4.2.1/4.2.2 原文做的**工程形式化**，官方对应实现是完全
  agentic 的容器内 claude-code 循环（无形式化 enum），二者语义等价但实现
  路径不同——重写成"完全 agentic 容器循环"属于架构级改动，超出本次
  "外围资产对齐"范围，且用户已明确排除对核心算法/aggregation 思想的修改。
- **`_VERIFICATION_DISCIPLINE_BLOCK`**（执行验证纪律指令）：官方无对照物，
  本仓库为修复真实执行层 bug 而保留，已记录为 reproduction deviation。
- **Stage1/2 压缩版 prompt 省略的官方细节**（Task buckets 写法示例/7 步
  Workflow 编号/worked Examples/Helper scripts 清单）：这些内容依赖官方
  "容器内可调用 shell 脚本的 agentic loop"这一执行模型，本仓库 Stage1/2 是
  直接 LLM completion 调用，不具备调用 helper scripts 的能力，因此这些
  内容本身不可迁移（迁移了也无法执行），保留现状。
- **SELR 指标**：官方框架代码中未找到参考实现，维持本仓库自行设计、已在
  `docs/SIMPLIFICATIONS.md` 披露的现状。

## 下一步

1. 就 Tier A 的 A1/A2（低风险、有明确官方数字可抄）与 A3（需要工程判断）
   分别征求用户确认。
2. 确认后实施代码修改 + 补充回归测试 + 全量 pytest。
3. 在 `docs/SIMPLIFICATIONS.md` 补记录 Part2 的 reproduction deviation
   （`_VERIFICATION_DISCIPLINE_BLOCK`）。
4. 运行最小验证实验（1 family，8 tasks，Setting1）产出
   `before_after_alignment_report.md`（需要用户确认是否现在执行真实 API
   调用，涉及成本与终端断连风险，见会话记忆里此前对 Part7 的同类提醒）。
