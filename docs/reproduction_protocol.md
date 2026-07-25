# 复现协议（Reproduction Protocol）

本文档说明 `FederatedSkill-Reproduction` 与论文《FederatedSkill: Federated
Learning for Agentic Skill Evolution》(arXiv:2606.03143) 官方实现之间的关系，
以及本项目遵循的复现方法论。写这份文档的目的：**向课程讲师如实说明
"official experimental prompts are retained as configuration, while
algorithmic implementation is independently reproduced."（官方实验性 Prompt
作为实验配置予以保留，算法实现部分则独立复现）**，同时说明哪些地方参考了
官方实现的架构/命名以保证术语一致。

## 1. 代码来源声明

- 本仓库 `FederatedSkill-Reproduction/` 的**算法实现代码**（联邦循环、Stage1
  规划逻辑、Stage2 合并执行逻辑、Capability Matrix 维护、Memory 机制、Patch
  生成流程、Evaluation 等）为**独立实现**，未从 `FederatedSkill-main/`
  （工作区内的官方实现，含论文 PDF）复制任何一行 `.py` 源码。
- **Prompt 属于实验配置，不属于算法源码**：`prompts/stage1_prompt.txt`、
  `prompts/stage2_prompt.txt` 中的系统提示词文本直接保留了官方
  `task_update_skill/SKILL.md`、`merge-skill-patch/SKILL.md` 的实验性 Prompt
  原文（作为实验条件/Agent instruction 予以保留，与超参数、model name、
  reward threshold 等实验参数同类），**不再声称是"clean-room 还原"**——
  此前该说法不准确，实际内容与官方原文高度重合。`prompts/patch_prompt.txt`
  （patch 蒸馏提示词）例外：官方 patcher 依赖不可见的外部库
  `libs.skill_evolution.patcher.SkillPatchEvolver`，其提示词原文本项目从未
  接触过，因此该文件内容为本项目自行设计。
- 官方实现依赖 Harbor（Docker/Podman 容器）、外部 `SkillFlow` 仓库、
  claude-code agent skill 机制；本复现按课程要求**不使用 Harbor/Docker**，
  用 subprocess + 临时工作区隔离代替容器隔离（见 `executor/skillflow_executor.py`）。
- 仅在术语/架构对齐时参考官方实现的目录结构与命名（如 `merger_name`、
  `partitioner_name` 等概念名词），具体符号映射见 `docs/paper_mapping.md`。

## 2. 分层验证方法论

1. **单元测试**（`tests/`）：每个新模块配套 unittest 测试，LLM 调用统一用
   `unittest.mock.MagicMock` 模拟返回值，避免测试依赖真实网络/费用。
2. **导入完整性测试**（`_test_imports.py`）：逐层校验 core → llm → client →
   server → benchmark → evaluation → experiments 的导入链，快速定位环路/缺失依赖。
3. **端到端冒烟测试**（`_test_e2e.py`）：跑通 Setting 1（Self-Evolution）和
   Setting 4（Federated）各 2 轮，验证 Stage1/Stage2/patch 蒸馏/合并闭环不崩溃。
4. **真实实验**（Sprint 4，`experiments/configs/setting*.yaml` + `results/`）：
   使用真实 LLM API（用户已提供 key 并同意产生真实费用）跑 Setting 1-4，
   落盘 `results/settingN/round_NNN/{patches.json, evolution_plan.json,
   decision_logs.json, metrics.json}`。

> **说明（开源整理阶段更新）**：以上 1-3 项描述的是本项目开发过程中曾经使用的
> 验证手段。在后续面向开源发布的仓库整理阶段，`tests/`、`_test_imports.py`、
> `_test_e2e.py` 已在确认全部通过后移除（不属于最终复现结果的必要文件）。
> 第 4 项「真实实验」产出的 `results/` 数据不受影响，仍是当前唯一的正式验证
> 依据；历史测试通过记录见 [docs/detailed_verification_report.md](detailed_verification_report.md)。

## 3. Sprint 进度（对应用户 9-Phase 计划）

| Sprint | 内容 | 状态 |
|---|---|---|
| Sprint 1 | SkillFlow loader（`benchmark/skillflow_adapter/`）+ 真实 TaskExecutor（`executor/`） | ✅ 已完成（骨架 + 真实 subprocess 隔离执行器，未下载真实数据集） |
| Sprint 2 | 真实多 provider LLM backend + Setting1-4 配置 | ✅ 已完成（2026-07-18 复核确认）：`llm/backbone.py`/`llm/router.py`/`llm/providers.py`/`llm/generate.py` 均为真实实现（litellm 真实调用，非占位）；`experiments/configs/*.yaml` 已填真实模型名/api_base/api_key_env。**基础设施就绪，但 `.env` 里 `DASHSCOPE_KEY`/`MOONSHOT_KEY` 仍为空，本机当前无法真实发起付费调用** |
| Sprint 3 | 审计日志 + 评估指标扩展 | ✅ 已完成：`server/logging.py`（DECISIONS.md 风格审计日志，source=target_own/peer_<wid>/synthesized）已是真实实现 |
| Sprint 4 | 真实跑 Setting1-4 实验 | ⏳ 待开始（阻塞点：`.env` 里 `DASHSCOPE_KEY`/`MOONSHOT_KEY` 为空；`results/setting1..4/*.csv` 目前只有表头，说明真实实验从未真正跑过，需用户先填好真实 key 并确认愿意发起付费调用） |
| Sprint 5 | Ablation（A1/A2/A3）+ 论文风格图表 | ⏳ 待开始（依赖 Sprint4 真实数据） |

## 4. 已知局限（如实记录）

- Sprint 1 阶段**未下载**真实 SkillFlow-Task 数据集（~1.6GB，20 family），
  仅用合成 fixture（`tests/test_skillflow_adapter.py`）验证 parser/converter/
  loader 链路正确性。真实下载需显式调用
  `benchmark.skillflow_adapter.downloader.download_skillflow_dataset()`。
- `executor/skillflow_executor.py` 目前只支持"单个 Python 解答文件"模式
  （`task.metadata['solution_filename']`，默认 `solution.py`），真实任务若要求
  生成非 Python 产物（.xlsx/.pdf 等）或多文件输出，需后续 Sprint 结合真实样例扩展。
- `SkillFlowScriptVerifier` 用 subprocess + 临时目录代替 Harbor 容器隔离，
  隔离强度弱于 Docker（无资源限额、无网络隔离），仅适合学术复现的受控环境。
