# 官方组件迁移审查（Part1）

> 对应用户 7-part 官方对齐任务的 Part1：审查官方仓库
> `FederatedSkill-main/FederatedSkill-main/skillfl/`，为 **task runner /
> Harbor execution bridge / verifier / task metadata parser / trajectory
> logging** 这五类"执行闭环"组件建立"官方文件 → 本仓库对应文件 → 是否已
> 迁移/对齐"的映射表。**明确排除**联邦算法本体（partitioning / merge /
> sync_schedule / aggregation）与 patch evolution 逻辑本体（本仓库的
> `server/planner.py`、`server/merge.py`、`experiments/aggregation.py`、
> `benchmark/family.py` 任务顺序、`evaluation/*.py` reward 定义均不在本次
> 迁移范围内，也未被本次审查触碰）——与 `docs/SIMPLIFICATIONS.md` §6 的既有
> 披露一致，本文件是对该既有披露的补充/细化，不重复其结论。

## 0. 审查方法与诚实性声明

- 本次审查直接读取了以下官方源码文件的完整内容：`skillfl/harbor_runner.py`、
  `skillfl/skillflow_adapter/harbor_bridge.py`、
  `skillfl/skillflow_adapter/_single_trial.py`、
  `skillfl/skillflow_adapter/worker_trial.py`、
  `skillfl/skillflow_adapter/patcher_bridge.py`、
  `skillfl/skillflow_adapter/runner.py`（cli.py/config.py 部分片段）。
- **关键发现**：官方 task runner 的核心执行体（"在容器里跑 agent、跑
  verifier、产出 result.json/ctrf.json"）并不在 `skillfl/` 这个 Python 包里，
  而是委托给外部 pip 依赖 `harbor`（`from harbor import Job` /
  `from harbor.models.job.config import JobConfig`）——这是 Laude Institute
  的 Terminal-Bench/Harbor 容器化执行框架，**其源码不在本仓库、也不在
  `FederatedSkill-main/` 里**，本次审查无法获取其内部实现，只能通过
  `skillfl/skillflow_adapter/` 这一层"胶水代码"侧面推断它的输入输出契约
  （`JobConfig` 输入、`TrialResult`/`ctrf.json`/`result.json` 输出）。
  这一点在 `docs/SIMPLIFICATIONS.md` §6.3/§6.4 已披露过（"课程环境不具备
  Harbor/Docker 依赖"），本文件不重复展开，只在下表中标注"委托给外部
  harbor 包，本仓库无法迁移其内部实现，只能对齐其契约"。
- 因此，本次"迁移"实际做的事是：**把官方胶水代码里可以看到的、且课程环境
  能够独立复现的协议/契约/顺序（execute → verify → 记录失败原因 →
  蒸馏）搬到本仓库对应的 Windows subprocess 执行层**，而不是安装 Docker/
  Harbor 本身。下表逐项列出。

## 1. Task Runner（单次 trial 执行入口）

| 官方文件 | 官方职责 | 本仓库对应文件 | 状态 |
|---|---|---|---|
| `skillfl/skillflow_adapter/_single_trial.py` | 单进程跑一个 `harbor.Job`（1 agent + 1 task），写 `result.json` | `harness/base_harness.py::BaseAgentHarness.run()`（模板方法：`initialize→execute_task→force_execute→verify→collect_trajectory→cleanup`） | ⚠️ 部分对齐：官方把"跑 agent"整体委托给 `harbor.Job.run()`（容器内部自己决定何时收尾），本仓库因为没有 Harbor，`execute_task()` 是真实 CLI 子进程调用（`claude`/`qwen-code`/`kimi` 二进制），**流程阶段划分（跑 agent→跑 verifier→写结果）已对齐**，但容器隔离机制不同（见 `docs/SIMPLIFICATIONS.md` §6.3/§6.4） |
| `skillfl/skillflow_adapter/harbor_bridge.py::HarborBridge.launch_trial()` | 每个 trial 起一个独立 OS 子进程跑 `_single_trial.py`（规避 asyncio 事件循环互相卡死），并处理 429 限流重试整个 trial | `executor/harness_executor.py::HarnessAwareExecutor`（按 `profile.agent_harness` 分派到对应 Harness 实例，无重试子系统） | ℹ️ 未迁移 429 整 trial 重试逻辑（课程环境的真实实验规模远小于论文规模，暂未观察到需要该重试策略；如需要可后续在 `harness/cli_harness_base.py` 补充，不属于本次 Part2-5 范围） |
| `skillfl/skillflow_adapter/worker_trial.py::WorkerTrialResult` | 单 trial 结果结构体（`worker_id, task_name, reward, verifier_passed, trial_dir, exception_type, exception_message, extra`） | `core/datatypes.py::Trajectory` | ✅ 语义对齐（字段更丰富）：`reward`↔`reward`，`verifier_passed`↔`reward>=1.0`（计算属性隐含），`exception_type/message`↔`exception_info`，另有官方没有的 `soft_reward/verifier_output/verifier_subtest_failures/generated_files/actions/execution_logs/failure_reason` 等扩展字段 |

## 2. Harbor Execution Bridge（容器化执行桥接）

| 官方文件 | 官方职责 | 本仓库对应文件 | 状态 |
|---|---|---|---|
| `skillfl/skillflow_adapter/harbor_bridge.py::HarborBridge._build_job_config_dict()` | 构造 harbor `JobConfig`：`SharedSkillsDockerEnvironment`（挂载共享技能目录到容器）、`force_build`（从 task 的 `environment/Dockerfile` 构建镜像） | `executor/environment.py::WorkspaceManager`（`subst` 盘模拟 `/root`，`copy_input_tree()` 按 `environment/Dockerfile` 的 `COPY` 指令解析目标路径） | ⚠️ **本次会话已验证**：`copy_input_tree()` 对 Dockerfile COPY 指令的解析逻辑与官方"以 Dockerfile 为准决定输入文件落位"的语义方向一致，但本仓库用 Windows `subst` 盘 + 文件系统拷贝模拟"容器挂载"，不是真正的容器隔离（无进程/网络/文件系统级沙箱），这是本仓库的架构级简化，已在 `docs/SIMPLIFICATIONS.md` §6.3 披露 |
| `skillfl/skillflow_adapter/harbor_bridge.py::DryRunHarborBridge` | 确定性假执行（无 docker/harbor/LLM），用于结构验证 | `run.py --mock`/`--dry-run` 模式（`harness/api_workspace_harness.py` 等） | ✅ 语义对齐（都是"无需真实基础设施的结构验证通道"） |
| （委托给外部 `harbor` 包内部）容器内 agent 执行完毕后"是否真的产出了 verifier 要求的文件"这一判定 | harbor 容器内部机制未知（黑盒，源码不可得） | **本次 Part2 新增**：`harness/base_harness.py::BaseAgentHarness._force_execute_solution()` | ✅ **本次新增能力，官方契约里没有直接对应物**——这是本仓库针对"真实 CLI agent 在 Windows subprocess 环境下会自称完成但从未真正执行/落盘产物"这一课程环境特有 bug 的独立设计（官方 Harbor 容器执行模型是否有类似强制步骤，因源码不可得，无法确认，因此不声称"对齐官方"，只声称"独立设计以解决课程环境观察到的真实 bug"，证据见 `results_real_family1/.../trajectory.json`） |

## 3. Verifier（校验器）

| 官方文件 | 官方职责 | 本仓库对应文件 | 状态 |
|---|---|---|---|
| （委托给外部 `harbor` 包）容器内运行 task 自带的 `tests/` 验证脚本，产出 `verifier/ctrf.json`（Common Test Report Format，命名子测试通过/失败）+ `verifier/test-stdout.txt` | 源码不可得（外部依赖） | `benchmark/verifier.py::SkillFlowScriptVerifier`/`FunctionTestVerifier` | ✅ 契约层面对齐：本仓库 verifier 同样跑 task 自带的 `tests/` 脚本（subprocess pytest），产出结构化 `subtest_failures`（命名列表）+ 原始 `verifier_output`（stdout/stderr），与官方 `ctrf.json`（命名子测试）+ `test-stdout.txt`（原始 pytest 输出）语义一一对应，已在 `docs/SIMPLIFICATIONS.md` §6.3 披露隔离机制不同 |
| `skillfl/skillflow_adapter/patcher_bridge.py::_compute_soft_reward()` | 从 `verifier/test-stdout.txt` 用正则 `([.F]{2,})\s*\[100%\]` 解析内层 pytest 进度条，算出子测试通过率作为 soft reward | `core/datatypes.py::Trajectory.soft_reward` + 相应解析逻辑（`executor/agent_executor.py`/`benchmark/verifier.py` 内） | ✅ 已对齐（此前会话已确认，见 repo 记忆"Runtime Fidelity"相关条目），非本次新增 |

## 4. Task Metadata Parser（任务元数据解析）

| 官方文件 | 官方职责 | 本仓库对应文件 | 状态 |
|---|---|---|---|
| `skillfl/skillflow_adapter/cli.py::resolve_family_tasks()` | 按 `test_tasks/<family>/<task>/task.toml` 是否存在筛选任务目录，按 family 的 ranking 文件排序 | `benchmark/skillflow_adapter/loader.py::load_skillflow_family()` | ✅ 已对齐（此前会话已实现：同样按 `task.toml` 存在性筛选 + family 顺序加载，非本次新增） |
| `task.toml` 本身的字段 schema（由外部 `harbor`/terminal-bench 定义，`skillfl/` 只是"存在性探测"，不解析具体字段） | 源码不可得（外部依赖） | `benchmark/task.py::Task`/`VerificationSpec` | ℹ️ 本仓库自行定义了 `Task`/`VerificationSpec` 的字段集合来承载 `task.toml`+`environment/`+`tests/` 里的信息，因为官方 `task.toml` 的正式 schema 定义在外部 `harbor`/terminal-bench 里，本仓库无法获取，只能按实际观察到的 `test_tasks/` 目录内容反向设计字段——已按需扩展 `Task.metadata`（如 `solution_filename`），未改变已有字段含义 |

## 5. Trajectory Logging（轨迹记录）

| 官方文件 | 官方职责 | 本仓库对应文件 | 状态 |
|---|---|---|---|
| （外部 `harbor` 包）容器内 agent 的原始对话记录，标准化为 `ensure_standard_trajectory(trial_dir)` 能找到的格式 | 源码不可得 | `harness/cli_harness_base.py::collect_trajectory()` + `executor/trajectory.py::TrajectoryCollector` | ✅ 契约层面对齐（都是"把 agent 原始交互序列化为可被蒸馏器消费的标准结构"） |
| `libs.skill_evolution.patcher::TrajectoryCompactor.extract_trial_outcome()`（官方 patcher 内部，`skillfl/skillflow_adapter/patcher_bridge.py` 调用它） | 从 `trajectory_path` + `trial_result.json` + `verifier_ctr`（ctrf.json）三份材料里提取 `TrialOutcome`（供 LLM 蒸馏 prompt 使用），而不是从 agent 聊天文本猜测失败原因 | **本次 Part3/Part5 新增**：`core/datatypes.py::Trajectory.execution_logs`/`failure_reason`（`_derive_failure_reason()`）+ `TrialOutcome.verifier_feedback`/`failure_reason`（`client/distiller.py::_step3_outcome()` 透传，`llm/prompt_builder.py::_section_trial_outcome()` 展示） | ✅ **设计思路已核实与官方一致**：官方 `TrajectoryCompactor.extract_trial_outcome()` 同样是"结构化材料（trajectory + result.json + ctrf.json）驱动，而不是聊天文本驱动"，本次新增的 `_derive_failure_reason()`（优先级：`exception_info` > 命名子测试失败列表 > 原始 verifier 输出 > 兜底提示，**显式排除 `final_message`**）与官方"吃 ctrf.json 结构化子测试信息，而不是吃 agent 自己的话"这一设计原则同源，只是本仓库没有 `ctrf.json`（用 `verifier_subtest_failures` 列表代替），因此不是逐字节复制官方 `TrajectoryCompactor` 源码，而是遵循同一原则的独立实现（未复制官方 `.py` 源码，符合课程"不直接复制算法源码"的约束） |
| `skillfl/skillflow_adapter/patcher_bridge.py::PatcherBridge.generate()` 里 "agent 未产出可用 trajectory 时返回空 patch，不当成异常吞掉" 的防御式判断 | 见上 | `client/distiller.py::PatchDistiller.distill()` 现有降级路径（`docs/SIMPLIFICATIONS.md` §4.1/§4.3 已披露） | ✅ 已对齐，非本次新增 |

## 6. 本次 Part2-5 新增/修改代码清单（供交叉核对）

以下均已跑 `pytest -q` 全量回归确认无新增失败（332 passed / 1 个既有已知无关失败 / 2 skipped）：

- `core/datatypes.py`：`Trajectory.execution_logs`、`Trajectory.failure_reason`
  （+ `_derive_failure_reason()`，扩展 `_sync_derived_fields()`）、
  `TrialOutcome.verifier_feedback`、`TrialOutcome.failure_reason`。
- `harness/base_harness.py`：`HarnessExecutionResult.forced_execution` 字段、
  `BaseAgentHarness._force_execute_solution()`、`run()` 模板方法接入强制执行步骤。
- `executor/trajectory.py`：`TrajectoryCollector.add_execution_log()`，
  `finalize()` 接入 `execution_logs`。
- `harness/cli_harness_base.py`：`collect_trajectory()` 接入
  `add_execution_log()`；`CLIAgentHarnessBase._VERIFICATION_DISCIPLINE_BLOCK`
  + `_build_workspace_prompt()` 接入"结束前必须完成的校验"指令块。
- `client/distiller.py`：`_step3_outcome()` 透传 `verifier_feedback`/
  `failure_reason`。
- `llm/prompt_builder.py`：`_section_trial_outcome()` 独立展示
  failure_reason/verifier_feedback（排在 agent chat 文本之前）；
  `_section_instructions()` 失败分支要求生成的 SKILL.md 含
  `## Failure Cause`/`## Future Prevention Rule`/`## Verification Procedure`
  三段。
- `tests/test_official_alignment_execution_layer.py`：14 个新测试，覆盖以上全部改动。

## 7. 明确未迁移/不属于本次范围的部分（Part6 约束的直接体现）

- `skillfl/skillflow_adapter/merge.py`（联邦合并算法）——**未读取其算法细节，
  未触碰本仓库 `server/merge.py`**。
- `skillfl/skillflow_adapter/partitioning.py`/`sync_schedule.py`（任务分片/
  同步调度）——**未触碰本仓库 `experiments/federated.py`/`aggregation.py`**。
- `skillfl/skillflow_adapter/patcher_bridge.py::PatcherBridge.generate()` 内部
  调用的 `SkillPatchEvolver.generate_patch()`（patch evolution 算法本体）——
  **未读取其内部 prompt/算法逻辑，未触碰本仓库 `server/planner.py`/
  `client/distiller.py` 的蒸馏 prompt 策略部分（仅新增了"喂给它更真实的
  verifier 材料"，未改变其决策算法本身）**。
- family 任务顺序（`benchmark/family.py`）、reward 定义
  （`evaluation/*.py`/`benchmark/verifier.py` 的 reward 计算公式）——
  **完全未改动**。
