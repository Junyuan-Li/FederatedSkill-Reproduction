# FederatedSkill — 论文复现代码框架说明（精校版）

> **论文原文**：*FederatedSkill: Federated Learning for Agentic Skill Evolution*
> Yang et al., EMNLP 2025 / arXiv:2606.03143
> **复现目标**：自主实现，设计模式不同于官方代码，核心算法与数据结构逐条对齐论文
> **本文档状态**：已逐文件阅读源码核实（函数签名 / 默认参数 / 校验逻辑），标注了已验证正确的部分和已发现的问题；已额外完成一轮"非 Toy 复现"专项核查（见 §零），确认核心链路均为真实执行/真实 API 调用，而非返回固定值的桩代码

---

## 零、非 Toy 复现验证（核心函数真实性核查，本轮新增）

> 目的：逐一核查论文核心链路对应的函数**内部实现**，而不只是核查"函数是否存在/签名是否匹配"。
> 判定标准：函数体内是否有真实的子进程执行 / 真实的网络 API 调用 / 真实的数据结构变换与校验，
> 还是仅仅 `return` 一个写死的常量或 `pass`。全部结论均基于直接阅读源码得出，附精确行号可复核。

| 论文环节 | 核心函数（可直接跳转） | 真实实现证据（非 stub/mock 的具体依据） |
|---|---|---|
| §4.1.1 任务执行 → 轨迹 τ_i | [client/executor.py](client/executor.py#L109) `TaskExecutor.run()` | 真实调用 `router.get(worker_id).call()` 发起 LLM 生成，再交给 `benchmark.verifier` 做真实执行验证；无任何写死的成功/失败返回值 |
| §4.1.1 真实 Agent Harness（Model→Tool Calling→Environment→Test） | [executor/agent_executor.py](executor/agent_executor.py#L96) `AgentWorkspaceExecutor.run()` | 用 [executor/environment.py](executor/environment.py) `WorkspaceManager` 在磁盘上创建**真实临时目录**，解析 LLM 输出的多文件代码块并逐个 `write_file` 落盘，再用 [executor/runner.py](executor/runner.py#L56) `CommandRunner` 以 `cwd=workspace` 发起**真实 `subprocess` 子进程**执行；执行后用 `ws.diff_generated_files()` 比对文件系统真实变化得到 `generated_files`，不是拼接字符串模拟 |
| R_{i,x}(τ) 执行奖励 | [benchmark/verifier.py](benchmark/verifier.py#L101) `PythonSandboxVerifier._run_script()` | `subprocess.run([sys.executable, tmp_path], capture_output=True, timeout=...)`，按**真实退出码** `proc.returncode == 0` 判定 reward，附真实 `stdout`/`stderr`/`TimeoutExpired` 处理，不是正则匹配代码文本 |
| R_{i,x}(τ)（函数级测试） | [benchmark/verifier.py](benchmark/verifier.py#L95) `FunctionTestVerifier.verify()` | 动态拼接注入 `entry_point` 函数并生成**逐用例可执行脚本**（`repr()` 精确值比较 + 异常捕获），子进程运行后解析真实 `passed/total/failures` JSON 输出 |
| δ_i^t = g_i(L_i^t, τ_i, ρ_i) patch 蒸馏 | [client/distiller.py](client/distiller.py#L120) `PatchDistiller.distill()` | 七步流水线中 `_step5_call_llm()` 真实调用 `router.get(worker_id).call_json()`（LLM 判断如何更新技能库），并非返回预置 patch；`_audit_privacy()` 对生成内容做真实正则扫描 |
| LLM 调用（论文 §4.1.2/§4.2） | [llm/backbone.py](llm/backbone.py#L233) `LLMBackbone._call_with_retry()` | `import litellm; litellm.completion(**kwargs)` **真实发起 HTTP API 请求**到 DashScope/Moonshot/Anthropic 端点，`litellm.completion_cost()` 计算真实费用，`resp.usage.prompt_tokens/completion_tokens` 取真实返回值（非硬编码 token 数） |
| P^t = (C^t, M^t, D^t) Stage1 规划 | [server/planner.py](server/planner.py#L66) `EvolutionPlanner.plan()` | 真实调用服务器 backbone `self._backbone.call_json()` 让 LLM 产出能力矩阵/记忆/指令；调用失败时才降级为 `_fallback_plan()`（保守不变，而非伪造成功） |
| Δ_i^t Stage2 个性化合并 | [server/merge.py](server/merge.py#L59) `EvolutionExecutor.execute_for_worker()` | 仅 `directive is None` 或 `action == SkipUpdate.NO_UPDATE` 时走空 patch 快速路径（真实的"维持现状"语义），其余动作（`PaperMergeAction.ABSORB`/`REPAIR`/`REFACTOR`）均真实调用服务器 backbone 生成合并后的补丁内容 |
| 统一 LLM 接口 `generate()` | [llm/generate.py](llm/generate.py#L83) `generate()` | 内部 `time.monotonic()` 实测真实调用耗时得到 `latency`，`_build_backbone()` 按 `model` 名真实路由到 `LLMBackbone` 并转发到上面同一条 `litellm.completion()` 真实调用链，不是独立的假实现 |
| Capability Matrix C^t 演化 | [evaluation/capability_tracker.py](evaluation/capability_tracker.py#L86) `CapabilityEvolutionTracker.record()` | 遍历真实 `CapabilityMatrix.cells`（来自 Stage1 LLM 输出经 [server/capability.py](server/capability.py) 更新后的真实状态）逐格计数，而非模拟固定分布 |
| 隐私压缩/SELR 近似 | [evaluation/metrics.py](evaluation/metrics.py#L145) `FederatedMetrics.sensitive_entity_leakage_rate()` | 用真实正则模式（task_id/IP/邮箱/电话/凭据/路径）在两段真实文本上做实体抽取 + 子串匹配统计，非固定返回值（但仍是近似值，非论文完整 LLM 裁判流程，见 §七 7.1 的诚实说明） |
| 完整实验回路（Setting1-4） | [experiments/runner.py](experiments/runner.py#L83) `ExperimentRunner.run()` → [experiments/run_experiment.py](experiments/run_experiment.py) | 委托给已跑通的 `run_experiment()`：真实读取 YAML、真实构造 `WorkerProfile`/`LLMBackbone`、真实执行 `SelfEvolutionRunner`/`FederatedRunner` 多轮循环，`--dry-run` 才会跳过真实 LLM 调用（仅用于配置校验，且会在输出中明确标注 `[DRY-RUN]`） |

**核查方法**（本轮新增）：对每个函数直接读取完整函数体源码（而非只读签名/docstring），确认内部是否存在 `subprocess.run`/`litellm.completion`/真实文件 I/O/真实数据结构遍历等**可观察的副作用或外部调用**；凡是"只 `return` 常量""`pass`""硬编码结果"的实现，会在上表中明确标注为"⚠️ 近似/占位"而非笼统打勾（本次核查未发现新的占位实现；已知的占位/简化点均已在 §七 中如实记录，如 `verification.type="none"` 的 20 个真实 SkillFlow family、`privacy_gain()` 的 token 级近似）。

---

## 一、复现完成度总览

| 层 | 完成度 | 对应论文节 | 关键文件 |
|----|--------|-----------|---------|
| 数据结构（core/） | ✅ 完整，含 Pydantic v2 校验 | §3 形式化定义 | [core/datatypes.py](core/datatypes.py), [core/constants.py](core/constants.py), [core/exceptions.py](core/exceptions.py) |
| LLM 调用层（llm/） | ✅ 完整；⚠️ 曾有路由 bug（已修复，见六.14） | §4.1.2, §4.2 | [llm/backbone.py](llm/backbone.py), [llm/providers.py](llm/providers.py), [llm/router.py](llm/router.py) |
| 客户端 patch 蒸馏（client/） | ✅ 完整，7 步流水线 | §4.1.1–4.1.2 | [client/distiller.py](client/distiller.py), [client/trajectory.py](client/trajectory.py), [client/library.py](client/library.py) |
| Agentic 执行运行时（client/agent_runtime/） | ✅ 完整（Planner-Action-Observation 循环） | §4.1.1 | [client/agent_runtime/agent.py](client/agent_runtime/agent.py) |
| 服务端演化（server/） | ✅ 完整 Stage1+Stage2 | §4.2.1–4.2.2 | [server/planner.py](server/planner.py), [server/merge.py](server/merge.py), [server/evolution.py](server/evolution.py) |
| Benchmark 任务系统（benchmark/） | ✅ 完整；含 25 个真实 SkillFlow family JSON | §5.1 | [benchmark/task.py](benchmark/task.py), [benchmark/family.py](benchmark/family.py), [benchmark/families/](benchmark/families/) |
| 实验评估（evaluation/） | ✅ 完整；⚠️ `privacy_gain()` 非严格 SELR | §5.2–5.4 | [evaluation/metrics.py](evaluation/metrics.py), [evaluation/evaluator.py](evaluation/evaluator.py) |
| 实验控制器（experiments/） | ✅ 完整（统一 `run_experiment.py` 入口 + `ExperimentRunner` 门面类 + Setting 1-4 + 3 项消融） | §5.1 Setting 1–4 / Algorithm 1 | [experiments/run_experiment.py](experiments/run_experiment.py), [experiments/baseline.py](experiments/baseline.py), [experiments/federated.py](experiments/federated.py), [experiments/runner.py](experiments/runner.py) |
| 真实 Agent Workspace 执行运行时（executor/） | ✅ 完整（mock/python/skillflow 三种执行器 + 独立 workspace/runner/trajectory 组件） | §4.1.1（真实 harness） | [executor/base.py](executor/base.py), [executor/environment.py](executor/environment.py), [executor/runner.py](executor/runner.py), [executor/trajectory.py](executor/trajectory.py), [executor/agent_executor.py](executor/agent_executor.py), [executor/skillflow_executor.py](executor/skillflow_executor.py) |
| 真实 SkillFlow benchmark 接入（benchmark/skillflow_adapter/） | ✅ 完整；含 synthetic fallback | §5.1 | [benchmark/skillflow_adapter/](benchmark/skillflow_adapter/) |
| 统一 LLM 调用接口（llm/generate.py） | ✅ 新增；Qwen/GLM/Kimi/Claude 统一 `generate(model, prompt, json_mode)` + token/cost/latency 统计 | §4.1.2 | [llm/generate.py](llm/generate.py) |
| Capability Matrix 跨轮演化追踪（evaluation/capability_tracker.py） | ✅ 新增；covered/absorbing/broken/gap 计数历史 + CSV 导出 | §4.2.1 | [evaluation/capability_tracker.py](evaluation/capability_tracker.py) |
| Prompt 配置包（prompts/） | ✅ 新增；Stage1/Stage2 提示词保留官方原文作为实验配置，patch 提示词本项目自研；算法实现（`server/planner.py`/`server/merge.py`）独立不复制官方 `.py` | Appendix B | [prompts/\_\_init\_\_.py](prompts/__init__.py), [prompts/stage1_prompt.txt](prompts/stage1_prompt.txt), [prompts/stage2_prompt.txt](prompts/stage2_prompt.txt), [prompts/patch_prompt.txt](prompts/patch_prompt.txt) |

---

## 二、当前目录结构（与磁盘实际内容一致）

```
FederatedSkill-Reproduction/
│
├── core/                       # 全局数据类型 + 常量 + 异常
│   ├── constants.py            # K_STEP / K_OBS / 温度 / 重试参数等超参数
│   ├── datatypes.py            # 全部 Pydantic v2 模型（ρ_i / τ_i / δ_i^t / C^t / M^t ...）
│   └── exceptions.py           # 异常层次（见 §3.3）
│
├── llm/                        # LLM 调用抽象层（litellm 统一路由）
│   ├── backbone.py             # LLMBackbone：单模型调用器 + 双级重试 + resolve_litellm_model()
│   ├── providers.py            # Provider 注册表（DashScope / Moonshot / Anthropic）+ 模型→provider 映射
│   ├── router.py                # BackboneRouter：per-worker 路由表
│   ├── prompt_builder.py       # PatchDistiller 用的 (system, user) prompt 构建（system 部分现通过 prompts.load_prompt() 加载 prompts/patch_prompt.txt，纯英文，规避编码问题）
│   ├── retry.py                # ErrorBucket 分类 + 指数/线性退避
│   ├── json_parser.py          # 从 LLM 响应中三级降级提取 JSON
│   ├── generate.py             # ✅ 新增：统一 generate(model, prompt, json_mode) 接口（薄封装，路由 Qwen/GLM/Kimi/Claude）
│   └── llm_client.py           # 兼容层（旧接口）
│
├── prompts/                    # ✅ 新增：Prompt 文本包（与算法代码解耦）
│   ├── __init__.py              # load_prompt(filename)：按文件名读取本目录下的 .txt 模板
│   ├── stage1_prompt.txt        # Stage1 系统提示词，保留自官方 task_update_skill/SKILL.md（实验配置，非算法源码）
│   ├── stage2_prompt.txt        # Stage2 系统提示词，保留自官方 merge_skill/SKILL.md（同上）
│   └── patch_prompt.txt         # Patch 蒸馏提示词，本项目自研（官方 patcher 提示词来自不可见的外部库，从未接触过原文）
│
├── client/                     # 客户端执行 + 蒸馏
│   ├── executor.py              # TaskExecutor：5 步 pipeline（检索→prompt→生成→沙箱执行→验证）
│   ├── distiller.py             # PatchDistiller：7 步 pipeline，δ_i^t = g_i(L_i^t, τ_i, ρ_i)
│   ├── library.py               # SkillLibrary：snapshot() / digest() / apply_patch() / rollback()
│   ├── trajectory.py             # TrajectoryCompressor：K_step/K_obs 压缩
│   ├── federated_client.py     # FederatedClient：把三者封装为统一接口
│   └── agent_runtime/           # 完整 agentic 循环（Planner-Action-Observation）
│       ├── agent.py             # AgentRuntime
│       └── tools.py             # BuiltinTools / ToolRegistry
│
├── server/                     # 服务端演化
│   ├── planner.py               # Stage 1：EvolutionPlanner → EvolutionPlan P^t
│   ├── merge.py                  # Stage 2：EvolutionExecutor（per-client 个性化合并）→ MergedPatch Δ_i^t
│   ├── evolution.py              # FederatedServer：串联 Stage1+Stage2 的 run_round()
│   ├── capability.py             # CapabilityTracker：能力矩阵 C^t
│   ├── memory.py                 # EvolutionMemoryStore：双层记忆 M^t
│   ├── prompt_builder.py        # Stage1PromptBuilder / Stage2PromptBuilder（现通过 prompts.load_prompt() 加载 prompts/stage{1,2}_prompt.txt，不再内嵌大段文本）
│   └── logging.py                # DecisionLog 落盘（Appendix B.4 DECISIONS.md）
│
├── benchmark/                   # 任务 benchmark 系统
│   ├── task.py                  # Task / VerificationSpec 数据模型
│   ├── family.py                 # TaskFamily：同一技能的递增难度任务序列
│   ├── curriculum.py             # FamilyCurriculumSampler：按 round 递增采样同一 family
│   ├── sampler.py                 # RandomSampler / HeterogeneousSampler（旧版随机/异质采样）
│   ├── skillflow_benchmark.py    # FamilyTaskSampler（curriculum 模式的另一实现，供 run_experiment.py 用）
│   ├── verifier.py                # PythonSandboxVerifier / FunctionTestVerifier / OutputMatchVerifier / SkillFlowScriptVerifier
│   ├── loader.py                  # 旧版任务加载（default_tasks.json）
│   ├── evaluator.py               # 轻量轮次指标快照（RoundMetrics/ExperimentSummary）
│   ├── check_dataset.py           # 数据集完整性检查脚本
│   ├── tasks/
│   │   └── default_tasks.json     # 旧版：20 个互相独立的 Python 编程任务
│   ├── families/                  # 新版：25 个 family JSON（5 个手写 + 20 个真实 SkillFlow）
│   ├── cache/SkillFlow-Task/       # 从 HuggingFace 下载的原始 SkillFlow 数据缓存
│   └── skillflow_adapter/         # 真实 SkillFlow → Task/TaskFamily 格式转换适配器
│       ├── parser.py              # parse_task_dir()：解析 task.toml/instruction.md/environment/tests
│       ├── converter.py           # to_task()：RawSkillFlowTask → Task/VerificationSpec
│       ├── loader.py              # load_skillflow_family() / load_skillflow_benchmark()
│       ├── downloader.py          # download_skillflow_dataset() / is_dataset_present()（HuggingFace snapshot_download）
│       └── download.py            # ✅ 新增：downloader.py 的文件名别名（纯转发，不重复实现）
│
├── executor/                    # 真实任务执行沙箱（无 Docker/Harbor 依赖）
│   ├── mock_executor.py         # 单测用 mock
│   ├── python_executor.py       # 通用 Python 代码沙箱执行
│   ├── skillflow_executor.py    # SkillFlowTaskExecutor：真实 SkillFlow 任务（type=skillflow_script）执行器
│   ├── base.py                  # ✅ 新增：BaseExecutor 抽象接口（统一 mock/python/skillflow/agent workspace 执行器）
│   ├── environment.py           # ✅ 新增：WorkspaceManager，真实文件系统工作区隔离
│   ├── runner.py                # ✅ 新增：CommandRunner/CommandResult，subprocess 命令执行封装
│   ├── trajectory.py            # ✅ 新增：TrajectoryCollector，记录 actions/tool_calls/generated_files/exceptions
│   └── agent_executor.py        # ✅ 新增：AgentWorkspaceExecutor，组合以上四者的真实 agent workspace 执行模式
│
├── evaluation/                  # 实验评估 + 报告
│   ├── metrics.py                # 论文 6 项核心指标（静态方法）
│   ├── evaluator.py               # ExperimentEvaluator：多轮实验汇总
│   ├── reporter.py                # 控制台 + CSV 报告
│   ├── plotter.py                 # Figure 2/3/4 绘图
│   ├── privacy.py                 # 隐私相关辅助计算
│   ├── results_exporter.py        # 结果导出（Table/CSV）
│   └── capability_tracker.py      # ✅ 新增：CapabilityEvolutionTracker，跨轮 covered/absorbing/broken/gap 计数 + CSV 导出
│
├── experiments/                  # 实验控制器
│   ├── run_experiment.py         # ✅ 统一 CLI 入口（YAML 驱动，支持 Setting1-4 + 消融 + --dry-run）
│   ├── baseline.py                # SelfEvolutionRunner（Setting 1: SE 基线）
│   ├── federated.py               # FederatedRunner（Setting 2-4: 联邦协作）
│   ├── runner.py                  # ✅ 新增：ExperimentRunner 门面类，for_setting(1..4) 映射到 configs/ 已有 4 个 YAML
│   ├── generate_results.py        # 批量生成 Table/Figure
│   └── configs/                   # 8 个 YAML 配置（4 Setting + 3 消融 + 1 family 版）
│
├── scripts/                      # 环境准备 / 真实 API 联调脚本
│   ├── preflight_check.py         # 真实实验前置环境检查
│   ├── test_llm_connection.py     # LLM API 连通性测试
│   ├── validate_configs.py        # YAML 配置合法性校验
│   ├── download_skillflow_dataset.py  # 完整下载 SkillFlow-Task 数据集（~1.6GB）
│   ├── fetch_skillflow_instructions.py # 仅下载 instruction.md + task.toml（轻量版）
│   ├── install_dependencies.sh
│   └── install_harness.sh
│
├── docs/                          # 补充文档
│   ├── paper_mapping.md           # 论文符号 ↔ 代码对照表（详细版）
│   ├── experiment_settings.md     # 4 种 Setting 的详细配置说明
│   ├── real_experiment_setup.md   # 真实 API 实验环境搭建指南
│   ├── reproduction_protocol.md   # 复现协议与已知局限记录（代码来源声明：Prompt 保留官方原文作为实验配置，算法实现独立复现）
│   └── SIMPLIFICATIONS.md         # 全仓库简化点详细清单（按模块组织，供撰写复现报告时逐条核对）
│
├── tests/                          # pytest 测试套件（当前 125 passed, 2 skipped）
│   ├── conftest.py
│   ├── test_benchmark.py          # family 加载/排序/采样器/verifier 回归测试
│   ├── test_executor.py
│   ├── test_agent_executor.py     # ✅ 新增：AgentWorkspaceExecutor + workspace/runner/trajectory 组件回归测试
│   ├── test_family_sampler.py     # ✅ 新增：FamilyAwareSampler（含依赖图）回归测试
│   ├── test_real_llm_pipeline.py  # 需要 --real 标志 + API key 才运行
│   ├── test_skillflow_adapter.py  # skillflow_adapter 各组件单元测试（合成数据）
│   ├── test_real_skillflow_loader.py  # ✅ 新增：真实目录结构端到端集成测试 + synthetic fallback 验证
│   ├── test_llm_generate.py       # ✅ 新增：llm.generate() 统一接口 + 模型路由回归测试
│   ├── test_capability_tracker.py # ✅ 新增：CapabilityEvolutionTracker 计数/CSV 导出回归测试
│   └── test_prompts.py            # ✅ 新增：prompts/ 包 load_prompt() + 三个 Builder 迁移后系统提示词回归测试
│
├── main_trainer.py                # 旧版主入口（run_single / run_compare）
├── _test_imports.py               # 全模块导入健康检查
├── _test_e2e.py                   # 端到端回归测试（mock LLM）
├── _check.py                      # 快速语法/结构自检脚本
├── requirements.txt                # 生产依赖（litellm<1.85.0 等版本锁定）
├── requirements-real.txt           # 真实 API 实验专用依赖（纯 ASCII，避免 GBK 编码问题）
└── .env / .env.example             # API Key 环境变量配置
```

---

## 三、核心模块详解（含函数签名核实结果）

### 3.1 [core/constants.py](core/constants.py) — 全局超参数

实际读取的常量值（已核实，与文件内容完全一致）：

| 常量 | 实际值 | 论文/工程来源 |
|-----|--------|--------------|
| `K_STEP` | `20` | §4.1.2 "retaining at most K_step agentic steps" |
| `K_OBS` | `3_000` | §4.1.2 "observations are truncated to K_obs characters" |
| `TRUNCATION_MARKER` | `"<truncated>"` | §4.1.2 显式截断标记 |
| `MAX_SKILL_MD_LINES` | `500` | 官方 `validate_skill_md.py` 软上限 |
| `MAX_SKILLS_PER_FAMILY` | `4` | 官方 SKILL.md 合并规则硬上限 |
| `ALLOWED_SKILL_SUBDIRS` | `{"scripts", "references", "assets"}` | SKILL.md 目录约定 |
| `DEFAULT_TEMPERATURE` | `0.2` | §5.1 Implementation（patcher 默认温度） |
| `MOONSHOT_TEMPERATURE` | `1.0` | Moonshot/Kimi API 硬性要求 temperature ≥ 1.0 |
| `DEFAULT_MAX_TOKENS` | `8_192` | patcher LLM 调用上限 |
| `MERGER_MAX_TOKENS` | `16_384` | 服务器端 Stage1/Stage2 LLM 调用上限 |
| `MAX_RETRY_ATTEMPTS` | `20` | 限频重试上限（大值防止丢弃 worker 贡献） |
| `RETRY_BASE_SLEEP` / `RETRY_MAX_SLEEP` | `5.0` / `300.0` 秒 | 指数退避参数 |
| `TRANSIENT_MAX_RETRIES` | `5` | 网络抖动类错误的有界重试次数 |
| `MAX_LIBRARY_PROMPT_CHARS` | `20_000` | 防止 prompt 中库快照超长 |
| `MAX_TRAJECTORY_PROMPT_CHARS` | `12_000` | 防止 prompt 中轨迹超长 |

---

### 3.2 [core/datatypes.py](core/datatypes.py) — 全部数据模型（Pydantic v2）

符号速查（docstring 内原文）：

```
ρ_i    → WorkerProfile         （frozen=True，含 computed_field profile_hash）
τ_i    → Trajectory            （原始轨迹，绝不离开客户端）
B_i^t  → TrajectoryBuffer       （单轮轨迹批次，包裹 list[Trajectory]）
L_i^t  → SkillLibrary 管理（client/library.py），快照类型为 LibrarySnapshot
δ_i^t  → WorkerPatch            （上传服务端的唯一产物，见下方"严格 4+1 字段"）
g_i(·) → PatchDistiller.distill()
P^t    → EvolutionPlan = (C^t, M^t, D^t)
Δ_i^t  → MergedPatch
C^t    → CapabilityMatrix
```

**`WorkerProfile`**（`model_config = ConfigDict(frozen=True)`）实际字段：

```python
client_id: str
backbone_model: str          # 如 "qwen3.6-plus" / "glm-5" / "kimi-k2.5"
agent_harness: str            # 如 "claude-code" / "qwen-code" / "kimi-cli"
model_provider: str           # 如 "dashscope" / "moonshot" / "anthropic"
api_base: str
api_key_env: str
system_prompt_name: str = "default"
agent_import_path: str = ""
agent_kwargs: dict[str, Any] = {}
max_context_tokens: int = 32_768
generation_config: dict[str, Any] = {}
metadata: dict[str, Any] = {}
```
计算字段：`profile_hash`（`sha256(f"{backbone_model}:{agent_harness}")` 取前 12 位）。
`is_moonshot` 属性：`"moonshot" in model_provider.lower() or "kimi" in backbone_model.lower()`。

**`WorkerPatch`（δ_i^t）实际字段**（严格对照 Appendix B.2 patch manifest）：

```python
worker_id: str          # 路由标识，对应 manifest 里的 "worker_id"
upserts: dict[str, str] = {}     # U_i^t: {rel_path: full_content}
deletions: list[str] = []        # D_i^t: 待删除路径列表
reward: float                     # R_{i,x}(τ)
summary: str                      # s_i^t：一句话理由
```
`upserts` 字段有 `@field_validator`，对每个 path 调用 `validate_safe_rel_path()`，拒绝绝对路径 / `..` 穿越 / 盘符前缀（如 `C:`）。**不含** `round_idx` / `timestamp` / `task_name` / `profile_hash` — 这是刻意的最小化设计，与 Appendix B.2 实际 JSON 示例完全一致。

**`TrialOutcome`**：`success` 由 `@model_validator(mode="after")` 自动计算，规则是 `reward >= 1.0`（严格二值判定，不存在"部分成功"）。

**`CapabilityState`**（`str, Enum`）：`COVERED / ABSORBING / BROKEN / GAP`（小写字符串值，序列化友好）。

**`PaperMergeAction`**（`str, Enum`，`[PAPER]`）：`ABSORB / REPAIR / REFACTOR`——**没有** REJECT/KEEP/SYNTHESIZE，与论文 §4.2.2 原文动作集合完全一致（不多不少）。原来合并在 `MergeAction` 里的第 4 个"无需更新"快速路径已拆分为独立的 `SkipUpdate`（`[EXTENSION]`，成员 `NO_UPDATE`），以免让类型本身暗示论文有 4 种动作。

**路径安全函数** `validate_safe_rel_path(path: str) -> str | None`：拒绝空字符串、`/` 或 `\` 开头的绝对路径、`..`/`.`/空 part、Windows 盘符（`part[1] == ':'`）。所有 `WorkerPatch.upserts`、`MergedPatch` 涉及路径的字段均复用此函数。

---

### 3.3 [core/exceptions.py](core/exceptions.py) — 异常层次

```
FederatedSkillError
├── PatchDistillationError → PatchValidationError
├── LibraryError → LibraryValidationError
├── LLMCallError → LLMRateLimitError
│              → LLMEmptyResponseError
│              → LLMJSONParseError
├── TrajectoryError
├── ServerPlanningError
├── ServerEvolutionError
├── BenchmarkError → TaskLoadError
│               → VerificationError
└── TaskExecutionError
```
（已核实与代码完全一致，无遗漏分支。）

---

### 3.4 [llm/backbone.py](llm/backbone.py#L233) — LLM 调用器与路由推断

`LLMBackbone` 构造参数：`litellm_model, api_key=None, api_base=None, temperature=DEFAULT_TEMPERATURE, max_tokens=DEFAULT_MAX_TOKENS, retry_config=None, extra_headers=None`。

**关键函数** `resolve_litellm_model(model_name: str, api_base: str | None) -> str`：把模型名解析为 litellm 的 `<provider>/<model>` 格式。**当前实现规则（已修复版本）**：

1. 已含 `/` → 原样返回（用户显式指定）
2. `model_name` 以 `claude` 开头 → `anthropic/{name}`
3. `api_base` 含 `/google` 或 `model_name` 以 `gemini` 开头 → `gemini/{name}`
4. 其余（DashScope / Moonshot / OpenAI 兼容端点，含 Qwen / GLM / Kimi）→ 统一 `openai/{name}`

> ⚠️ **已修复的 bug**：旧版本还会检测 `"/anthropic" in api_base`（即 DashScope 的 Anthropic 兼容网关 URL）并返回 `anthropic/{name}`，导致 litellm 走 Anthropic SDK 内部序列化路径。该路径在处理中文系统提示词时会触发 `UnicodeEncodeError: 'ascii' codec can't encode characters`（DashScope 的 Anthropic-compatible 网关对 litellm 的 Anthropic 客户端不完全兼容）。**修复方式**：`resolve_litellm_model()` 不再依据 `api_base` 里的 `/anthropic` 子串做判断，只有原生 `claude-*` 模型才走 `anthropic/` 前缀；`experiments/configs/setting_se.yaml` 中 qwen worker 的 `api_base` 也同步改为 DashScope 的 **OpenAI 兼容端点** `https://dashscope.aliyuncs.com/compatible-mode/v1`（而非 `/apps/anthropic`）。同时 `llm/prompt_builder.py` 的全部 system/user 提示词已从中文改写为英文，作为双重防护。

`_call_with_retry()`：双级重试 —— RATE_LIMIT（指数退避，`RetryConfig.max_rate_retries` 默认较大，防止丢弃某个 worker 整轮贡献）+ TRANSIENT（线性退避，`TRANSIENT_MAX_RETRIES=5` 有界）。空响应（200 但内容 `< 8` 字符）会被 `_extract_response_text` 判定为 `LLMEmptyResponseError` 并纳入 TRANSIENT 重试路径。

`call_json()` 内部调用 `call()` 后经 `llm/json_parser.safe_parse_json()` 三级降级解析：直接 `json.loads` → 提取 ` ```json ``` ` 代码块 → 提取裸 `{...}`。

---

### 3.5 [llm/providers.py](llm/providers.py) — Provider 注册表

`PROVIDERS` 字典目前含 5 个 provider：`dashscope_anthropic` / `dashscope_openai` / `moonshot_anthropic` / `moonshot_openai` / `anthropic_native`，每个含 `api_base` / `api_key_env` / `litellm_prefix` / `min_temperature`。

`MODEL_TO_PROVIDER` 映射（**已修复**）：
```python
MODEL_QWEN: "dashscope_openai"   # 原为 dashscope_anthropic，已改为 openai-compatible 端点
MODEL_GLM:  "dashscope_openai"   # 同上
MODEL_KIMI: "moonshot_anthropic" # Kimi 仍走 Anthropic 兼容端点（未复现上述 ASCII bug，保留）
```
`resolve_provider_for_model()` 按精确匹配 → 关键词匹配（`kimi/moonshot`→moonshot_anthropic，`qwen/glm`→dashscope_anthropic，`claude`→anthropic_native）→ 默认 `dashscope_anthropic` 的优先级链推断。**注意**：此函数的关键词匹配分支目前仍然把 `qwen/glm` 兜底到 `dashscope_anthropic`（未随 `MODEL_TO_PROVIDER` 一起修）——若 `backbone_model` 不在 `MODEL_TO_PROVIDER` 精确匹配表中（例如自定义模型名），仍可能触发前述 ASCII 编码问题，需要连同 `resolve_litellm_model()` 一起看（后者已按 `api_base` 而非 provider 名判断，实际上是双重防护，只要 YAML 里 `api_base` 配置正确指向 OpenAI 兼容端点即安全）。

---

### 3.6 [llm/router.py](llm/router.py) — BackboneRouter

`BackboneRouter.from_profiles(profiles, temperature=DEFAULT_TEMPERATURE, max_tokens=DEFAULT_MAX_TOKENS, retry_config=None, fallback_backbone=None)`：批量按 `WorkerProfile.client_id` 建路由表。`get(worker_id)` 查不到时使用 `fallback_backbone`（通常是服务器 backbone），仍查不到抛 `KeyError`。

论文强约束（§4.1.2 *"the patcher... shares the same backbone LLM as the execution LLM"*）由此路由表在结构上保证——`TaskExecutor` 和 `PatchDistiller` 对同一个 `worker_id` 永远从同一个 `BackboneRouter` 取同一个 `LLMBackbone` 实例。

---

### 3.7 [client/trajectory.py](client/trajectory.py) — TrajectoryCompressor

`TrajectoryCompressor(k_step=K_STEP, k_obs=K_OBS)`，构造时校验 `k_step >= 1` 且 `k_obs >= 1`（否则 `ValueError`）。

`compress(trajectory) -> CompactedTrajectory` 主步骤：
1. `_select_steps()`：保留首步 + 最近 `k_step - 1` 步（严格对应论文 "the initial step plus the K_step − 1 most recent steps"）
2. `_clean_step()`：`observation` 超过 `k_obs` 字符则截断并追加 `<truncated>`；`tokens_used` 清零
3. `_strip_tool_call_args()`：只保留 `function.name`，去除具体参数（隐私保护）
4. `_extract_exception_types()`：从 `trajectory.exception_info` 提取异常类型列表

---

### 3.8 [client/distiller.py](client/distiller.py#L120) — PatchDistiller 七步流水线

`distill(trajectory, library, profile=None) -> WorkerPatch`。**注意**：若不传 `profile` 会直接 `raise ValueError`（代码里显式禁止无 profile 调用，docstring 说"profile: None → 从 router 中查 trajectory.worker_id"，但实际实现里没有这个降级路径——这是文档与实现不一致之处，实际使用时必须显式传入 `profile`）。

七步：
1. `_step1_compress()` → `TrajectoryCompressor.compress()`
2. `_step2_snapshot()` → `library.snapshot(round_idx)`
3. `_step3_outcome()` → 从 `trajectory.reward`（`None` 时按 `0.0` 处理）等字段组装 `TrialOutcome`
4. `_step4_prompt()` → `DistillerPromptBuilder.build()` 组装 `(system_prompt, user_prompt)`
5. `_step5_call_llm()` → `router.get(worker_id).call_json(user_prompt, system_prompt)`；**捕获 `LLMCallError` 后降级返回空 patch dict**（`{"upsert_files": {}, "delete_paths": [], "summary": "[LLM 调用失败: ...]"}`），不会让单个 worker 的失败中断整轮实验
6. `_step6_validate()` → 统一字段名（兼容 `upsert_files`/`upserts`）、路径安全检查、隐私审计 `_audit_privacy()`
7. `_step7_build_patch()` → 组装最终 `WorkerPatch`

**隐私审计** `_audit_privacy()`：对 `upserts` 内容做正则扫描，检测任务 ID / IP 地址 / 疑似凭证 / `confidential` 类关键词，命中时记录警告（当前实现为告警而非硬性拒绝——如需严格阻断需要调用方自行处理告警）。

---

### 3.9 [client/library.py](client/library.py) — SkillLibrary

`SkillLibrary(root: Path, worker_id: str)`，构造时 `root.mkdir(parents=True, exist_ok=True)`。

- `snapshot(round_idx=0) -> LibrarySnapshot`：`rglob("*")` 递归读取所有文件文本内容，遇 `UnicodeDecodeError`/`OSError`（二进制文件）静默跳过；路径统一转为 POSIX 正斜杠。
- `digest() -> list[LibraryDigest]`：只解析每个技能目录下 `SKILL.md` 的 YAML frontmatter（`name`/`description`/`tags`），**不读取正文和其他文件**——这是 Stage1 信息最小化原则的代码级保证。
- `apply_patch()` / `rollback()` / `validate()`：见文件内注释，逻辑为先执行 `deletions` 后写 `upserts`，`rollback()` 支持恢复到任意历史快照。

---

### 3.10 [benchmark/task.py](benchmark/task.py) — Task 与 VerificationSpec

`VerificationSpec.type` 取值（**已扩展为 5 种，含新增 `"none"`**）：

```python
Literal["python_test", "function_test", "output_match", "skillflow_script", "none"] = "python_test"
```

`@model_validator(mode="after") _check_consistency()` 规则：
- `type == "none"` → 直接放行（**新增**，供真实 SkillFlow 任务使用，验证逻辑完全交给外部 subprocess/Docker，不在 Pydantic 层强制要求任何字段）
- `python_test` 需要 `test_code`
- `function_test` 需要 `entry_point`
- `output_match` 需要 `expected_output`
- `skillflow_script` 需要 `test_script`

`Task` 模型字段（**`category` 和 `verification` 均已改为带默认值**）：
```python
task_id: str                       # 必填
category: str = ""                 # 曾经必填，现已改为默认空字符串
family_id: str = ""                # 留空时应等于 category（兼容旧任务文件）
description: str                    # 必填，传给 agent 生成代码
required_skills: list[str] = []
verification: VerificationSpec = VerificationSpec(type="none")  # 曾经必填，现已默认 none
difficulty: int = 1  # 1..20
```

> ⚠️ **已修复的 bug**：`category` 和 `verification` 原本都是 Pydantic 必填字段（无默认值）。真实 SkillFlow 数据集下载脚本 `scripts/fetch_skillflow_instructions.py` 生成的 family JSON 里没有这两个字段（只有 `task_id`/`description`/`difficulty` 等），导致加载 `benchmark/families/*.json` 时抛出 `ValidationError: category Field required` / `verification Field required`。修复后二者均有默认值，且 `VerificationSpec` 新增 `"none"` 类型跳过一致性校验，允许"验证委托给外部 Docker/脚本，Pydantic 层不做任何断言"的场景。

---

### 3.11 [benchmark/family.py](benchmark/family.py) — TaskFamily

`TaskFamily(family_id, description, tasks, skill_name="")`：构造时按 `difficulty` 升序排序 `tasks`，空列表会 `raise ValueError`。

`get_task_by_difficulty(level)`：精确匹配 `difficulty == level`；找不到时钳制到 `≤ level` 的最后一个（越界不抛异常，退化为"维持最高难度"）。

`load_all_families(directory=_DEFAULT_FAMILIES_DIR)`：加载目录下全部 `*.json`，返回 `{family_id: TaskFamily}`。**当前 `benchmark/families/` 目录实际含 25 个 family JSON**：5 个手写家族（`data_cleaning` / `financial_analysis` / `document_processing` / `data_transformation` / `report_generation`，均为 `function_test` 类型，纯本地可执行验证）+ 20 个通过 `scripts/fetch_skillflow_instructions.py` 从 HuggingFace `zhang-ziao/SkillFlow-Task` 数据集下载的真实 SkillFlow family（如 `Compensation-Scenario-Modeling` / `SEC-13F-Financial-Analysis` 等，`verification.type` 目前为 `"none"`，因为真实验证逻辑依赖数据集自带的 Docker 测试脚本，尚未接入 `executor/skillflow_executor.py` 的完整 subprocess 验证链路——这是当前的已知局限，见 §七）。

---

### 3.12 [benchmark/curriculum.py](benchmark/curriculum.py) — FamilyCurriculumSampler

`FamilyCurriculumSampler(families, worker_family_map=None, seed=None)` 继承 `TaskSampler`。`family_for(worker_id)`：未显式绑定时按"已绑定 worker 数量取模 family 数量"做**确定性 round-robin**分配（刻意不用 `hash()`，避免 Python 的哈希随机化导致不可复现）。`sample(worker_id, round_idx)` 返回该 worker 绑定 family 内难度 `round_idx+1`（或钳制后）的任务。

与 `RandomSampler`/`HeterogeneousSampler` 接口完全兼容（都实现 `sample_batch(worker_ids, round_idx) -> dict[str, Task]`），可在 YAML 里通过 `sampler: family_curriculum` 直接切换，`experiments/run_experiment.py`/`baseline.py`/`federated.py` 零改动。

---

### 3.13 [benchmark/verifier.py](benchmark/verifier.py#L101) — 验证器

| 类 | 对应 `VerificationSpec.type` | 判定逻辑 |
|----|------------------------------|---------|
| `PythonSandboxVerifier` | `python_test` | 生成代码 + `test_code` 拼接为完整脚本，subprocess 执行，`exit(0)` → reward=1.0 |
| `FunctionTestVerifier` | `function_test` | 注入 `entry_point` 函数，逐 `test_cases` 执行并 `==` 比较，全部通过才 reward=1.0（二值） |
| `OutputMatchVerifier` | `output_match` | 检查 stdout 是否包含 `expected_output` 子串 |
| `SkillFlowScriptVerifier` | `skillflow_script` | 在任务工作区目录内 subprocess 执行 `test_script`，退出码 0 视为通过；由 `executor/skillflow_executor.py` 调用，不走通用 `verify()` 接口 |

`VerificationResult.verifier_score`：软得分 = 通过用例数 / 总用例数（无子用例时退化为 `reward`）。

---

### 3.14 [server/capability.py](server/capability.py) — CapabilityTracker

`init_from_patches(patches: dict[str, WorkerPatch], round_idx)`：为本轮新出现的 `(workflow, worker)` 组合初始化为 `GAP`。⚠️ **实现细节**：目前用 `worker_id` 本身作为 workflow 行的 key（代码注释里写明"fallback：用 worker_id 作行 key，实际 workflow 由 Stage1 更新"），真正的 workflow 名称要靠 `update_from_plan_dict()` 从 Stage1 LLM 返回的矩阵里覆盖更新。这是一个**弱假设**——如果 Stage1 LLM 调用失败降级（`_fallback_plan()`），矩阵会长期停留在以 `worker_id` 为行名的占位状态，而非真实 workflow 名称，可能影响 `is_workflow_retired()` 等基于 workflow 名称聚合的统计准确性。

`is_workflow_retired()`：仅当"所有 worker 在该 workflow 行均为 COVERED"时才判定退役，严格对应论文 *"retired only when every client's cell becomes covered"*。

---

### 3.15 [server/memory.py](server/memory.py) — EvolutionMemoryStore

`update_high_level(new_content, round_idx)`：**完整替换**（非追加）高层记忆内容，由 Stage1 LLM 输出驱动。`update_low_level()`：更新单个 worker 的私有记忆，由 Stage2 LLM 输出驱动。两者初始内容均为占位符"暂无记录"，`last_updated_round=-1`。

---

### 3.16 [server/planner.py](server/planner.py#L66)（Stage1）/ [server/merge.py](server/merge.py#L59)（Stage2）

`EvolutionPlanner.plan(round_idx, family_name, patches, library_digests, capability_tracker, memory_store, worker_profiles) -> EvolutionPlan`：单次服务器 backbone LLM 调用，失败时 `_fallback_plan()` 降级（矩阵/记忆不变、无新指令，不中断整轮实验）。**输入严格限定为 `library_digests`（描述级摘要），不传完整库内容**，对应论文信息最小化要求。

`EvolutionExecutor.execute_for_worker(target_worker_id, target_profile, directive, current_snapshot, peer_patches, peer_profiles, memory_store, round_idx) -> (MergedPatch, DecisionLog)`：
- `directive is None` → 直接返回空 patch（不调用 LLM，维持现状）
- `directive.action == SkipUpdate.NO_UPDATE` → 同样走空 patch 快速路径，不调用 LLM（节省成本，`[EXTENSION]`）
- 其余动作（`PaperMergeAction.ABSORB`/`REPAIR`/`REFACTOR`，`[PAPER]`）才真正调用服务器 backbone

**独立性约束**：每个 worker 的 Stage2 调用相互独立（顺序执行，非并行，但语义上是"non-all-reduce"，即彼此互不干扰），对应论文 *"Stage 2 runs independently per client"*。

---

### 3.17 [server/evolution.py](server/evolution.py) — FederatedServer

`FederatedServer.create(server_backbone, family_name, worker_profiles)`：一键工厂，内部创建 `CapabilityTracker` + `EvolutionMemoryStore` + `EvolutionPlanner` + `EvolutionExecutor`。

`run_round(round_idx, patches: dict[str, WorkerPatch], library_snapshots: dict[str, LibrarySnapshot]) -> dict[str, MergedPatch]`：完整 round（Stage1 规划 → 逐 worker Stage2 执行），返回值**必须是 dict**（历史上 `experiments/federated.py` 曾传入 `list[WorkerPatch]` 导致 `CapabilityTracker.init_from_patches()` 因 `list` 无 `.items()` 而崩溃，现已修复为 `dict[str, WorkerPatch]`，见 §六 修正 9）。

---

### 3.18 [evaluation/metrics.py](evaluation/metrics.py#L59) — 论文指标

`FederatedMetrics` 全部为无状态静态方法：

| 方法 | 公式 | 论文位置 | 备注 |
|------|------|---------|------|
| `success_rate(rewards)` | SR = N_success / N_total，`reward>=1.0` 记为成功 | Table 1/2 | — |
| `compression_ratio(patch_tokens, trajectory_tokens)` | CR = max(0, 1 - patch/traj) | Appendix C | 有 `max(0, ...)` 钳制，防止负值 |
| `privacy_gain(trajectory_tokens, patch_tokens)` | 数值上与 `compression_ratio()` **完全相同** | Appendix E（代理指标） | ⚠️ **不是**真正的 SELR（Table 8 用敏感实体计数，需 NER/正则扫描 patch 内容），代码 docstring 已明确注明此局限 |
| `skill_growth()` | L_i^(t+1) - L_i^t 技能数之差 | Figure 3 | — |
| `heterogeneity_gain()` | SR_fed - SR_solo | §5.3 / Table 2 | — |
| `cost_per_solved_task()` | cost / N_success | Figure 4 | — |
| `family_success_curve()` | 按 family+round 分组统计 SR | 非论文原始指标，附加分析用 | 基于已存的 `RoundEvalResult.snapshots`，不改动 `evaluation/evaluator.py` 主体 |
| `compute_all()` | 一次性返回以上全部 | 实验报告汇总 | — |

---

### 3.19 [experiments/run_experiment.py](experiments/run_experiment.py) — 统一实验入口

**这是目前推荐的唯一实验入口**（`main_trainer.py` 为旧版，仍保留但功能重叠）。

```bash
python experiments/run_experiment.py --config experiments/configs/setting_se.yaml
python experiments/run_experiment.py --config experiments/configs/setting_se.yaml --rounds 8 --output results/setting1_se
python experiments/run_experiment.py --config experiments/configs/setting_se.yaml --dry-run
```

内部关键函数：
- `_load_yaml(config_path)`：加载 YAML，非 dict 类型直接 `raise ValueError`
- `_build_worker_profile(worker_cfg)`：从 YAML worker 节点构造 `WorkerProfile`
- `_build_backbone(cfg, role="worker")`：**API key 未设置时提前 `raise EnvironmentError`**（早于实验开始时报错，而非跑到一半才失败）
- `_build_sampler(cfg, families)`：支持 `"random"` / `"curriculum"` / `"family_curriculum"` 三种取值

---

### 3.20 [experiments/baseline.py](experiments/baseline.py#L78) — SelfEvolutionRunner（Setting 1）

核心循环（对应论文 §5.1 SE 基线，无服务器）：
```python
for round_idx in range(rounds):
    for client in clients:
        task = sampler.sample(client.worker_id, round_idx)
        trajectory = executor.run(task, ...)          # §4.1.1
        patch = client.distill_patch(trajectory)        # §4.1.2 Eq.(2)
        client.apply_update(patch)                       # 直接 apply 自己的 patch，无 Stage1/2
```
支持 `disable_patch_distillation=True` 消融开关（对应 `ablation_a3_full_trajectory.yaml`）。

---

### 3.21 [experiments/federated.py](experiments/federated.py#L97) — FederatedRunner（Setting 2-4）

完整 Algorithm 1：Client Phase（execute + distill）→ Server Phase（Stage1 + Stage2）→ Apply Phase（`client.apply_update(merged[worker_id])`）。三种 Setting 的区别仅在于 `WorkerProfile` 配置的异构程度（backbone 相同/不同、harness 相同/不同），Runner 代码本身不区分 Setting。

---

### 3.22 `executor/` — 真实 Agent Workspace 执行模式（Phase12 新增）

对应论文 §4.1.1 "Model → Agent Framework → Skill Retrieval → Tool Calling → Environment → Test" 的完整 harness 架构，在 `mock_executor.py`/`python_executor.py`/`skillflow_executor.py` 三种早期执行器之外，新增一套可组合的真实 workspace 组件：

- `base.py::BaseExecutor` — 统一执行器抽象接口，四种执行器（Mock/Python/SkillFlow/AgentWorkspace）均实现同一契约。
- `environment.py::WorkspaceManager` — 为每个任务创建独立的真实临时文件系统工作区（隔离，避免任务间相互污染）。
- `runner.py::CommandRunner` / `CommandResult` — 在工作区内执行 shell/subprocess 命令并采集 stdout/stderr/exit_code。
- `trajectory.py::TrajectoryCollector` — 记录 `actions`/`tool_calls`/`generated_files`/`exceptions`/`verification`/`token_usage` 等完整轨迹字段（`core.datatypes.Trajectory` 的扩展字段，向后兼容旧版 Trajectory）。
- `agent_executor.py::AgentWorkspaceExecutor` — 组合以上四者的真实 agent 执行器，替代早期"生成代码字符串直接跑"的简化模式。

对应测试：`tests/test_agent_executor.py`。

### 3.23 `benchmark/skillflow_adapter/` — 真实 SkillFlow benchmark 接入（Phase13）

- `parser.py::parse_task_dir()` — 解析官方目录约定（`task.toml` + `instruction.md` + `environment/` + `tests/`），`solution/` 参考解目录被安全忽略、不泄漏进 `Task.files`。
- `converter.py::to_task()` — `RawSkillFlowTask` → 现有 `Task`/`VerificationSpec`（`verification.type="skillflow_script"`），不新增/不修改 Task 字段。
- `loader.py::load_skillflow_family()` / `load_skillflow_benchmark()` — 目录 → `TaskFamily`，可直接喂给既有 `FamilyCurriculumSampler`。
- `downloader.py` / `download.py` — `download_skillflow_dataset()`（HuggingFace `snapshot_download`，支持断点续传）+ `is_dataset_present()`；`download.py` 是纯转发别名（满足文件命名要求，不重复实现）。
- **synthetic fallback 保留**：真实数据集不存在/未下载时，`benchmark.family.load_all_families()`（25 个自建 family JSON）完全独立可用，不受本适配器影响。

对应测试：`tests/test_skillflow_adapter.py`（单元级）+ `tests/test_real_skillflow_loader.py`（端到端集成 + fallback 验证）。

### 3.24 `llm/generate.py` — 统一 LLM 调用接口（Phase13）

```python
def generate(model: str, prompt: str, json_mode: bool = False, **kwargs) -> GenerateResult
```

薄封装（不修改 `backbone.py`/`providers.py`/`router.py` 任何已测试逻辑）：按 `model` 名称经 `llm.providers.resolve_provider_for_model()` 自动路由到 Qwen/GLM/Kimi/Claude 对应 provider，构造 `LLMBackbone` 并调用其 `call()`/`call_json()`。返回 `GenerateResult(text, input_tokens, output_tokens, cost, latency, json_data)`——`input_tokens`/`output_tokens`/`cost` 取自 `BackboneCallResult.prompt_tokens`/`completion_tokens`/`cost_usd`，`latency` 由本模块用 `time.monotonic()` 在调用前后实测。

对应测试：`tests/test_llm_generate.py`（文本/JSON 两种模式 + 4 种模型路由 + Kimi 强制最低温度）。

### 3.25 `experiments/runner.py::ExperimentRunner` — 统一实验运行器门面（Phase13）

```python
ExperimentRunner.for_setting(1).run(dry_run=True)   # Setting1: Self-Evolve
ExperimentRunner.for_setting(4).run()                # Setting4: Full Heterogeneity
```

`SETTING_CONFIG_MAP` 把 Setting 1-4 直接映射到 `experiments/configs/` 下已有的 4 个 YAML（`setting_se.yaml`/`setting_homo_fed.yaml`/`setting_hetero_backbone.yaml`/`setting_full_hetero.yaml`），不新建重复配置文件；`run()` 方法零逻辑重复地委托给已测试的 `experiments.run_experiment.run_experiment()`。

### 3.26 `evaluation/capability_tracker.py::CapabilityEvolutionTracker` — Capability Matrix 跨轮演化（Phase13）

与 `server/capability.py::CapabilityTracker`（单轮"当前状态"追踪器，服务器决策用）分工不同：本模块是**只读历史记录器**，每轮结束后摄入一份 `CapabilityMatrix` 快照（`CapabilityTracker.to_capability_matrix()` 的输出），累计 covered/absorbing/broken/gap 四态计数，提供：
- `record(matrix) -> RoundCapabilitySummary`：单轮四态计数 + `coverage_ratio`
- `coverage_trend() -> list[float]`：跨轮覆盖率序列（论文能力覆盖曲线的数据来源）
- `to_csv(path)` / `per_worker_to_csv(path)`：导出 capability evolution CSV（全局 / 按 worker 拆分两个粒度）

对应测试：`tests/test_capability_tracker.py`。

### 3.27 `prompts/` — Prompt 配置包（Official Prompt Retention 重构，见 §六修正 18）

```python
from prompts import load_prompt
template = load_prompt("stage1_prompt.txt")   # 读取原始文本，未 format
system = template.format(family_name="OCR-Data-Extraction", round_idx=3)
```

本包把三个系统提示词从 `server/prompt_builder.py`/`llm/prompt_builder.py` 的内联大段字符串中拆出，按**来源**明确分类：

| 文件 | 来源 | 说明 |
|------|------|------|
| `stage1_prompt.txt` | 保留官方 `task_update_skill/SKILL.md` 原文 | 逐句比对确认此前"clean-room 还原"的表述不准确，实际是官方文本的高度复用；现按新政策明确标注为**保留的实验配置**，不再谎称独立重写 |
| `stage2_prompt.txt` | 保留官方 `merge_skill/SKILL.md` 原文 | 同上，含 `vs_peers: match_peers \| keep_target_with_evidence \| target_only_skill` 等原文三态枚举 |
| `patch_prompt.txt` | 本项目自研（英文） | 官方 patcher 提示词封装在不可见的外部库 `libs.skill_evolution.patcher.SkillPatchEvolver` 内，本项目从未接触过其原文，因此不存在"保留"一说，只能独立设计；译为英文是为了和修正 14 的"翻译+路由"双重防护策略保持一致 |

**边界原则**（用户明确裁决）：Prompt 文本属于实验配置（experimental configuration / agent instruction），可以保留官方原文；但调用这些提示词的算法逻辑——`server/planner.py`（Stage1 规划流程）、`server/merge.py`（Stage2 合并执行流程）、`client/distiller.py`（Patch 蒸馏流水线）——必须独立实现，不能复制官方 `.py` 源码。三者核查后确认本就只做“输入 → 调用 LLM → 解析输出”，无需重写。

对应测试：`tests/test_prompts.py`（6 tests：3 个占位符完整性测试 + 3 个 Builder 端到端 format 测试）。

> Phase12/13 的完整"论文符号 ↔ 代码"映射与"剩余差距"说明见 [`docs/paper_mapping.md`](docs/paper_mapping.md) 的对应章节。

---

## 四、核心公式 × 代码对照（已核实签名）

| 论文公式 | 代码入口 | 文件 | 核实结果 |
|---------|---------|------|---------|
| J_i(L) = E[R_{i,x}(τ)] Eq.(1) | `FederatedMetrics.success_rate(rewards: list[float]) -> float` | [evaluation/metrics.py](evaluation/metrics.py#L59) | ✅ 一致 |
| δ_i^t = g_i(L_i^t, B_i^t, ρ_i) Eq.(2) | `PatchDistiller.distill(trajectory, library, profile) -> WorkerPatch` | [client/distiller.py](client/distiller.py#L120) | ✅ 一致；⚠️ `profile=None` 时直接抛异常，docstring 描述的降级路径未实现 |
| δ_i^t = (U_i^t, D_i^t, R_{i,x}(τ), s_i^t) Eq.(4) | `WorkerPatch(worker_id, upserts, deletions, reward, summary)` | [core/datatypes.py](core/datatypes.py) | ✅ 一致，字段数与 Appendix B.2 manifest 完全对应 |
| L_i^(t+1) = Apply(L_i^t, Δ_i^t) | `SkillLibrary.apply_patch(patch)` | [client/library.py](client/library.py) | ✅ 一致 |
| P^t = (C^t, M^t, D^t) | `EvolutionPlanner.plan(...) -> EvolutionPlan` | [server/planner.py](server/planner.py#L66) | ✅ 一致 |
| J̄^t = Σ q_i · J_i(L_i^t) Eq.(3) | `BenchmarkEvaluator`（`benchmark/evaluator.py`） | [benchmark/evaluator.py](benchmark/evaluator.py) | ⚠️ 未在本次核查中逐行验证权重 q_i 的实现细节，建议使用前二次确认 |
| SELR(C) Eq.(5) | `FederatedMetrics.privacy_gain()`（代理指标） | [evaluation/metrics.py](evaluation/metrics.py#L99) | ❌ **非严格实现**，见 3.18 与 §七局限说明 |

---

## 五、与官方代码的设计差异

| 方面 | 官方代码 | 本复现 |
|------|---------|--------|
| LLM 调用 | `make_llm_call()` 函数闭包 | `LLMBackbone` 类（状态显式，含 `BackboneStats` 累计统计） |
| 重试策略 | 嵌在 LLM 调用内 | 独立 `retry.py` 模块（`ErrorBucket` 四态枚举 + 独立配置对象 `RetryConfig`） |
| backbone 路由 | 硬编码 per-worker | `BackboneRouter` 路由表 + `resolve_litellm_model()` 显式推断规则 |
| Agent Harness | Harbor Docker 容器 | 轻量 [client/agent_runtime/](client/agent_runtime/)（真实 Planner-Action-Observation 循环）+ [executor/skillflow_executor.py](executor/skillflow_executor.py#L58)（subprocess 隔离，替代容器） |
| 任务 benchmark | SkillFlow 官方 20 家族（Docker 验证） | 25 个 family JSON：5 个手写 `function_test` 家族（纯本地可执行验证）+ [benchmark/families/](benchmark/families/) 20 个真实 SkillFlow family（验证机制已扩展至 `function_test`/`skillflow_script`/`docker`，见 [benchmark/verifier.py](benchmark/verifier.py#L101) + [executor/harbor_adapter.py](executor/harbor_adapter.py)；但这 20 个 family 自身尚未填充官方 `docker_image`/`test_script`，机制就绪、数据未就绪） |
| Stage2 触发 | SkillFlow 内置 evolver（不透明） | [server/merge.py](server/merge.py#L59) `EvolutionExecutor`（独立 per-client，逻辑透明可测） |

---

## 六、架构校对修正说明（累计修正记录，按发现顺序编号）

| # | 修正点 | 修改前 | 修改后 | 依据 |
|---|--------|--------|--------|------|
| 1 | WorkerPatch 字段 | 含 `metadata`（worker_id/timestamp/task_name） | 仅 `worker_id` + 严格 4 字段：upserts/deletions/reward/summary | Eq.(4) + Appendix B.2 真实 manifest 示例 |
| 2 | MergeAction | ABSORB/REPAIR/REFACTOR/REJECT/KEEP/SYNTHESIZE | ABSORB/REPAIR/REFACTOR/**DROP**（后续进一步拆分为 `PaperMergeAction`{ABSORB/REPAIR/REFACTOR}+`SkipUpdate`{NO_UPDATE}，见下方"分类标签"一节） | §4.2.2 |
| 3 | Trajectory 符号 | B_i^t / τ 混用 | τ_i（单次）+ TrajectoryBuffer（B_i^t 批次） | §4.1.1 |
| 4 | CapabilityState | 已正确（lowercase 枚举值） | — | §4.2.1 |
| 5 | EvolutionPlan 字段 | 已含 `low_level_memories` | — | P^t=(C^t,M^t,D^t) |
| 6 | patches 接口 | `list[WorkerPatch]` | `dict[str, WorkerPatch]`（key=worker_id） | 路由由 dict key 完成 |
| 7 | Agent Harness | 简单 subprocess 执行 | `client/agent_runtime/`（完整 Planner-Action-Obs 循环） | §4.1.1 agentic |
| 8 | Benchmark 规模（旧版） | 8 个任务 | 20 个任务（5 类别，难度 1-3） | §5.1 |
| 9 | `experiments/federated.py` 类型不匹配 | `patches: list[WorkerPatch]` 传给期望 dict 的 `server.run_round()`（运行时崩溃：`list` 无 `.items()`） | 改为 `dict[str, WorkerPatch]`，`patches[wid] = patch` 填充 | 与修正 6 一致 |
| 10 | `PatchMetadata` 死代码 | 类定义残留在 `core/datatypes.py` 且被多处导入但无字段实际使用 | 完全移除该类及所有导入/导出引用 | Eq.(4) 只有 4 字段，不应存在游离 metadata 类型 |
| 11 | WorkerPatch 缺 `worker_id` | 严格 4 字段，无 worker_id | 加回单个 `worker_id: str` 字段，其余不加 | Appendix B.2 真实 manifest 示例 |
| 12 | Benchmark 任务结构 | 20 个互相独立的一次性编程题，随机采样 | 新增 `benchmark/families/*.json`（同技能递增难度序列）+ `TaskFamily` + `FamilyCurriculumSampler` | §5.1 "20 task families，每个 family 内部同一技能递增难度序列" |
| 13 | **`Task.category` / `Task.verification` 无默认值** | 两字段均为 Pydantic 必填，导致加载真实下载的 SkillFlow family JSON（无 category/verification 字段）时抛 `ValidationError` | `category: str = ""`；`verification: VerificationSpec = VerificationSpec(type="none")`；`VerificationSpec.type` 新增 `"none"` 枚举值并在 `_check_consistency()` 中直接放行 | 真实数据下载脚本产出的 JSON 结构与手写 family 不同，需要兼容 |
| 14 | **`resolve_litellm_model()` 对 DashScope Anthropic 兼容端点误判** | `"/anthropic" in api_base` 时返回 `anthropic/{model}`，导致 qwen3.6-plus 走 litellm 的 Anthropic SDK 路径，遇中文 system prompt 抛 `UnicodeEncodeError: 'ascii' codec can't encode characters` | 只有 `model_name.startswith("claude")` 才返回 `anthropic/` 前缀；DashScope/Moonshot/OpenAI 兼容端点一律 `openai/` 前缀；`setting_se.yaml` 的 qwen worker `api_base` 改为 `.../compatible-mode/v1`（OpenAI 兼容）而非 `.../apps/anthropic`；`llm/providers.py` 的 `MODEL_TO_PROVIDER[MODEL_QWEN/MODEL_GLM]` 同步改为 `dashscope_openai`；`llm/prompt_builder.py` 全部提示词改写为英文作为双重防护 | 实测跑 Setting 1 时持续抛出该 `InternalServerError`，5 次重试后仍失败，属于阻塞性 bug |
| 15 | Benchmark 家族与执行器新增（Phase12） | 旧版 `client/executor.py` 只能"生成代码字符串直接跑"，与 §4.1.1 的 Agent Harness 架构（Model→Agent Framework→Tool Calling→Environment→Test）差距较大 | 新增 `executor/base.py`（`BaseExecutor` 统一接口）+ `executor/environment.py`（真实临时工作区隔离）+ `executor/runner.py`（subprocess 命令执行）+ `executor/trajectory.py`（完整轨迹采集）+ `executor/agent_executor.py`（组合以上四者的 `AgentWorkspaceExecutor`），`core.datatypes.Trajectory` 附加 `actions`/`tool_calls`/`generated_files`/`exceptions` 等字段（向后兼容旧版） | §4.1.1 真实 agentic 执行循环 |
| 16 | 真实实验阶段补缺（Phase13） | 5 项任务要求接入真实 SkillFlow benchmark、统一 LLM 调用接口、`ExperimentRunner`、Capability Matrix 演化 CSV；审计发现绝大部分基础设施已在此前会话中完整实现 | 精准补缺 4 个新文件：`benchmark/skillflow_adapter/download.py`（文件名别名）、`llm/generate.py`（统一 `generate(model, prompt, json_mode)`）、`experiments/runner.py::ExperimentRunner`（门面类）、`evaluation/capability_tracker.py::CapabilityEvolutionTracker`（跨轮演化 CSV），全部为纯新增/薄封装，未修改任何已测试模块 | 见 §3.22–3.26 及 `docs/paper_mapping.md` Phase13 章节 |
| 17 | Gap1/Gap2/Gap3：真实验证通道 + 严格 SELR + 一键报表 | (a) `verification.type="none"` 无注册项时被通用 `except` 捕获包装成模糊的"验证器内部异常"；(b) 没有 `"docker"` 验证类型/Harbor 执行器；(c) `evaluation/metrics.py` 与 `evaluation/privacy.py` 存在两套不一致的 SELR 近似实现，且都没有 canary 注入审计；(d) 没有一键跑通 Setting1-4 并出表的脚本 | (a) 新增 `NoVerificationVerifier`（显式注册为 `"none"`，reward=0.0 + `exception_info={"type":"NoVerificationDefined"}`，不再被误当成运行时异常）；(b) `benchmark/task.py::VerificationSpec` 新增 `"docker"` 类型 + `docker_image` 字段，新增 `executor/harbor_adapter.py::HarborExecutor`（真实 `docker build`/`docker run`，无 Docker 时明确返回 `DockerUnavailable`，不伪造成功）+ `benchmark/verifier.py::DockerScriptVerifier`，`executor/skillflow_executor.py` 按 `verification.type` 自动路由；(c) `evaluation/privacy.py` 新增 `scan_sensitive_entities()`（详细实体清单，新增 person_name/email/bank_account 类别）、`compute_SELR(source, target)`（真正的 source→target 泄露比对，与旧版"单文本实体密度"区分开）、`inject_canaries()` + `canary_injection_test()`（Appendix E 金丝雀注入审计）、`privacy_summaries_to_csv()` / `canary_reports_to_csv()`；(d) 新增 `experiments/run_paper_table.py`，复用现有 `ExperimentRunner.for_setting()` + `ResultsExporter`，一键跑通 Setting1-4 并导出 success_rate/communication/privacy/skill_growth 四张 CSV | 本轮用户提出的"目前真正缺失的地方" Gap1–Gap3 及任务排序；新增 22 个单元测试（`tests/test_harbor_adapter.py` 12 个 + `tests/test_privacy_selr.py` 10 个）均通过，全量回归 73 passed / 2 skipped |
| 18 | **Official Prompt Retention 重构**：Stage1/Stage2 提示词曾被错误声称"clean-room 还原"，实际逐句比对官方 `task_update_skill/SKILL.md`/`merge_skill/SKILL.md` 后确认近乎逐字复制 | 全仓文档（`server/prompt_builder.py` 模块 docstring、`docs/SIMPLIFICATIONS.md`、`docs/reproduction_protocol.md`、`docs/paper_mapping.md` Appendix B 行）均声称提示词是"clean-room 还原"/独立重写，与实际内容不符 | 用户裁决新政策：**Prompt 属于实验配置，允许保留官方原文；只有算法实现不能复制官方源码**。新增 `prompts/` 包（`load_prompt()` + `stage1_prompt.txt`/`stage2_prompt.txt` 保留官方原文、`patch_prompt.txt` 本项目自研且已译为英文以维持修正 14/"翻译+路由"双重防护策略），`server/prompt_builder.py`/`llm/prompt_builder.py` 的 `_build_system()` 改为 `load_prompt(...).format(...)`；`server/planner.py`/`server/merge.py` 核查确认无需重写（本就只做"输入→调用 LLM→解析输出"）；全仓"clean-room"表述已删除，统一改为"official experimental prompts are retained as configuration, while algorithmic implementation is independently reproduced." | 见 `tests/test_prompts.py`（6 tests）；`core.datatypes.PaperMergeAction`/`SkipUpdate`/`DecisionLog` 预览字段顺带补充"非论文原文"澄清 docstring；全量回归 125 passed / 2 skipped，`_test_imports.py` 61 OK |
> 修正 9、10 是早期代码审计（对照文档逐一核对函数签名与调用点）发现的问题，修正 9 是会导致 Setting 2-4 联邦实验真实运行时抛异常的实质性 bug。修正 11 依据用户提供的论文 Appendix B.2 原文证据。修正 12 是 benchmark 层重构，不触及 core/client/server/library 任何代码。修正 13、14 是本次真实 API 联调（DashScope + qwen3.6-plus）过程中新发现并修复的运行时 bug，均已通过独立单元验证（`Task(task_id=..., name=..., description=..., difficulty=1)` 可正常构造；`resolve_litellm_model('qwen3.6-plus', 'https://dashscope.aliyuncs.com/compatible-mode/v1')` 返回 `'openai/qwen3.6-plus'`）。修正 15、16 是新增功能（非 bug 修复），分别对应 Phase12（真实 agent workspace 执行运行时）和 Phase13（真实实验阶段基础设施补缺），均为 additive-only 改动，未重构任何既有已测试模块。修正 18 是文档诚实性审计发现的问题（非代码 bug）——已确认 `server/planner.py`/`server/merge.py` 的算法逻辑本身从未复制官方 `.py` 源码，问题只出在提示词文本的来源声明不准确，现已更正声明并保留官方 Prompt 原文（Prompt 属于实验配置，非算法源码）。

---

## 七、已知局限（主动简化，非 bug，跑实验/写报告前应知情）

### 7.1 `privacy_gain()` 与论文真正的 SELR 指标（Appendix E, Eq.5）—— 详细差异对照

**论文原始定义**（Appendix E "Privacy Audit"）：论文并**不是**用 token 压缩比衡量隐私，而是定义了 **Sensitive Entity Leakage Rate（SELR）**：

$$
\text{SELR}(C) = \frac{|\{\,e \in \mathcal{E}_{\text{sens}}(\tau) : \text{leak}(e, C)\,\}|}{|\mathcal{E}_{\text{sens}}(\tau)|} \tag{5}
$$

其中 $C$ 是被审计的目标语料（如上传的 patch $\delta_i^t$），$\mathcal{E}_{\text{sens}}(\tau)$ 是从原始轨迹 $\tau$ 中抽取出的敏感实体集合（人名、银行账号、文件路径、SSN 形式的 ID 等）。判定 $\text{leak}(e, C)$ 的规则是 **strict-AND**：精确子串匹配 *或者* 两个独立 LLM 裁判都判定 $C$ 泄露了该实体的语义信息。

论文的完整审计流程分三阶段（Appendix E.2）：
1. **实体抽取**：LLM 抽取器解析完整轨迹 $\tau$，为每个候选实体打标签（`sensitive` / `task_necessary` / `neutral`），只对 `sensitive` 子集计算 SELR；
2. **审计语料组装**：对比多种语料——原始轨迹（ceiling）、朴素摘要（strawman，无脱敏指令的逐字 LLM 摘要，作为上界）、任务描述（floor，公开基线）、上传的 patch $\delta_i^t$、服务器广播回的个性化更新 $\Delta_i^t$、跨家族的无关 patch（负对照，用于检验审计流程本身是否会"误报"）；
3. **评判**：strict-AND 规则判定泄露。

论文 **Table 8** 实测结果（$n_{\text{sens}}=1{,}123$，覆盖 6 个 family × 3 个 worker × 3 轮 = 54 个审计单元）：

| 目标语料 $C$ | SELR（strict）| 95% CI |
|---|---|---|
| 原始轨迹（上界/ceiling） | 100.00% | — |
| Top-K 朴素摘要（strawman） | 52.00% | [49.1, 54.9] |
| 公开任务描述（下界/floor） | 22.53% | [20.2, 25.1] |
| **上传的 skill patch $\delta_i^t$** | **5.08%** | [3.9, 6.5] |
| 服务器广播的个性化更新 $\Delta_i^t$ | 0.09% | [0.02, 0.5] |
| 累计技能库（末轮快照） | 3.12% | [2.3, 4.3] |
| 跨家族负对照（sanity check） | 0.09% | [0.02, 0.5] |

论文进一步给出：按实体类型细分（Figure 5）PII 级类别（`filename` / `bank_account` / `date` / `price`）在 patch 中泄露率**严格为 0%**，残余泄露集中在 `magic_number`（12.1%）等领域常量；**任务重构攻击**（Appendix E.4）显示仅凭 patch 重建任务描述的余弦相似度为 0.532，接近"跨家族下界"0.357，远低于 strawman 摘要上界 0.799；**PII 金丝雀注入测试**（Appendix E.5，Table 9）向 9 条轨迹注入 6 类合成 PII（人名、内部工号、SSN 形式号码、内部邮箱、项目代号），54 次审计**全部 0 泄露**。

**本仓库 `evaluation/metrics.py::FederatedMetrics.privacy_gain()` 的实际实现**（已逐行核实源码）：

```python
@staticmethod
def privacy_gain(trajectory_tokens: int, patch_tokens: int) -> float:
    if trajectory_tokens <= 0:
        return 0.0
    return max(0.0, (trajectory_tokens - patch_tokens) / trajectory_tokens)
```

这与同文件中的 `compression_ratio(patch_tokens, trajectory_tokens) = max(0, 1 - patch/traj)` 在代数上**完全等价**（$(T_{\text{traj}} - T_{\text{patch}})/T_{\text{traj}} \equiv 1 - T_{\text{patch}}/T_{\text{traj}}$），代码 docstring 中也明确用中文注明了这一点（"这个公式在数值上与 `compression_ratio()` 完全相同…不是论文 Appendix E (Table 8) 里真正的 SELR"）。

**具体差距（不做简化，逐条列出）**：

| 维度 | 论文 SELR（Eq.5 / Table 8） | 本仓库 `privacy_gain()` |
|------|---------------------------|------------------------|
| 度量对象 | **敏感实体个数**（人名/账号/路径/ID 等，经 LLM 抽取并打标签） | **token 数量**（原始轨迹 token 数 vs patch token 数） |
| 判定方式 | strict-AND：精确子串匹配 **或** 双 LLM 裁判语义判定泄露 | 无判定逻辑，纯算术比例 |
| 是否区分实体敏感等级 | 是（`sensitive` / `task_necessary` / `neutral` 三级，只统计 `sensitive`） | 否，不做任何内容分析 |
| 是否有负对照/统计显著性 | 是（跨家族负对照 0.09%，95% Wilson 置信区间） | 否 |
| 是否验证过 PII 注入鲁棒性 | 是（6 类合成 PII 金丝雀，54/54 审计 0 泄露） | 否，代码里没有对应的注入测试 |
| 数值含义 | 泄露率（越低越好，理想 0%） | 压缩率（越高越"好"，但与隐私无必然因果） |
| 与本仓库 `client/distiller.py::PatchDistiller._audit_privacy()` 的关系 | 论文用独立审计流程（Appendix E），产出可比的 SELR 数值 | `_audit_privacy()` 只做正则扫描任务 ID / IP / 疑似凭证 / `confidential` 关键词，**仅用于告警**，不产出 SELR 数值，也未与 `privacy_gain()` 打通 |

**结论**：若要在报告中对照论文 Table 8 / Figure 5 / Figure 6 / Table 9 的隐私结果，**不能引用 `privacy_gain()` 的输出值**，因为它与 SELR 没有数值对应关系（一个基于实体计数，一个基于 token 计数）。

> ✅ **本次代码修正已新增部分对应实现**：`FederatedMetrics.sensitive_entity_leakage_rate(source_text, target_text)`（`evaluation/metrics.py`）用正则实体抽取（task_id / IP / 邮箱 / 电话 / 凭据 / 绝对路径 / 敏感标记词，复用 `client/distiller.py::_LEAK_PATTERNS` 的检测思路）+ 精确子串匹配，给出一个**实体级**（而非 token 级）的 SELR 近似值，比 `privacy_gain()` 更接近论文 Table 8 的统计口径。但它仍是**近似实现**，与论文的差距明确记录在该方法的 docstring 里，主要是：(1) 固定正则模式代替 LLM 实体抽取与三级敏感度打标签；(2) 精确子串匹配代替论文的 strict-AND 语义裁判（会低估真实泄漏率，尤其是改写/同义替换类泄漏）。若要严格复现 Table 8，仍需(1) 用 LLM/NER 做实体抽取与分级、(2) 加入 LLM 语义裁判、(3) 按 Eq.(5) 计算比例并给出 Wilson 置信区间——这部分工作量较大，未在本次修正范围内完成，`privacy_gain()` 和新方法都应在报告中明确标注各自的近似程度，不能与论文数字直接做定量对比。
>
> ✅ **本轮新增**：[`evaluation/privacy.py`](evaluation/privacy.py) 补齐了三个此前缺失的能力——① `scan_sensitive_entities(text)` 返回详细实体清单（值+类别，覆盖 person_name/email/bank_account/proper_noun 等）；② `compute_SELR(source_text, target_text)` 是**真正的 source→target 泄露比对**（从 source 抽取实体，逐一检查是否原样出现在 target 中），与旧版 `scan_for_sensitive_entities()`/`compute_selr_for_patch()`（各自独立扫描单一文本、把"实体密度"当成 SELR）有本质区别，前者才对应论文 Eq.(5) 的定义；③ `inject_canaries()` + `canary_injection_test(distill_fn, ...)` 实现了 Appendix E.5 的合成 PII 金丝雀注入审计（`DEFAULT_CANARIES` 含虚构人名/SSN/项目代号/邮箱），配合 `canary_reports_to_csv()` 可直接产出对照论文 Table 9 风格的审计记录。**仍未做到位**：`compute_SELR()` 的实体抽取仍是固定正则而非 LLM/NER，也没有 strict-AND 双裁判、没有 Wilson 置信区间，因此仍不能与论文 Table 8/9 的具体数字做定量对比，只能定性验证"泄露率显著低于原始轨迹"这一结论方向。回归测试见 [`tests/test_privacy_selr.py`](tests/test_privacy_selr.py)（10 项，全部通过）。

### 7.2 其余已知局限（部分已在本轮修正）

| 局限 | 影响范围 | 说明 |
|------|---------|------|
| ⚠️ **部分修正**：真实 SkillFlow family（20 个）验证机制已接入，但实际验证脚本内容仍未填充 | 影响真实 benchmark 的 reward 计算准确性 | `VerificationSpec.type` 现已支持 `"docker"`（`benchmark/task.py`），搭配 `executor/harbor_adapter.py::HarborExecutor`（真实 `docker build`/`docker run`，无 Docker 时明确返回 `DockerUnavailable`，不伪造成功）+ `benchmark/verifier.py::DockerScriptVerifier`；`"none"` 类型也新增了显式的 `NoVerificationVerifier`（不再被通用 `except` 包装成模糊异常）。**仍未解决的部分**：`benchmark/families/*.json` 里那 20 个真实 family 自身还没有 `docker_image`/`test_script` 内容（需要官方 SkillFlow 数据集自带的 Dockerfile + 测试脚本，本仓库未下载/未接入），因此目前运行仍会命中 `"none"` 分支，只是从"模糊异常"变成了"明确的未验证说明"——机制就绪，数据未就绪，详见 `docs/SIMPLIFICATIONS.md` | 见上一条 |
| Benchmark 手写家族规模 | 5 个手写家族 < 论文 20 个 | 手写的 5 个 `function_test` 家族是规模简化，用于快速本地回归测试；真实规模由另外 20 个下载的 SkillFlow family 补充，合计 25 个，接近论文规模但验证链路尚未完全打通（见上一条）。论文实际 20 个 family 名称（Table 1/6 已核实）：Cross-Format-Data-Reconciliation、Distribution-Center-Auditing、Document-Fraud-Detection、Embedded-Data-Repair、HWPX-Document-Automation、Healthcare-Cost-Benefit-Analysis、Industry-Correlation-Analysis、Inventory-&-Finance-Integration、Medical-Data-Standardization、OCR-Data-Extraction、Operational-Recovery-Planning、Production-Capacity-Planning、SEC-13F-Financial-Analysis、Supply-Chain-Replenishment、PPT-Formatting-Optimization、Compensation-Scenario-Modeling、DMAIC-Quality-Analysis、Financial-Statement-Rolling、Sales-Pivot-Analysis、Weighted-Risk-Assessment |
| ✅ **已修正**：`CapabilityTracker.init_from_patches()` 用 `worker_id` 作占位 workflow key | 原问题：Stage1 LLM 调用失败降级时，能力矩阵行名可能长期停留在占位状态，且一旦 Stage1 成功后也不会清理，长期积累成"僵尸行" | **修正内容**：新增 `_prune_placeholder_rows()`，在 `update_from_plan_dict()`（即 Stage1 成功返回真实 `capability_matrix` 时）被调用后，自动删除所有仍以 `worker_id` 字面量命名、且未出现在本轮 `plan_matrix` 中的矩阵行。仍保留的局限：`_fallback_plan()` 降级路径下（Stage1 调用失败）矩阵确实不会被清理或更新为真实 workflow 名，这是论文设计本身要求 LLM 推断 workflow 语义、无法在客户端/服务端本地规则中绕过的固有局限 |
| ✅ **已修正**：`PatchDistiller.distill()` 的 `profile=None` 降级路径 | 原问题：docstring 声称"None → 从 router 中查 trajectory.worker_id"，但实现里直接 `raise ValueError`，与文档不符 | **修正内容**：`llm/router.py::BackboneRouter` 新增 `_profiles` 存储与 `register_profile()` / `get_profile()` 方法，`from_profiles()` 自动为每个 worker 注册 profile；`distill(profile=None)` 现在会先尝试 `self._router.get_profile(trajectory.worker_id)`，仍查不到才 `raise ValueError`（错误信息已更新，明确提示调用方应显式传入 `profile` 或先调用 `router.register_profile()`），docstring 与实现现已一致 |
| ✅ **已修正**：`resolve_provider_for_model()` 关键词兜底 / `DEFAULT_SERVER_PROVIDER` / `make_worker_profile()` 默认值仍指向 Anthropic 兼容端点 | 原问题：自定义 qwen/glm 模型名、未登记模型名的通用兜底、以及 GLM-5 server backbone 默认值均指向 `dashscope_anthropic`，存在与已修正 #14 相同的 ASCII 编码崩溃风险 | **修正内容**：`llm/providers.py` 中 `resolve_provider_for_model()` 的 `"qwen"/"glm"` 关键词分支与函数末尾默认兜底均改为 `dashscope_openai`；`make_worker_profile()` 的 `MODEL_TO_PROVIDER.get(model, ...)` 默认值同步改为 `dashscope_openai`；`MODEL_TO_PROVIDER["glm-4"]` 精确匹配项、以及 `DEFAULT_SERVER_PROVIDER`（GLM-5 server backbone 默认 provider）均从 `dashscope_anthropic` 改为 `dashscope_openai` |
| ✅ **新发现并已修正**：`server/prompt_builder.py` 中 Stage1/Stage2 提示词含大量中文内容 | 原问题：`_STAGE1_SCHEMA` / `_STAGE2_SCHEMA` 占位符、`_section_*()` 各段标题与字段标签、`_HARNESS_STYLE_HINTS`、`action_guide` 等均含中文，直接拼接进发给服务器 backbone（GLM-5）的 user prompt，与已修正 #14（client 侧 `llm/prompt_builder.py`）是同一类风险，但服务器侧此前未被覆盖 | **修正内容**：已将上述所有拼接进提示词正文的中文字符串全部翻译为英文（模块级 docstring/注释保留中文，不影响提示词内容）；配合上一条 `DEFAULT_SERVER_PROVIDER` 改为 `dashscope_openai`，形成"翻译 + 路由"双重防护，与 client 侧修正策略一致 |
| 无统计显著性检验（对照论文 Appendix D） | 报告中的成功率提升缺少置信区间 | 论文 Appendix D 对 5 个代表性 family 做了多次独立 SE 基线运行（Qwen×4、GLM×3、Kimi×2）并给出 95% bootstrap 置信区间（Pooled +14.35pp, CI [+9.17, +19.62], W/T/L=12/3/0）；本仓库 `evaluation/evaluator.py` 和 `evaluation/metrics.py` 均未实现多次重复运行的聚合统计或置信区间计算，报告中的单次运行数字应避免与论文表格直接做显著性对比——**未修正，属于统计工具链缺失，超出本次代码修正范围**，详见 `docs/SIMPLIFICATIONS.md` |

> 📄 **完整的、按模块组织的"简化点清单"**（比本节更详尽、覆盖全仓库、不局限于上表 6 项）已单独整理到 [`docs/SIMPLIFICATIONS.md`](docs/SIMPLIFICATIONS.md)，供撰写复现报告时逐条核对。

---

## 八、快速安装与运行

```bash
pip install -r requirements.txt
# 关键依赖版本锁定：litellm>=1.67.0,<1.85.0（≥1.85 在 Windows 上需要 Rust 编译工具链）
#                  pydantic>=2.8.0  pyyaml>=6.0.0  python-dotenv>=1.0.0  tqdm>=4.66.0
```

配置 API 密钥（`.env` 文件，参考 `.env.example`）：
```
DASHSCOPE_KEY=sk-...    # Qwen / GLM（DashScope）
MOONSHOT_KEY=sk-...     # Kimi K2.5（Moonshot）
ANTHROPIC_API_KEY=sk-... # Claude 原生（可选）
```

运行 Setting 1（SE 基线，8 轮，qwen3.6-plus）：
```bash
python experiments/run_experiment.py --config experiments/configs/setting_se.yaml --rounds 8 --output results/setting1_se
```

真实实验前建议先跑环境自检：
```bash
python scripts/preflight_check.py
python scripts/test_llm_connection.py
python scripts/validate_configs.py
```

单元测试：
```bash
python _test_imports.py      # 全模块导入健康检查（当前 61 个模块 OK, 0 FAIL）
python _test_e2e.py          # 端到端回归（mock LLM，SE + FedSkill 各 2 轮）
pytest tests/ -v             # 完整 pytest 套件（当前 125 passed, 2 skipped；--real 标志下才跑真实 API 测试）
```

用新增 `ExperimentRunner` 门面类快速 dry-run 某个 Setting（等价于直接调 `run_experiment.py --dry-run`，但按论文 Setting 编号索引，不用记具体 YAML 文件名）：
```bash
python -c "from experiments.runner import ExperimentRunner; ExperimentRunner.for_setting(1).run(dry_run=True)"
```

一键跑通 Setting 1-4 并生成论文对照表格（新增 [`experiments/run_paper_table.py`](experiments/run_paper_table.py)，复用 `ExperimentRunner` + `ResultsExporter`，单个 setting 失败不会中断整体流程）：
```bash
python experiments/run_paper_table.py                       # 跑全部 4 个 setting 并导出 CSV/图表
python experiments/run_paper_table.py --settings 1,3 --rounds 2  # 只跑 Setting 1/3，各 2 轮（冒烟测试）
python experiments/run_paper_table.py --skip-run             # 跳过执行，只对已有 results/ 重新生成表格
```
