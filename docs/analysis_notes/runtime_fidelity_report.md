# runtime_fidelity_report.md — Phase 1：运行时协议保真度报告

对应任务书 Phase 1 要求："Verify agent/task/verifier timeouts, retry
policy, and execution isolation match the official runtime protocol
(not the paper's algorithm)."

> **说明**：本报告要求覆盖的 5 项（①官方 timeout 来源 ②当前值
> ③目标值 ④retry policy 对比 ⑤benchmark 兼容性），**在本会话更早阶段
> 已经作为"Task Execution Timeout Audit"完整完成并落地修复**（见同目录
> [timeout_policy_report.md](timeout_policy_report.md)，含完整审计证据链 +
> 修复 diff 说明 + 回归测试结果）。本文档不重复造轮子，而是把已完成的
> 结论按本次任务书要求的结构重新汇总成独立文件，并补充一项本次新发现、
> 尚未处理的小偏差（§5）。**本文档本身不包含任何新的代码改动。**

---

## 1. Agent Timeout

| 项 | 值 | 证据 |
|---|---|---|
| 官方来源 | SkillFlow-Task 数据集每任务自带 `task.toml::[agent] timeout_sec`（抽样恒为 1800.0）；官方 `paper_logs/*/config.json::agent_timeout_multiplier: 1.0`，即原样生效不缩短 | `FederatedSkill-main/FederatedSkill-main/paper_logs/4_fed_hetero_mixed_cli/qwen3.6-plus/DMAIC-Quality-Analysis/round_000/worker_0/config.json` |
| 修改前（本仓库） | 固定 600s 工程常量（`configs/runtime.yaml::real_experiment.timeout`），与 task.toml 完全无关 | `timeout_policy_report.md` §2/§4 偏差1 |
| **修改后（已落地）** | 优先读取该 task 的 `task.toml [agent] timeout_sec`（经 parser→converter 写入 `Task.metadata["agent_timeout_seconds"]`），`harness/cli_harness_base.py::execute_task()` 取 `effective_timeout = task.metadata.get("agent_timeout_seconds") or self.default_timeout`；`configs/runtime.yaml` 的 600 改为 **1800**，仅作字段缺失时兜底 | `timeout_policy_report.md` §0.3；已通过 286 passed / 2 skipped 全量回归验证 |
| Setting1-4 是否统一 | 是，三个 setting 走同一套 harness 基类逻辑，无 per-setting 差异 | `harness/cli_harness_base.py` 为 4 个 setting 共用基类 |

## 2. Verifier Timeout

| 项 | 值 | 证据 |
|---|---|---|
| 官方来源 | `task.toml::[verifier] timeout_sec`，抽样 900.0-1200.0，逐 family 不同（非全仓库统一常量） | 抽样 3 个 family 的 `task.toml`（见 `timeout_policy_report.md` §2 表格） |
| 修改前 | parser 只找 TOML 顶层 `timeout_seconds`/`timeout` 键（task.toml 实际是嵌套 `[verifier]` section，永远匹配不到），静默 fallback 到硬编码 30s；`converter.py` 再做 `min(max(x,1),300)` clamp，即使读对了也会被砍到 300 上限 | `benchmark/skillflow_adapter/parser.py:123`、`converter.py:64`（`timeout_policy_report.md` §4 偏差2） |
| **修改后（已落地）** | `parser.py` 新增 `_section_timeout_sec()`，按 `[verifier].timeout_sec` 正确读取（找不到才用默认 900.0）；`converter.py` 去掉人为 300 上限；`benchmark/task.py::VerificationSpec.timeout_seconds` 的 Pydantic 约束从 `le=300` 放宽到 `le=3600`（仍是有限上界，防脏数据挂起，非无限超时） | `timeout_policy_report.md` §0.3 |
| `[environment]` 段 | 同理新增 `environment_timeout_seconds` 解析（`timeout_sec`/`build_timeout_sec` 两个别名），保留在 `RawSkillFlowTask` 上，本轮范围外逻辑未使用，暂不影响其它路径 | 同上 |

## 3. Retry Policy

| | 官方（`paper_logs/*/config.json`） | 本仓库修改前 | 本仓库修改后（已落地） |
|---|---|---|---|
| `max_retries`（task 级，超时/验证失败） | 0 | 2（`setting_se*.yaml`），不区分异常类型 | 0（`SelfEvolutionRunner` 默认值 + 全部 setting yaml + `run_experiment.py` 兜底默认值三处同步改为 0） |
| 超时/执行失败是否重试 | 否，显式排除 `AgentTimeoutError`/`VerifierTimeoutError`/`RewardFileEmptyError`/`VerifierOutputParseError`/`RewardFileNotFoundError` | 是，无条件重试 | 否；耗尽后不再 `raise` 中断整个 family，记为 `reward=0.0` 继续下一个 task（`PatchDistillationFailure` 例外，仍中断——独立失败类别，非本次范围） |
| Setting2-4（联邦场景）client-phase retry | — | `experiments/federated.py::_run_client_phase_with_retry()` 默认 `max_retry=2`，失败仍向上传播 | **本轮未改动**——涉及服务器端多 client 补丁聚合时序/语义，属用户明确禁止触碰的"federation logic/aggregation"范畴，留待你确认后再决定是否对齐官方的 0 |

## 4. Execution Isolation

| 项 | 状态 | 证据 |
|---|---|---|
| family 顺序透明化 | `experiment_summary.json` 新增 `family_order: "sorted_by_name"` / `family_order_reason: "official_order_unavailable"` / `family_ids_in_order: [...]`，如实记录排序依据 | `timeout_policy_report.md` §0.3 |
| family 间清理 | 复用既有 `family_failure_cleanup()`：每个 family 开始前清空技能库/工作区；意外异常中断后清理临时产物、继续下一个 family。Issue2 落地后，多数"task 执行失败"已不再触发中断路径（变成 reward=0 正常继续），该函数职责收窄但未被移除 | 同上 |
| 非合规后台实验 | 此前用 `timeout=600`/`max_retry=2` 跑的后台终端（ID `7034d189-...`）核实已不存在（已结束/会话已重启），无需手动终止；其 `results/phase1_sanity_se3/`、`results/phase1_sanity_se3_stale_20260721_1150/` 产物**不得**用于最终结论，仅供参照 | 本会话终端核实 + `timeout_policy_report.md` Issue7 |

## 5. 本次新发现、尚未处理的小偏差（供你确认，未擅自修改）

官方仓库 README 记录了独立于"task 级 retry"之外的 **LLM 层 429/限频重试**
调参环境变量：`SKILLFL_AGENT_429_RETRIES`(默认 20) /
`SKILLFL_AGENT_429_BASE_SLEEP`(默认 30) / `SKILLFL_AGENT_429_MAX_SLEEP`(默认 600)。
本仓库确认**存在功能对等实现**（`llm/retry.py`+`llm/backbone.py`+
`llm/llm_client.py`：区分 `RATE_LIMIT`（指数退避，`max_rate_retries` 默认
`MAX_RETRY_ATTEMPTS=20`，与官方 20 一致）与 `TRANSIENT`（有界重试，
`TRANSIENT_MAX_RETRIES=5`，官方 README 未提及此细分，可能是本仓库更细的
工程实现）两级，**这与 Issue2 修复的"task 级 max_retry"是完全不同的层
级**（一个是 LLM HTTP 调用重试，一个是 task 整体重试），不冲突。

数值差异（`core/constants.py`）：

| 常量 | 官方 env 默认值 | 本仓库值 | 差异 |
|---|---|---|---|
| 限频重试次数上限 | `SKILLFL_AGENT_429_RETRIES=20` | `MAX_RETRY_ATTEMPTS=20` | 一致 |
| 退避基础等待 | `SKILLFL_AGENT_429_BASE_SLEEP=30`s | `RETRY_BASE_SLEEP=5.0`s | 本仓库更短（6 倍） |
| 退避等待上限 | `SKILLFL_AGENT_429_MAX_SLEEP=600`s | `RETRY_MAX_SLEEP=300.0`s | 本仓库更短（一半） |

**未做任何修改**——这两个数值差异不在用户已批准的 Issue1-7 范围内（那批
只覆盖 agent/verifier timeout + task 级 retry + 隔离 + family 顺序），
是否需要对齐官方的 30s/600s，留待你确认。若不对齐，实际影响仅是"遇到
429 限流时重试间隔更短、更快耗尽 20 次重试"——偏保守方向（更快放弃而非
更慢），不会导致"隐藏性延长实验时间掩盖失败"这类你需要警惕的问题。

---

## 结论

- Agent timeout / Verifier timeout / task 级 retry policy / family 顺序
  透明化 / family 间隔离 —— 5 项均已在本会话更早阶段完成审计+修复+回归
  验证（286 passed, 2 skipped），本文档是该工作按本次任务书结构的重新
  汇总，**未新增任何代码改动**。
- 唯一遗留、本次新发现的小偏差是 §5 的 LLM 层限频退避秒数（30/600 vs
  5/300），已如实披露，未擅自调整，等待你确认是否需要对齐。
- Setting2-4 联邦场景的 client-phase retry（`federated.py`，默认仍为 2）
  按你此前的范围界定，本轮依旧未触碰。
