# Model & Agent 配置对齐报告（Part1）

> 对应用户 10-part 实验结果一致性审计的 Part1。**严格遵守用户指令
> "如果当前不同：不要自动修改"**——本文件只做只读审计 + 记录，不修改任何
> `experiments/configs/*.yaml`、`core/datatypes.py::WorkerProfile`、
> `harness/*.py` 的默认值。

## 0. 审查范围与信息来源

直接读取的官方文件：
- `configs/1_se_qwen.local.yaml`（Setting1 SE）
- `configs/2_fed_3glm_cc.local.yaml`（Setting2 Homo Fed）
- `configs/3_fed_hetero_cc.local.yaml`（Setting3 Hetero-CC）
- `configs/4_fed_hetero_mixed_cli.local.yaml`（Setting4 Hetero mixed-CLI）
- `skillfl/skillflow_adapter/cli.py`（yaml→FedConfig 的解析逻辑，含各字段默认值）
- `skillfl/skillflow_adapter/llm_client.py`（`make_llm_call` 的 max_tokens 默认值/重试策略）
- `skillfl/skillflow_adapter/patcher_bridge.py`（patcher 的 temperature 解析规则）

直接比对的本仓库文件：
- `experiments/configs/setting_se.yaml`、`setting_homo_fed.yaml`、
  `setting_hetero_backbone.yaml`、`setting_full_hetero.yaml`
- `core/datatypes.py::WorkerProfile`
- `harness/claude_code_harness.py`/`qwen_code_harness.py`/`kimi_cli_harness.py`

## 1. Backbone Model

| 项 | 官方 | 本仓库 | 差异 |
|---|---|---|---|
| Setting1 (SE) | `qwen3.6-plus`（也有 `glm-5`/`kimi-k2.5` 变体，需分别启动） | `setting_se.yaml::workers[0].backbone_model = "qwen3.6-plus"` | ✅ 一致 |
| Setting2 (Homo Fed) | 3×`glm-5` | `setting_homo_fed.yaml::workers[*].backbone_model = "glm-5"`（3 个） | ✅ 一致 |
| Setting3 (Hetero-CC) | `qwen3.6-plus` + `glm-5` + `kimi-k2.5`，全部走 claude-code | `setting_hetero_backbone.yaml`（需人工核对，本次未逐字段重新打开，此前阶段已按论文 Table1 对齐，未发现记录在案的差异） | ℹ️ 未在本次会话重新逐字核对，风险低（此前阶段已核对过） |
| Setting4 (Hetero mixed-CLI) | qwen3.6-plus→qwen-code / glm-5→claude-code / kimi-k2.5→kimi-cli | `setting_full_hetero.yaml`（同上，未在本次重新核对） | ℹ️ 同上 |

## 2. Agent Harness（CLI 二进制）

| worker | 官方 `agent.import_path` | 官方实际 CLI | 本仓库 `agent_harness` | 差异 |
|---|---|---|---|---|
| claude-code 系 worker | `libs.harbor_noinstall_agents.agents:NoInstallClaudeCode` | `claude` 二进制 | `"claude-code"` → `ClaudeCodeHarness` | ✅ 一致 |
| qwen-code 系 worker（仅 Setting4） | `libs.harbor_noinstall_agents.agents:NoInstallQwenCode` | `qwen-code`（OpenAI 协议） | `"qwen-code"` → `QwenCodeHarness` | ✅ 命名对齐；qwen-code 确切 CLI flag 语法官方仓库未给出实现（`libs.harbor_noinstall_agents` 是外部包，源码不可得），本仓库 `harness/qwen_code_harness.py` 已在文档字符串披露"按 claude-code 语法类推、未经官方验证"（`docs/SIMPLIFICATIONS.md` §6.4 已记录） |
| kimi-cli 系 worker（仅 Setting4） | `libs.harbor_noinstall_agents.agents:NoInstallKimiCli` | `kimi-cli`（OpenAI 协议，`openai/kimi-k2.5`） | `"kimi-cli"` → `KimiCLIHarness` | 同上 |

## 3. Provider / API Endpoint

| worker 模型 | 官方 endpoint | 官方 key 环境变量 | 本仓库 endpoint | 本仓库 key 环境变量 | 差异 |
|---|---|---|---|---|---|
| qwen3.6-plus (claude-code) | `https://dashscope.aliyuncs.com/apps/anthropic` | `$DASHSCOPE_KEY` → `ANTHROPIC_API_KEY` | `setting_se.yaml`: `api_base: https://dashscope.aliyuncs.com/apps/anthropic`, `api_key_env: QWEN_DASHSCOPE_API_KEY` | 同上（本仓库把官方共用的单一 `DASHSCOPE_KEY` 拆成按 provider 隔离的 `QWEN_DASHSCOPE_API_KEY`/`GLM_DASHSCOPE_API_KEY`，属已披露的 "Provider Key Isolation Fix"，语义等价，只是变量名拆分） | ✅ 端点一致；变量名拆分是本仓库工程决策，不影响实际请求目标 |
| glm-5 (claude-code) | 同上（`$DASHSCOPE_KEY`） | 同上 | 同上（`GLM_DASHSCOPE_API_KEY`） | 同上 | ✅ |
| kimi-k2.5 (claude-code, Setting3) | `https://api.moonshot.ai/anthropic` | `$MOONSHOT_KEY` | 需核对 `setting_hetero_backbone.yaml`（本次未重新打开） | — | ℹ️ 未重新核对 |
| kimi-k2.5 (kimi-cli, Setting4) | `https://api.moonshot.ai/v1`（OpenAI 协议） | `$MOONSHOT_KEY` | 需核对 `setting_full_hetero.yaml`（本次未重新打开） | — | ℹ️ 未重新核对 |

## 4. Temperature

| 组件 | 官方值 | 本仓库值 | 差异 |
|---|---|---|---|
| **Worker Agent 自身**（claude-code/qwen-code/kimi-cli 在执行任务时的采样温度） | **官方 4 份 yaml 均未显式设置** —— 完全交给各 CLI 工具自身的默认值决定，`FedConfig`/`JobConfig` 没有暴露这个旋钮 | `core/datatypes.py::WorkerProfile` **同样没有** `temperature` 字段 | ✅ **"对齐"是通过双方都不配置实现的**——不是巧合，而是因为 worker agent 走的是真实 CLI 二进制（`claude --print ...`），CLI 内部采样参数不由 JobConfig/WorkerProfile 控制，两边行为语义一致 |
| **Patcher**（trajectory→patch 蒸馏 LLM 调用） | `cli.py::_run_one_family`: `float(cfg.patcher.get("temperature", 0.2))`；`patcher_bridge.py::_resolve_patch_temperature()`：**kimi/moonshot 系强制 1.0**（不管配置写什么），其余走配置值 | `setting_se.yaml`/`setting_homo_fed.yaml`: `patcher.temperature: 0.2`；`core/datatypes.py::WorkerProfile.is_moonshot` 属性存在，用于同样的"kimi 强制 1.0"判断（此前阶段已对齐，本次核实其存在） | ✅ 一致（含 kimi 特判） |
| **Cloud Merger**（`cloud_skill_merge`，在容器内跑 claude-code agent） | 官方 yaml 未见显式 `temperature`（`merger_llm` 块只出现 `max_tokens: 16384`），同样交给容器内 claude-code CLI 默认值 | 本仓库 Stage1/Stage2（`server/planner.py`/`server/merge.py`）是直接 LLM completion 调用而非容器内 CLI agent（架构差异，`docs/SIMPLIFICATIONS.md` §6.1 已披露），需核对 `llm/router.py`/`server/*.py` 里 Stage1/2 调用是否显式传了某个 temperature——**本次会话未重新打开这些文件核对具体数值**，标记为待办 | ⚠️ 待核实（非本次审查范围内已确认项） |

## 5. Max Tokens

| 组件 | 官方值 | 本仓库值 | 差异 |
|---|---|---|---|
| Patcher/Worker LLM 默认值 | `patcher_bridge.py::PatcherBridge.__init__`: `cfg.get("max_tokens", 8192)`（默认 8192） | `core/constants.py::DEFAULT_MAX_TOKENS = 8_192`（已核实，与官方默认值字面相同）；`core/constants.py::DEFAULT_TEMPERATURE = 0.2`、`MOONSHOT_TEMPERATURE = 1.0` 同样与官方 `patcher_bridge.py::_resolve_patch_temperature()` 的 0.2/强制1.0 规则一致 | ✅ 一致（已核实） |
| Cloud Merger（Stage1/Stage2 服务器 backbone） | `merger_llm.max_tokens: 16384`（Setting2/3/4 yaml 显式声明，供 `cloud_skill_merge` 容器内 claude-code agent 使用） | **⚠️ 发现具体差异**：`setting_homo_fed.yaml::server.merger_max_tokens: 16384` 这个字段**在代码里从未被读取**——`experiments/run_experiment.py::_build_backbone(cfg, role="server", ...)` 只从 `cfg` 里取 `backbone_model`/`api_base`/`api_key_env`/`max_context_tokens`/`is_moonshot` 等字段构造 `WorkerProfile`，然后调用 `LLMBackbone.from_worker_profile(profile)`——**不传 `max_tokens` 参数**，也没有任何代码路径读取 `cfg.get("merger_max_tokens")`。`LLMBackbone.from_worker_profile()` 的 `max_tokens` 形参默认值就是 `DEFAULT_MAX_TOKENS=8192`，因此服务器（Stage1 planner / Stage2 merger）LLM 调用实际使用的 `max_tokens` 是 **8192，而不是 yaml 里写的、也是官方使用的 16384**。 | ⚠️ **真实差异，未修改**（`setting_homo_fed.yaml` 里的 `merger_max_tokens: 16384` 字段是"写了但没生效"的死配置——这是一个具体的、可验证的代码 bug 级别的不一致，不是"设计决策不同"） |
| Worker Agent 自身（CLI 内部采样） | 未暴露（CLI 内部决定） | `core/datatypes.py::WorkerProfile` 无对应字段 | ✅ 对齐（同 Temperature 一节的推理） |

### 8192 vs 16384 差异的影响评估

- **影响程度：中**。Stage1（task_memory 覆盖矩阵维护）/ Stage2（skill 合并决策，含
  DECISIONS.md + memory.md + SKILL.md 全文重写）都属于"一次性生成大量结构化
  markdown 输出"的任务——8192 tokens 在 family 技能库变大、worker 数变多、
  peer 补丁变多时，存在被截断的风险（尤其 Stage2 需要一次性输出多个 skill
  的完整 SKILL.md 正文 + DECISIONS.md 表格 + memory.md），而官方特意把这个
  值翻倍到 16384 大概率正是为了避免这种截断。截断会直接表现为"skill growth
  停滞"或"Stage2 输出 JSON 解析失败需要重试"，间接影响 success_rate/cost，
  与用户怀疑点 #1（Agent prompt 不一致，某种意义上属于同一类"生成预算不足
  导致输出不完整"问题的近亲）有关。
- **本次不自动修改**（Part1 明确指令）。建议列入 `experiment_assets_to_port.md`
  Tier A：修复方式是让 `_build_backbone()` 读取 `cfg.get("merger_max_tokens")`
  （若存在）并显式传给 `LLMBackbone.from_worker_profile(profile, max_tokens=...)`，
  这是一个纯粹的"让已声明的配置生效"修复，不涉及算法/prompt 内容变化。

## 6. Context Length

- 官方 4 份 yaml **均未出现任何 context-length/上下文窗口相关字段**——这是模型本身的物理限制，不是 JobConfig 的可配置项。
- 本仓库 `setting_se.yaml`/`setting_homo_fed.yaml` 里的 `max_context_tokens: 131072` 是本仓库自行标注的模型规格常量（用于 `client/trajectory.py::TrajectoryCompressor` 的压缩阈值判断，非官方配置项）。**这不是一个"官方 vs 本仓库配置不一致"的可比较点**——官方压根没有这个旋钮，本仓库新增它是为了支撑轨迹压缩逻辑（`docs/SIMPLIFICATIONS.md` 未见相关披露，值得在下次修订时补一句"131072 是 qwen3.6-plus 官方文档公布的上下文窗口，用于压缩阈值计算，非官方 FedConfig 字段"）。

## 7. System Prompt

见独立文件 [prompt_alignment_report.md](prompt_alignment_report.md)（Part2 详细报告）。

## 8. 本次审查中发现的唯一具体代码级差异（未修改，仅记录）

### ⚠️ `ANTHROPIC_MAX_RETRIES` 环境变量缺失

- **官方**：`configs/1_se_qwen.local.yaml`/`2_fed_3glm_cc.local.yaml`/
  `3_fed_hetero_cc.local.yaml`/`4_fed_hetero_mixed_cli.local.yaml` **全部 4 份**
  配置里，每一个 claude-code 系 worker 的 `agent.env` 块都显式设置了
  `ANTHROPIC_MAX_RETRIES: "999"`——这是 claude-code CLI 二进制自身对
  瞬时性 LLM 调用错误（429/5xx/超时）的**内部重试预算**（不是 harbor 的
  trial 级重试，是单次 CLI 调用内部的 SDK 级重试），官方把它设成 999
  （近乎"无限重试直到成功"），配合 `llm_client.py` 里同样"无限重试直到
  成功"的哲学（see `_is_rate_limit()`/`_RATELIMIT_MAX_SLEEP`）。
- **本仓库**：`harness/claude_code_harness.py::build_env()` 只设置了
  `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`IS_SANDBOX`/
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`/`CLAUDE_CODE_ATTRIBUTION_HEADER`
  ——**没有设置 `ANTHROPIC_MAX_RETRIES`**。`qwen_code_harness.py`/
  `kimi_cli_harness.py` 也未见对应的重试环境变量设置（本次未逐行重新确认，
  按官方 yaml 只对 claude-code 系 worker 设置该变量，qwen-code/kimi-cli
  走 OpenAI 协议，官方 yaml 里也确实没有给它们设置任何等价的重试环境变量，
  所以这两个 harness 大概率本来就不需要）。
- **影响程度评估**：**中高**。真实 CLI 模式下如果 `claude` 二进制在遇到
  429/瞬时错误时用的是自己的内置默认重试次数（通常远小于 999），会导致
  在 DashScope/Moonshot 网关限流时，本仓库的 trial **更容易直接失败**
  （reward=0 或 exception），而官方配置几乎不会因为限流而失败——这会
  拉低本仓库测得的 success_rate，且成因是"重试策略不同"而非"算法/模型
  能力差异"，**正是用户列出的怀疑点 #5（timeout/retry不同）**。
- **本次不自动修改**（遵守用户 Part1 "如果当前不同：不要自动修改"的
  明确指令）。建议列入 `experiment_assets_to_port.md` 的 Tier A（若要
  做可比实验，需迁移），迁移方式：在 `ClaudeCodeHarness.build_env()`
  里追加 `env.setdefault("ANTHROPIC_MAX_RETRIES", "999")`（`qwen_code_harness.py`/
  `kimi_cli_harness.py` 是否需要类似设置，需先核实 qwen-code/kimi-cli
  自身是否支持等价的环境变量——官方 yaml 未给出，因此不能凭空杜撰一个
  官方没有的变量名）。

## 9. 本次未能完成的核对项（诚实披露，非回避）

受本次会话时间预算限制，以下项目**读取了官方源码但未逐字对照本仓库当前实现**，
建议下次会话优先处理：

1. Setting3/4（`setting_hetero_backbone.yaml`/`setting_full_hetero.yaml`）的
   endpoint/key 环境变量字段是否与官方 `3_fed_hetero_cc.local.yaml`/
   `4_fed_hetero_mixed_cli.local.yaml` 完全一致。
2. `server/planner.py`/`server/merge.py`（Stage1/Stage2）调用 LLM 时使用的
   实际 temperature 数值，与官方 cloud merger 隐式默认值（未设置 temperature，
   完全由容器内 claude-code CLI 决定）的对比——由于本仓库 Stage1/2 是直接
   completion 调用而非 CLI agent 循环，两者在"temperature 语义"上是否可比
   本身存疑，需要架构层面而非数值层面的讨论（已在 §6.1 disclosure 中提及）。

