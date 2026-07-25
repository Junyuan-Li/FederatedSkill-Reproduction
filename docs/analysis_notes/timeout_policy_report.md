# Task Execution Timeout Audit — timeout_policy_report.md

对应本次任务书 "Task Execution Timeout Audit" 章节。

> **状态更新**：本报告最初版本（见下方第 1-4 节 + "结论"）是修改代码前的
> 只读审计。用户已明确批准"方案 A"并逐条给出 Issue1-7 的修复范围（仅对齐
> runtime protocol：timeout/retry/parser bug/隔离/family 顺序，**不改动**
> algorithm/federation/skill evolution/aggregation/benchmark tasks/
> evaluation metrics），以下 Issue1-7 修复**已经落地到代码**（非仅提议）。
> 按用户要求新增本节，覆盖必须包含的 5 项：
> ①官方 timeout 来源 ②当前 timeout（修改前）③修改后 timeout ④retry policy
> 对比 ⑤benchmark 兼容性。第 1-4 节原始审计内容保留在下方作为证据支撑，
> 未删改。**本次修复后已 STOP，未启动任何新实验**（详见文末"验证"一节）。

---

## 0. Issue1-7 修复摘要（用户已批准，已落地）

### 0.1 官方 timeout 来源

SkillFlow-Task 数据集每个任务自带 `task.toml`，含 3 个独立嵌套 section：
`[agent] timeout_sec` / `[verifier] timeout_sec` /
`[environment] timeout_sec`（部分 family 用 `build_timeout_sec` 别名，抽样
可见，详见下方 §2 表格）。官方 `paper_logs/*/config.json` 记录
`"agent_timeout_multiplier": 1.0`，即官方实验对 `[agent] timeout_sec`
原样生效、未缩短；同一 `config.json` 的 `"retry"` 块记录
`"max_retries": 0`，且显式把 `AgentTimeoutError`/`VerifierTimeoutError` 等
排除在可重试异常之外。这是本次所有修复的唯一依据来源。

### 0.2 当前 timeout（修改前，即本报告最初版本记录的状态）

| 项 | 修改前 |
|---|---|
| agent 执行超时（CLI 子进程 wall-clock） | 固定 600s（`configs/runtime.yaml::real_experiment.timeout`），与 task.toml 完全无关 |
| verifier 执行超时 | parser 只找顶层 `timeout_seconds`/`timeout` 键（task.toml 实际是嵌套 section，永远找不到），静默 fallback 到硬编码 `30`；`converter.py` 再对该值做 `min(max(x,1),300)` clamp |
| retry policy | `experiments/configs/setting_se*.yaml::max_retry: 2`（最多 3 次 attempt），不区分异常类型，超时也无条件重试；耗尽后向上 `raise`，整个 family 因单个 task 失败而中断 |
| family 顺序 | `_run_family_loop()` 内部已是 `sorted(families.keys())`，但未在 `experiment_summary.json` 里记录这一选择的依据 |

### 0.3 修改后 timeout（已落地）

| 项 | 修改后 |
|---|---|
| agent 执行超时 | 优先读取该 task 的 `task.toml [agent] timeout_sec`（经 `benchmark/skillflow_adapter/parser.py` → `converter.py` 写入 `Task.metadata["agent_timeout_seconds"]`），由 `harness/cli_harness_base.py::execute_task()` 取 `effective_timeout = task.metadata.get("agent_timeout_seconds") or self.default_timeout`；`configs/runtime.yaml::real_experiment.timeout` 从 600 改为 **1800**（仅在 task 未携带该字段时作为兜底默认值，抽样官方值恒为 1800，两者现已一致）。`sanity` 模式的 120s 保持不变（用于快速冒烟，非 real_experiment）。 |
| verifier 执行超时 | `parser.py` 新增 `_section_timeout_sec()`，按 `[verifier].timeout_sec` → 找不到才用默认值 900.0 读取（不再误读顶层键）；`converter.py::convert_tests()` 去掉 `min(max(x,1),300)` 人为上限，直接使用解析到的真实值；`benchmark/task.py::VerificationSpec.timeout_seconds` 的 Pydantic 约束从 `le=300` 放宽到 `le=3600`（仍是有限上界，非无限超时，防止脏数据导致挂起）。`[environment]` 段同理新增 `environment_timeout_seconds`（按 `timeout_sec`/`build_timeout_sec` 两个别名尝试），目前解析结果保留在 `RawSkillFlowTask` 上供后续需要时使用，不影响本轮范围外的其它逻辑。 |
| retry policy | `experiments/baseline.py::SelfEvolutionRunner` 的 `max_retry` 默认值 2→0；`experiments/configs/setting_se.yaml`、`setting_se_family.yaml` 的 `max_retry: 2` 均改为 `max_retry: 0`；`experiments/run_experiment.py` 里 `cfg.get("max_retry", 2)` 的兜底默认值也改为 `0`。`_run_client_trial()` 重写：耗尽 retry 后**不再向上 raise 中断整个 family**，而是把该 task 记为 `reward=0.0` 的 `TrialSnapshot`，继续跑下一个 task（`PatchDistillationFailure` 例外——这是"补丁蒸馏失败"这一独立失败类别，不属于 Issue2 定义的"超时/执行失败"，仍按既有 strict 模式向上抛出终止实验，未改变）。 |
| family 顺序 | `experiment_summary.json` 新增 `"family_order": "sorted_by_name"`、`"family_order_reason": "official_order_unavailable"`、`"family_ids_in_order": [...]` 三个键，如实记录排序依据（官方顺序不可得，代码用确定性的按名排序代替随机）。 |
| 实验隔离 | 复用上一阶段已实现的 `family_failure_cleanup()`（每个 family 开始前清空技能库/工作区，某个 family 因非预期异常中断后清理临时产物、继续下一个 family）；本轮起，绝大多数"task 执行失败"已经不再触发这条中断路径（因为 Issue2 把它们变成了 reward=0 的正常继续），`family_failure_cleanup()` 仅在真正意外的异常（如 `PatchDistillationFailure`、sampler/记账层 bug）时兜底触发，职责范围收窄但未被移除。 |
| 旧实验 | 检查此前用 `timeout=600`/`max_retry>0` 跑的后台实验终端（ID `7034d189-...`），核实时该终端已不存在（进程已结束或会话已重启），无需再手动终止；其 `results/` 产物**不得**用于最终结论（如仍存在，需在后续报告里明确标注"不合规、仅供参照，不代表官方 protocol 下的结果"）。 |

### 0.4 retry policy 对比

| | 官方（`paper_logs/*/config.json`） | 本仓库修改前 | 本仓库修改后 |
|---|---|---|---|
| `max_retries` | 0 | 2（`setting_se*.yaml`） | 0 |
| 超时/执行失败是否重试 | 否，且显式排除 `AgentTimeoutError`/`VerifierTimeoutError` 等 | 是，且不区分异常类型 | 否（`max_retry=0` 时循环只跑 1 次 attempt，效果等价） |
| 失败后果 | 该 task 失败，继续下一个 task/family | 耗尽 2 次重试后向上 raise，整个 family 因单个 task 失败而中断 | 记为 `reward=0.0`，继续下一个 task/family（`PatchDistillationFailure` 除外，仍中断） |
| 已知遗留 scoping | — | — | `experiments/federated.py::_run_client_phase_with_retry()`（Setting 2-4 联邦场景）本轮**未改动**，其 `max_retry` 默认仍为 2、失败仍向上传播——因为改动这里会牵涉服务器端多 client 补丁聚合的时序/语义，属于用户明确禁止触碰的"federation logic/aggregation"范畴，留待 Phase2 联邦场景审计时再决定是否对齐 |

### 0.5 benchmark 兼容性

- 以上改动全部落在"runtime protocol 对齐"范围：超时读取来源、retry 次数、
  实验隔离、family 顺序记录——**未触碰** `benchmark/skillflow_benchmark.py`
  / `benchmark/verifier.py` 的验证逻辑本身、`skillfl/` 联邦聚合算法、
  技能演化/蒸馏算法，也未改动任何具体 task 的定义或评测指标计算方式。
- `VerificationSpec.timeout_seconds` 的上界从 300 放宽到 3600 属于 Pydantic
  schema 约束调整，不改变验证脚本本身的执行逻辑，只是不再人为提前掐断官方
  数据集里合法的 900-1200s 长验证任务。
- 现在 agent/verifier 超时会随 task.toml 逐 family/逐任务变化（不再是全仓库
  统一常量），与官方"抽样 1800/900/1200 s 因 family 而异"的观察结果一致，
  兼容性由此前不一致变为一致。
- 影响面已做回归测试评估（详见文末"验证"一节），仅 1 处测试断言因字段改名
  需要同步更新（`tests/test_skillflow_adapter.py::test_parse_task_dir`），
  其余测试经排查确认不受影响。

---

范围：`executor timeout` / `CLI timeout` / `agent timeout` / `retry policy` 四项，
证据来源仅限：(1) 论文正文（本环境无法获取全文，见下方说明）、(2) 官方仓库
`FederatedSkill-main/FederatedSkill-main/`（含 `skillfl/skillflow_adapter/` 代码 +
`paper_logs/` 真实已发表实验的运行记录 + benchmark 数据集
`test_tasks/<family>/<task>/task.toml`）、(3) 本仓库
`FederatedSkill-Reproduction/` 当前默认配置。

---

## 1. 论文是否定义 task timeout？

**无法从论文正文核实**：本会话环境没有论文 PDF 本地副本，`fetch_webpage` 抓取
`arxiv.org/abs/2606.03143` / `arxiv.org/pdf/2606.03143` 均失败（"Failed to
extract meaningful content"）。这不是"论文没有规定"的结论，而是"当前环境
读不到论文原文，无法从论文文字直接回答"——如实标注，不假装已核实。

唯一可核实的是 **benchmark 数据集本身**（SkillFlow-Task，通过
`benchmark/families/*.json` + 缓存的 `test_tasks/**/task.toml` 落地）和
**官方仓库代码/真实运行记录**，两者均对 timeout 有明确定义（见下）。

---

## 2. 官方代码/官方数据集是否定义 timeout？

**是，而且是数据集自带的、逐 task 可能不同的字段**（不是论文文字描述，是
SkillFlow-Task 数据集本身的固有属性，随每个任务一起分发）。

抽样核实 3 个候选 family 各 1 个任务的 `task.toml`：

| Family | `[agent] timeout_sec` | `[verifier] timeout_sec` | `[environment] build_timeout_sec` |
|---|---|---|---|
| Compensation-Scenario-Modeling / `01_orchestra_foundation_model` | 1800.0 | 900.0 | 600.0 |
| Cross-Format-Data-Reconciliation / `01-cloud-service-portfolio-diff` | 1800.0 | 900.0 | 600.0 |
| DMAIC-Quality-Analysis / `harbor_soc_alert_analyze_01` | 1800.0 | **1200.0** | **900.0** |

**agent timeout 抽样恒为 1800s，但 verifier/environment 两项在 family 间不同**
（DMAIC 明显更高）——说明这是逐 task 可配置的数据字段，不能假设全仓库只有
一个固定常量，必须逐 task 读取。

官方仓库真实已发表实验的运行记录进一步印证——
`FederatedSkill-main/FederatedSkill-main/paper_logs/4_fed_hetero_mixed_cli/qwen3.6-plus/DMAIC-Quality-Analysis/round_000/worker_0/config.json`：
```
"agent_timeout_multiplier": 1.0,
```
即官方实验直接对 task.toml 的 `[agent] timeout_sec` 乘以 multiplier=1.0，
**原样生效（1800s），未做任何缩短**。

---

## 3. timeout 作用对象是什么？

核实确认存在 **3 个相互独立的作用对象层级**：

| 层级 | 作用对象 | 官方值来源 | 本仓库当前实现 |
|---|---|---|---|
| A. single LLM call timeout | 单次 HTTP 请求（仅 `execution_mode="api"` 时适用；`execution_mode="cli"` 下 LLM 调用被封装进 CLI 子进程内部，不走这一层） | 官方仓库未见独立配置此层（CLI 子进程自行管理） | `llm/backbone.py::request_timeout_seconds=300.0`（硬编码默认值） |
| B. agent trajectory timeout | 一个 task 的完整 agent 执行（CLI 子进程从启动到给出最终答案的整个 wall-clock 时长） | task.toml `[agent] timeout_sec`（抽样恒为 1800.0），`config.json::agent_timeout_multiplier=1.0` 原样生效 | `harness/cli_harness_base.py::default_timeout`，来自 `configs/runtime.yaml::real_experiment.timeout: 600`（**与 benchmark 数据完全无关的仓库工程常量**） |
| （B'）verifier execution timeout | 验证脚本的独立执行超时（区别于 agent 本身） | task.toml `[verifier] timeout_sec`（900.0-1200.0，逐 family 不同） | 见下方"偏差2"——当前实现存在 parser bug，实际恒为 30s |
| C. whole experiment timeout | 整个多 family 实验循环的全局超时 | 官方代码/`paper_logs` 未发现任何 family-loop / 整实验级别的全局超时字段 | 本仓库同样**未实现**任何全局实验超时——这一层两边一致（都不存在），无偏差 |

---

## 4. 当前实现是否一致？

**不一致，发现 2 处需要你确认的偏差 + 1 处附带发现的 retry policy 偏差**，均已找到
论文/官方仓库/默认配置层面的依据，未擅自修改任何代码。

### 偏差 1（关键，很可能是本次 Phase1 sanity 反复超时的根因）

agent 执行超时被写死为本仓库工程常量 **600s**
（`configs/runtime.yaml::real_experiment.timeout: 600` →
`harness/cli_harness_base.py::default_timeout`），**完全没有读取
task.toml 的 `[agent] timeout_sec` 字段**（抽样恒为 1800s，官方
`agent_timeout_multiplier=1.0` 原样使用）。

之前两次真实 Phase1 sanity 运行里，`Compensation-Scenario-Modeling` 的
`01_orchestra_foundation_model` 均在 **600.5s** 精确触发
`timeout_reason=wall_clock_timeout_600.0s` 失败——现在看，这很可能不是
模型/agent 能力不足，而是我们用的超时阈值只有官方数据集意图值的 1/3。

**这不属于"为了提高通过率放宽 timeout"**（你明确禁止的事）——恰恰相反，
是发现当前实现使用了一个与 benchmark 数据无关、来源不明的仓库常量，
本该读取 task 自带的 `timeout_sec` 字段。按你的规则，这是需要先给出
论文/官方仓库/默认配置依据、再决定是否调整的工程参数（依据已如上）。

### 偏差 2：verifier/environment 超时同样未逐 task 读取，且被人为砍到 300s 上限

- `benchmark/skillflow_adapter/parser.py:123`：
  ```python
  timeout_seconds = int(raw_toml.get("timeout_seconds", raw_toml.get("timeout", 30)))
  ```
  查找的是 TOML **顶层** `timeout_seconds`/`timeout` 键，但 task.toml 实际
  结构是嵌套的 `[verifier]`/`[agent]`/`[environment]` 三个 section，顶层
  根本没有这两个键——这段代码永远匹配不到，静默 fallback 到硬编码的 `30`。
- `benchmark/skillflow_adapter/converter.py:64`：
  ```python
  timeout_seconds=min(max(raw.timeout_seconds, 1), 300)
  ```
  即使 parser 修好后能正确读到 900-1200，这里也会被强行砍到 300s 上限。
- 影响：`benchmark/verifier.py::_run_script(full_script, spec.timeout_seconds)`
  的验证脚本超时目前恒为 30s，远低于官方 900-1200s，验证脚本较重的任务
  可能被误判超时失败（reward 被压低，但失败原因其实是"验证超时"而不是
  "任务真的没做对"——正是你要求的 Failure Classification 需要区分的场景）。

### 附带发现：retry policy 偏差（超出 timeout 范畴，但属于本次"检查 retry
policy"的要求）

官方 `paper_logs/4_fed_hetero_mixed_cli/*/config.json`：
```json
"retry": {
    "max_retries": 0,
    "exclude_exceptions": [
        "VerifierTimeoutError", "AgentTimeoutError",
        "RewardFileEmptyError", "VerifierOutputParseError",
        "RewardFileNotFoundError"
    ]
}
```
官方实验 **超时/验证失败不重试**（`max_retries=0`，且显式把
`AgentTimeoutError`/`VerifierTimeoutError` 排除在可重试异常之外）。

本仓库 `experiments/configs/setting_se.yaml::max_retry: 2`（最多 3 次
attempt），且不区分异常类型——超时也会被无条件重试。这是仓库既有配置
（非本次新增），如实披露：**当前 retry policy 本来就比官方更宽松**，
与你"禁止自行增加 retry 次数"的约束方向一致（不会再增加），但尚未对齐
官方的"到 0"。

---

## 结论：在你确认前不会修改代码/不会启动新实验

三处偏差都已找到论文数据集/官方仓库真实运行记录/本仓库默认配置三方证据，
按你的规则，属于"可以在你确认后调整"的工程参数（不是算法/benchmark/
harness 层面的改动），但改不改、怎么改，交由你决定：

- **方案 A（推荐，最贴近官方）**：
  1. 修 `benchmark/skillflow_adapter/parser.py`，从 `[agent]`/`[verifier]`/
     `[environment]` 三个 section 正确读取三个独立字段（而不是找不存在的
     顶层键）；
  2. 去掉 `converter.py` 里 `min(max(x,1),300)` 的人为 clamp；
  3. `harness/cli_harness_base.py` 改为优先使用 task 自带的
     `[agent] timeout_sec`（`configs/runtime.yaml` 的 600s 仅作为字段缺失
     时的 fallback）；
  4. `max_retry` 对齐官方改为 0，或至少让超时类异常不重试。
  以上都是"修 bug 使其匹配官方数据/官方配置"，不是"放宽 timeout 提高通过率"。
- **方案 B（保守，本次先不改代码）**：Phase1 sanity 继续用当前
  600s/max_retry=2 跑，但 `phase1_analysis.md` 必须明确标注"本次实验条件
  的 timeout/retry 与官方不同，结果仅用于验证链路是否跑通，不可与论文
  Table 1 数字直接比较"。

在你选定方案之前，本报告之外未修改任何代码，也未启动任何新实验。

---

## 验证（方案 A / Issue1-7 落地后，本次新增）

- 全量 `python -m pytest -q`：修复过程中发现 1 处真实回归——
  `harness/kimi_cli_harness.py::KimiCLIHarness._validate_cli_result()` 是
  基类同名方法的 override，忘记同步基类新增的 `effective_timeout` 形参，
  导致 3 个 kimi harness 测试报 `TypeError`；已修复该 override 签名。
  修复后重跑：**286 passed, 2 skipped**，与本次改动前的基线完全一致，无回归。
- Issue7：核实此前非合规的后台实验终端（ID `7034d189-...`，
  `timeout=600`/`max_retry=2`）已不存在（已结束或会话已重启），无需手动
  终止；其 `results/phase1_sanity_se3/` 产物若仍在磁盘上，**不得**用于
  最终结论（不符合新协议）。
- **STOP**：按你的要求，本次只完成"代码修复 + 报告更新"，未启动任何新
  实验。下一步需要你确认后，才会输出正式 Experiment Plan（Experiment
  ID/Setting/Paper Section/Families/Tasks per family/Rounds/Model/
  Harness/Timeout policy/Retry policy/Expected artifact）并再次等待确认
  后启动 Phase1 sanity 重跑。
