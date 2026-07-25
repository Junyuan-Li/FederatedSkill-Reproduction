# Agent System Prompt 对齐报告（Part2）

> 对应用户 10-part 审计的 Part2。目标：判断当前仓库新增的
> `_VERIFICATION_DISCIPLINE_BLOCK`（"结束任务前必须验证"指令）在官方仓库中
> 是否已有等价物；若有，优先用官方版本；若没有，保留但记录为
> **reproduction deviation**。

## 1. 结论先行

**官方 skillfl adapter 层（`FederatedSkill-main` 仓库范围内可见的代码）没有
任何等价物。** 本仓库 `harness/cli_harness_base.py::_VERIFICATION_DISCIPLINE_BLOCK`
是一处**纯粹的 reproduction-only 新增**，按用户指令予以保留，本文件将其正式
记录为 deviation（`docs/SIMPLIFICATIONS.md` 需要补一条对应条目，见文末）。

## 2. 论证过程

### 2.1 官方 worker agent 的"system prompt"实际由什么构成？

在官方管线里，一次 worker 任务执行 = 启动一个 `harbor.Job`，把
**task 自己的 `task.toml`/`instruction.md`**（SkillFlow benchmark 任务自带的
任务说明）作为唯一的任务侧输入，交给 `claude`/`qwen-code`/`kimi-cli` 这些
**外部 CLI 二进制**执行。这些 CLI 工具本身在被安装时就带有各自厂商写死的
system prompt（例如 Claude Code 自己的"你是一个编程助手…"内置提示词）——
**这部分源码不在 `FederatedSkill-main` 仓库里，也不在任何可及的官方仓库里**
（`claude`/`qwen-code`/`kimi-cli` 是各自厂商闭源或独立发布的 CLI 产品）。

搜索证据（本次会话读取的官方文件）：
- `skillfl/skillflow_adapter/cli.py`：只做 yaml→`FedConfig`/`UserSpec` 的转换，
  从未拼接/注入过任何"任务无关的通用系统提示词"字符串。
- `skillfl/skillflow_adapter/merge.py::make_claude_code_subprocess_runner`/
  `make_podman_claude_runner`：只是把 `merge_skill/SKILL.md`（一个具体的、
  仅用于**云端合并 agent**的任务说明文件）作为 `--print` 的输入喂给容器内
  的 claude-code；这是 **Stage2 合并阶段**专用的 prompt，不是 worker 执行
  任务时的 prompt。
- 官方 4 份 `configs/*.yaml`：只配置 `agent.import_path`/`model_name`/`env`，
  没有任何 `system_prompt`/`instruction_override` 字段。

**结论**：官方架构里根本不存在一个"worker 执行任务前，adapter 层额外注入
一段验证纪律指令"的环节——worker 是否会在完成前自我验证，完全取决于
(a) claude-code/qwen-code/kimi-cli 各自 CLI 厂商写死的、本仓库不可见的
内置行为，(b) 具体 task 的 `instruction.md` 里是否自带验证要求（这是
per-task 内容，不是通用 adapter 层 prompt，二者性质不同）。

### 2.2 与本仓库当前实现的对比

`harness/cli_harness_base.py::_build_workspace_prompt()` 会在"已有技能参考"
段落之后、"输出格式"段落之前，插入
`_VERIFICATION_DISCIPLINE_BLOCK`（4 步：写真实输出文件 → 真正执行主脚本 →
回读校验 → 才允许宣称完成）。这是**本仓库 adapter 层**（`cli_harness_base.py`
对应官方的"launch a Job"环节）主动拼装的通用指令，会对**所有**任务生效——
这一点在官方架构里没有直接对应物：官方在这一层只传递 task 自身的
`instruction.md`，不会由 adapter 代码额外拼一段通用纪律文本。

### 2.3 是否可能"事实上等价"？

需要诚实说明一个局限：由于 claude-code 等 CLI 的内置 system prompt 本身
不可见，**不能 100%排除**官方 CLI 工具自己内置了类似"完成前请验证"的通用
行为准则（多数严肃的 coding agent CLI 确实倾向于这样做）。但：
1. 这属于"CLI 厂商自己的默认行为"，不是"FederatedSkill 论文/官方仓库的
   实验设计选择"——即使存在，也不是本次审计能够复制或对齐的对象（不是
   FederatedSkill 仓库的资产，无法"迁移"）。
2. 本仓库的 `_VERIFICATION_DISCIPLINE_BLOCK` 是为了修复一个**已确认的真实
   执行层 bug**（THREAD A：agent 算出正确答案但从不落盘/执行，verifier 因此
   拿不到真实产物）——这个 bug 的根因是本仓库的 harness 实现（並非 claude-code
   本身的问题），因此这段指令的必要性来自**本仓库自己的架构选择**（例如
   是否有独立的"强制执行"步骤、verifier 何时介入），而不是对官方某段缺失
   prompt 的还原。

## 3. 官方"prompt 资产"里唯一真正存在、且本仓库应参考的对象

官方唯一手写、可读、非外部依赖的 agent-facing prompt，是**云端合并阶段**
的两份 SKILL.md（`task_update_skill/SKILL.md`、`merge_skill/SKILL.md`），
对应本仓库的 `prompts/stage1_prompt.txt`/`stage2_prompt.txt`。这两份文件的
比对结果记录在主报告 [official_experiment_alignment_audit.md](official_experiment_alignment_audit.md#part-5)
的 Part5 一节，不在本文件重复。**这两份官方 prompt 都没有"结束任务前必须
验证"这一类指令**——它们讨论的是"如何审计/合并技能库"，不是"worker 执行
任务时的自我验证纪律"，两者是完全不同层级的 prompt（一个面向云端合并 agent，
一个面向本地任务执行 agent），不能互相替代或对照。

## 4. 处理结论（遵照用户 Part2 指令）

> "如果官方没有：保留增强版本，但记录为 reproduction deviation"

- **保留** `harness/cli_harness_base.py::_VERIFICATION_DISCIPLINE_BLOCK`
  及其在 `_build_workspace_prompt()` 里的拼接逻辑——不做任何代码修改。
- **记录为 reproduction deviation**：需要在 `docs/SIMPLIFICATIONS.md` 补充
  一条新条目（建议追加为 §6.6 内部小节或新增 §6.8，视当前编号而定），措辞：

  > ⚠️ **Reproduction Deviation**：`_VERIFICATION_DISCIPLINE_BLOCK`
  > （"结束任务前必须验证"指令）是本仓库在 adapter 层（`cli_harness_base.py`）
  > 主动拼接的通用任务执行纪律，官方 `FederatedSkill-main` 仓库的
  > adapter 层（`cli.py`/`merge.py`/4 份 `configs/*.yaml`）中未发现任何等价
  > 机制——官方只是把 task 自身的 `instruction.md` 转交给外部 CLI 工具
  > （claude-code/qwen-code/kimi-cli），不额外注入通用验证指令。本条指令是
  > 为修复"agent 算出答案但从不执行/落盘"这一真实执行层 bug 而添加的
  > reproduction-only 内容，非官方资产的还原。CLI 工具自身是否内置类似
  > 行为准则不可考（源码不可见），不纳入本次审计范围。

## 5. 本次未核对项

- `benchmark/**/instruction.md`（具体 task 自带的任务说明文本）是否本身
  已包含类似"验证后再交付"的要求——若已包含，`_VERIFICATION_DISCIPLINE_BLOCK`
  就存在与 task 自带说明重复的可能性（不影响正确性，但值得未来精简）。
  本次未逐个 family 抽查 `instruction.md` 内容，标记为后续可选核对项。
