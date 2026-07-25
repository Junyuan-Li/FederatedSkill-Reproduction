# FederatedSkill-Reproduction 仓库审计报告（GitHub 开源整理 · 第一阶段）

> 生成方式：本文件基于对仓库磁盘内容的**完整实地扫描**（`list_dir` + PowerShell 体积统计），
> 不是凭空推测。所有分类结论都会在「二、文件分类」中给出具体路径。
> 本阶段**只做盘点，不做任何删除或修改**。

---

## 一、当前目录结构与体积概览

### 1.1 顶层目录体积/文件数（已实测）

| 目录 | 大小 | 文件数 | 说明 |
|---|---|---|---|
| `benchmark/` | 1596.6 MB | 5381 | 含 `benchmark/cache/SkillFlow-Task/`（HuggingFace 下载数据集，占绝大部分体积） |
| `results/` | 20.1 MB | 2548 | **官方/正式实验结果目录**（`.gitignore` 已排除，不会提交到 git） |
| `results_setting2/` | 9.0 MB | 987 | 根目录下的**非正式**调试结果（不在 `.gitignore` 内） |
| `results_real_family1/` | 1.0 MB | 183 | 同上，调试产物 |
| `tests/` | 1.0 MB | 99 | pytest 套件（40 个测试文件 + `__pycache__`） |
| `results_se_real/` | 0.8 MB | 174 | 同上，调试产物（**含今天/本次会话刚生成的运行结果**，见下方特别提示） |
| `evaluation/` `experiments/` `scripts/` | <0.5 MB 各 | — | 核心源码/脚本 |
| `results_family_debug/` | 0.31 MB | 160 | 调试产物 |
| `results_phase1_family_check/` | 0.31 MB | 160 | 调试产物 |
| `server/` `client/` `harness/` `core/` `executor/` `llm/` `docs/` `prompts/` | <0.25 MB 各 | — | 核心源码/文档 |
| `.pytest_cache/` `__pycache__/` | <0.1 MB 各 | — | 可重新生成的缓存 |
| `results_family_real/` | 0.02 MB | 3 | 调试产物 |
| `tmp_cli_validation/` | ~0 | 3 | 仅含 1 个探测文件 `claude-code/hello_agent.txt` |
| `results_family_real_setting2/` | 0 | 0（空） | 空目录 |
| `.vscode/` `configs/` `config/` | ~0 | 1 各 | 配置 |

> ⚠️ **特别提示**：`results_se_real/` 中最新的一次运行时间戳是本次会话终端刚执行的
> `experiments/run_experiment.py --config experiments/configs/setting_se.yaml --family Industry-Correlation-Analysis --output results_se_real`
> 命令产生的结果。删除前请确认这份结果是否还需要保留或另存。

### 1.2 目录树（核心源码部分，未展开 `results*/`、`benchmark/cache/`）

```
FederatedSkill-Reproduction/
├── core/                      # 数据类型 + 常量 + 异常（论文 §3 形式化定义）
├── llm/                       # LLM 调用抽象层（litellm 统一路由）
├── prompts/                   # Stage1/Stage2/Patch 提示词文本包
├── client/                    # 客户端执行 + patch 蒸馏 + agent_runtime/
├── server/                    # 服务端演化（Stage1 planner + Stage2 merge + evolution）
├── benchmark/                 # 任务 benchmark 系统 + families/ + skillflow_adapter/ + cache/（数据集缓存）
├── executor/                  # 真实任务执行沙箱（mock/python/skillflow/agent workspace）
├── evaluation/                # 实验评估指标 + 报告 + capability_tracker
├── experiments/                # 实验控制器（run_experiment.py 统一入口）+ configs/
├── scripts/                    # 环境预检 / 数据集下载 / 代表性子集选取与验证脚本
├── harness/                    # 真实 CLI Agent Harness（claude-code/qwen-code/kimi-cli）
├── config/  configs/            # 全局 settings.yaml / runtime.yaml
├── docs/                        # 已整理的复现文档（论文对照、实验设置、简化点声明等）
├── tests/                       # pytest 回归测试套件（40 个文件）
├── results/                     # 正式实验结果（.gitignore 已排除）
├── .vscode/tasks.json           # VS Code 任务（预检/下载/代表性子集运行等）
├── main_trainer.py, run.py      # 入口脚本
├── requirements.txt, requirements-real.txt
├── pytest.ini, README.md, .gitignore, .env
│
├── （根目录调试/日志/一次性脚本，见下方分类 B/C）
├── （根目录 results_*/、tmp_cli_validation/ 等非正式结果目录，见分类 B）
```

### 1.3 特殊发现：git 仓库结构

`FederatedSkill-Reproduction/` **不是独立的 git 仓库**，而是更大的 `D:\pythonlesson` 仓库的一个子目录；
该上级仓库同时还追踪了大量与本项目无关的个人课程作业文件（如 `pythonProject/*.py`），且未配置任何 `remote`。
**发布到 GitHub 前，需要在 `FederatedSkill-Reproduction/` 内单独执行 `git init` 并新建独立仓库**——这不属于"清理"范畴，
是否现在执行请你确认（详见 `cleanup_plan.md` 末尾"发布前置事项"）。

---

## 二、主要目录功能说明

| 目录 | 功能 |
|---|---|
| `core/` | 全局 Pydantic 数据模型（`WorkerProfile`/`Trajectory`/`WorkerPatch`/`EvolutionPlan`/`CapabilityMatrix` 等）+ 超参数常量 + 异常层次 |
| `llm/` | 统一 LLM 调用层：`LLMBackbone`、Provider 路由、重试策略、JSON 解析、`generate()` 统一接口 |
| `prompts/` | Stage1/Stage2/Patch 三份提示词文本（与算法代码解耦） |
| `client/` | `TaskExecutor`（任务执行）、`PatchDistiller`（patch 蒸馏）、`SkillLibrary`（技能库）、`TrajectoryCompressor`（轨迹压缩）、`FederatedClient`（封装）、`agent_runtime/`（Planner-Action-Observation 循环） |
| `server/` | `EvolutionPlanner`（Stage1）、`EvolutionExecutor`（Stage2）、`FederatedServer`（串联 `run_round()`）、`CapabilityTracker`、`EvolutionMemoryStore` |
| `benchmark/` | 任务/家族定义、采样器、验证器（`verifier.py`）、真实 SkillFlow 数据集适配（`skillflow_adapter/`）与下载缓存（`cache/`） |
| `executor/` | 真实/沙箱任务执行运行时（mock、python 沙箱、skillflow 脚本、agent workspace 四种执行器） |
| `evaluation/` | 论文核心指标计算、跨轮评估、Capability Matrix 演化追踪、报告与绘图 |
| `experiments/` | 统一实验 CLI 入口 `run_experiment.py`、`ExperimentRunner` 门面类、Setting1-4 与消融的 YAML 配置（`configs/`） |
| `scripts/` | 环境预检、SkillFlow 数据集下载、代表性子集选取/校验/运行、各类真实 API 连通性检查工具 |
| `harness/` | 真实 CLI Agent Harness 适配（claude-code / qwen-code / kimi-cli） |
| `docs/` | 已整理归档的论文对照与复现说明文档（9 个文件） |
| `tests/` | pytest 回归测试（40 个测试文件，覆盖 benchmark/executor/harness/metrics/skillflow 全链路） |
| `results/` | 正式实验产出（按 setting/family 组织，已在 `.gitignore` 中排除，不进版本库） |
| `config/` `configs/` | 全局运行时配置（`settings.yaml`、`runtime.yaml`） |

---

## 三、文件分类

### A. 必须保留（核心源码 / 实验配置 / 实验结果 / README 需引用）

- **核心源码目录（完全不改，不删）**：`core/`、`llm/`、`client/`、`server/`、`benchmark/`（除 `cache/` 需在 §C 单独处理）、`executor/`、`evaluation/`、`experiments/`、`prompts/`、`harness/`
- **配置**：`config/settings.yaml`、`configs/runtime.yaml`、`experiments/configs/*.yaml`
- **入口脚本**：`main_trainer.py`、`run.py`、`experiments/run_experiment.py`
- **依赖声明**：`requirements.txt`、`requirements-real.txt`
- **测试配置**：`pytest.ini`（若保留 `tests/`，见 §C）
- **文档（已整理归档）**：`docs/` 下全部 9 个文件（`audit_report_v1.md`、`experiment_settings.md`、`paper_fidelity_report.md`、`paper_mapping.md`、`paper_pipeline_alignment_client_side.md`、`real_experiment_setup.md`、`reproduction_changes.md`、`reproduction_protocol.md`、`SIMPLIFICATIONS.md`）
- **正式实验结果**：`results/`（含 `subset_setting1_v3/paper_tables_v1/` 等已生成的论文用表格）
- **`scripts/` 中已接入 VS Code 任务、属于"代表性子集"复现流程的脚本**：
  `download_skillflow_dataset.py`、`fetch_skillflow_instructions.py`、`preflight_check.py`、`preflight_representative_subset.py`、`select_representative_families.py`、`validate_subset_protocol.py`、`run_representative_subset.py`、`run_phase1_setting1_validation.py`、`install_dependencies.sh`、`install_harness.sh`、`validate_configs.py`
- **`README.md`**（本次任务将重写，见第四阶段）
- **`.gitignore`**（本次任务将补充规则，见 cleanup_plan.md）
- **`.env`**（本地必需的真实 API Key，已在 `.gitignore` 中排除，不提交但本地保留）

### B. 可以删除（debug 脚本 / 临时测试 / 临时日志 / cache / 中间生成文件）

以下均为**一次性调试产物**，与论文复现的正式链路无关，具体清单见 `cleanup_plan.md`：

1. 根目录 24 个下划线前缀调试脚本/日志（`_check.py`、`_mock_*`、`_phase*`、`_task5_*`、`_test_e2e.py`、`_test_imports.py`、`_tmp_*`、`_validate_*`、`_phaseA_pytest.xml`、`_full_pytest.log`）
2. 根目录 18 个 `*.log` 调试日志（`api_check.log`、`check_run.log`、`cli_validation_*.log`、`dryrun1-4.log`、`phase1_sanity3.log`、`phase1_setting4_real3*.log`、`setting1_run.log`、`task5_*.log`）
3. 根目录 9 个 `pytest_*.txt`/`.log` 历史测试输出快照
4. `download_log.txt`、`fetch_log.txt`
5. 根目录**非正式** `results_*` 调试结果目录（7 个）：`results_family_debug/`、`results_family_real/`、`results_family_real_setting2/`、`results_phase1_family_check/`、`results_real_family1/`、`results_setting2/`、`results_se_real/`（⚠️ 见上方特别提示，`results_se_real/` 含本次会话最新一次运行）
6. `tmp_cli_validation/`（仅探测文件 `hello_agent.txt`）
7. `__pycache__/`、`.pytest_cache/`（所有层级，可从 `.gitignore` 规则重新生成）
8. 根目录一次性连通性/校验报告：`api_preflight_report.json`、`api_preflight_run3.txt`、`qwen_route_report.json`、`subset_preflight_report.json`、`phase1_validation_report_failed_glm_endpoint.{json,md}`、`phase1_validation_report_failed_qwen_auth.{json,md}`、`phase1_validation_report_failed_qwen_tool_approval.{json,md}`
9. 根目录孤立的 `experiment_summary.json`（6KB，与 `results/` 内各次运行自带的同名文件重复，非正式汇总）

### C. 需要人工确认（用途或去留有歧义，默认保留，等待你的决定）

| 项目 | 疑问点 | 建议 |
|---|---|---|
| `tests/`（40 个文件） | 你的指令允许删除"测试代码"，但这是经终端验证通过（125 passed, 2 skipped）的真实回归测试套件，是"可复现"仓库的重要证据 | **建议保留**；如确认要删，请明确回复 |
| 根目录 21 个分析类 `.md`（`distiller_dataflow.md`、`execution_call_graph.md`、`execution_gap_report.md`、`experiment_assets_to_port.md`、`experiment_environment_report.md`、`experiment_protocol_audit.md`、`family_selection_report.md`、`federation_chain.md`、`metric_dataflow.md`、`model_alignment.md`、`official_component_mapping.md`、`official_experiment_alignment_audit.md`、`paper_experiment_protocol.md`、`paper_protocol_table.md`、`phase1_runner_audit.md`、`phase2_task_trace.md`、`prompt_alignment_report.md`、`reduced_experiment_plan.md`、`runtime_fidelity_report.md`、`skill_lifecycle.md`、`timeout_policy_report.md`、`trajectory_alignment.md`） | 内容有价值但与 `docs/` 中已整理文档存在重叠，堆在根目录不整洁 | 建议移入 `docs/analysis_notes/`（属于目录调整，不是删除，需你确认是否执行） |
| `scripts/` 中的其余脚本（`build_case_analysis.py`、`check_api_connection.py`、`check_cli_harness.py`、`check_experiment_integrity.py`、`check_federated_fidelity.py`、`check_qwen_routes.py`、`compare_official_protocol.py`、`compare_with_paper.py`、`paper_fidelity_check.py`、`probe_kimi_cli.py`、`resume_phase0_from_round3.py`、`run_phaseA_setting1.py`、`run_small_scale_phase0.py`、`subset_setting1_v3_report.py`、`test_llm_connection.py`、`validate_phase1_real.py`） | 部分像一次性调试脚本，部分像有用的连通性检查工具，无法仅凭文件名 100% 判定 | 默认保留；如需要我可以逐个读取内容再给出更细的删除建议 |
| `benchmark/cache/SkillFlow-Task/`（约 1.6GB，占仓库体积绝大部分） | 这是从 HuggingFace 下载的原始数据集缓存，不是源码 | **不建议删除本地文件**（复现实验需要），但**建议加入 `.gitignore`**，不提交到 GitHub |
| `.vscode/tasks.json` | 其中部分任务指向 §C 中列出的脚本（如 `run_phaseA_setting1.py`），若这些脚本后续被删除会导致任务失效 | 保留 `.vscode/`，是否精简任务列表待你决定 |
| `.env.example` | 当前仓库**没有** `.env.example` 模板文件，只有真实的 `.env` | 建议新增一份不含真实密钥的 `.env.example`（属于"添加说明文档"，允许范围内） |
| git 仓库归属 | 当前目录不是独立 git 仓库，是 `D:\pythonlesson` 大仓库的子目录 | 发布前需单独 `git init`，是否现在执行待你决定 |

---

## 四、下一步

按你的要求，第二阶段的具体删除清单（含每个文件的完整路径）已整理到同目录下的 `cleanup_plan.md`。
**在你确认之前，我不会执行任何删除操作**（尤其是 `results_se_real/` 等可能刚生成的实验结果，以及 §C 中的所有条目）。
