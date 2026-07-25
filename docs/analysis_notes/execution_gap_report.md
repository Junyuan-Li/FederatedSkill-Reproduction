# FederatedSkill 执行链差异报告

## 1. 审计范围

本报告对应 Phase 1，仅审计当前 Setting1 真实 CLI 执行链，不修改代码、不运行
实验。检查范围：

- `experiments/run_experiment.py`
- `experiments/baseline.py`
- `executor/`
- `harness/`
- `benchmark/`
- `benchmark/skillflow_adapter/`
- 官方 `skillfl/skillflow_adapter/` 与 Harbor 接入代码

结论适用于 `execution_mode="cli"`、`agent_harness="claude-code"` 的当前
Setting1 路径。API/debug 路径不是本次验证重跑的目标。

## 2. 当前实际执行流程

```text
官方 family JSON 中的 Task
  │
  ├─ FamilyCurriculumSampler 按 difficulty/ranking 取本轮 task
  │
  ├─ SelfEvolutionRunner._run_client_trial()
  │    └─ HarnessAwareExecutor.run()
  │         └─ ClaudeCodeHarness（strict CLI mode）
  │
  ├─ CLIAgentHarnessBase.initialize()
  │    ├─ tempfile.mkdtemp() 创建 Windows 临时目录
  │    ├─ 从 source_environment_dir 复制完整 environment/ 文件树
  │    └─ 记录初始文件路径快照
  │
  ├─ CLIAgentHarnessBase.execute_task()
  │    ├─ 从当前 family 技能库检索技能
  │    ├─ 构造原有 system/user prompt
  │    ├─ 以 cwd=临时目录启动 Claude Code CLI
  │    ├─ CLI 在工作区内读写文件
  │    └─ 工作区 diff 收集 generated files
  │
  ├─ BaseAgentHarness.run()
  │    └─ AgentWorkspaceExecutor._verify()
  │         ├─ 把拼接后的官方 tests 写为 _verify_test_script.py
  │         ├─ 以 cwd=临时目录启动本机 Python subprocess
  │         └─ 退出码 0 => reward=1.0，否则 reward=0.0
  │
  ├─ CLIAgentHarnessBase.collect_trajectory()
  │    └─ 记录 CLI 输出、generated file 名称、verifier 输出和 reward
  │
  ├─ CLIAgentHarnessBase.cleanup()
  │    └─ 删除临时工作区
  │
  └─ SelfEvolutionRunner
       ├─ 保存 trajectory.json、reward.json
       ├─ PatchDistiller 蒸馏并应用 skill patch
       └─ 保存 patch.json、task_status.json
```

### 2.1 入口与路由

`experiments/run_experiment.py::_build_executor()` 在 `execution_mode="cli"`
时构造 `HarnessAwareExecutor(mode="strict")`。后者按
`WorkerProfile.agent_harness` 选择 `ClaudeCodeHarness`。因此当前 Phase A/后续
Setting1 CLI 验证不经过 `VerificationAwareExecutor` 或
`SkillFlowTaskExecutor`。

### 2.2 工作区创建

`CLIAgentHarnessBase.initialize()` 每次调用都会创建新的系统临时目录，并从
`Task.metadata["source_environment_dir"]` 复制官方 `environment/` 的完整文件树。
这已经提供了“不同 trial 使用不同临时目录”的基础隔离。

任务 instruction 不作为工作区文件写入，而是通过原有 agent prompt 传给 CLI。
`environment/` 中的 Dockerfile 也会被复制，但不会被构建或执行。

### 2.3 Agent 执行

Claude Code CLI 的 `cwd` 是本次临时工作区。agent timeout 优先读取
`Task.metadata["agent_timeout_seconds"]`，缺失时回退到
`configs/runtime.yaml`，其代码级默认值仍为 600 秒。

CLI 执行完成后，以初始化前后的相对文件路径集合差值识别 generated files。
对二进制文件，trajectory 只内联一个大小标记，实际文件仍留在临时工作区直到
cleanup。

### 2.4 Verifier 与 reward

`BaseAgentHarness.run()` 在同一个临时工作区调用
`AgentWorkspaceExecutor._verify()`。对于 `skillflow_script`，当前实现把 `tests/`
下多个 Python 文件预先拼成一个脚本，再用宿主机 Python 执行。verifier timeout
来自 `VerificationSpec.timeout_seconds`。

reward 口径没有被另行修改：验证 subprocess 返回码为 0 时是 1.0，否则是 0.0。

### 2.5 产物与清理

当前仅将结构化 trajectory、reward、patch 和 status 写入：

```text
results/<experiment>/families/<family>/workers/<worker>/tasks/
  round_<index>_<task_id>/
```

临时 workspace 在 `finally` 中删除。删除前没有持久化完整 workspace、workspace
manifest 或 generated file 实体副本。因此当前结果不能事后证明 verifier 看到的
完整文件系统状态。

## 3. 官方 Harbor task lifecycle

官方实现为每个 `(worker, task)` 构造一个 Harbor `Job`，并把单个 trial 放入独立
Python 进程。其主要生命周期为：

```text
官方 task 目录
  │
  ├─ Harbor JobConfig 注册显式 task path
  ├─ 读取 task.toml 和 environment/Dockerfile
  ├─ force_build=True 构建/复用该 task 的容器镜像
  ├─ 创建新的 trial/container
  ├─ 把对应 family skill 目录挂载到 agent skill 路径
  ├─ 在容器 /root 中运行 agent
  ├─ 在同一容器任务环境中运行官方 verifier
  ├─ 从 Harbor verifier_result.rewards 读取 reward
  └─ 保存 Harbor trial artifacts；容器生命周期由 Harbor 管理
```

官方 task 的 Dockerfile 控制输入文件位于 `/root`、系统依赖、工作目录等环境
语义。`task.toml` 的 `[environment]` 控制资源和镜像信息；Harbor/SkillFlow 负责
解释 task metadata，而不是先转换为本地简化 schema 后再执行。

## 4. 差异与风险

### G1（关键）：绝对 `/root` 路径可逃逸临时工作区

官方 task instruction、solution 和 tests 大量使用 `/root/...`。在官方 Linux
容器中，它们都指向本 trial 的隔离根目录。在 Windows 本地 subprocess 中，
`/root/...` 会解析为当前盘符根目录下的 `\root\...`，不属于
`WorkspaceManager` 创建的临时目录。

本机已观察到 `C:\root` 中同时存在多个任务的输入/输出文件。当前第一轮
Compensation trajectory 也生成了 `/root/Orchestra_Compensation.xlsx`。因此：

- agent 或 verifier 可以读取以前 task/family 留下的文件；
- 当前 workspace diff 无法记录逃逸到 `C:\root` 的真实文件；
- 删除临时 workspace 不会删除这些文件；
- reward 可能依赖跨 trial 残留，不能据此证明 clean execution。

这是当前 Setting1 reward 有效性的首要阻断项。

### G2（高）：没有可审计的 workspace snapshot

现有 `WorkspaceManager` 确实为每次调用创建新的临时目录并在 finally 中清理，
但只把 generated file 名称写进 trajectory。没有保存：

- 执行前 manifest；
- verifier 前的完整 workspace snapshot；
- generated file 实体；
- 文件大小、哈希和来源分类；
- workspace 是否位于预期隔离根目录的证据。

因此即使某轮通过，也无法仅从 results 重建 verifier 的输入状态。

### G3（高）：官方 environment 语义只被部分保留

当前 parser 会读取 `[environment]` 的 timeout，并把完整 `raw_toml` 放进 metadata，
也会复制 `environment/` 文件树。但是 `converter.to_task()` 没有把
`environment_timeout_seconds` 放入可执行 metadata，CLI 路径也不解释以下字段：

- `docker_image`
- `cpus`
- `memory_mb`
- `storage_mb`
- Dockerfile 中安装的系统/语言依赖
- Dockerfile 的 `WORKDIR /root` 和 `COPY ... /root/...` 语义

在“不引入 Docker”的限制下不能完全复制这些语义，但必须明确哪些字段未生效，
不能把“文件已复制”表述为“environment 已对齐”。

### G4（高）：timeout 缺失时仍静默 fallback

当前键路径解析本身正确：

- `[agent].timeout_sec` -> `RawSkillFlowTask.agent_timeout_seconds`
- `[verifier].timeout_sec` -> `VerificationSpec.timeout_seconds`
- `[environment].timeout_sec/build_timeout_sec` ->
  `RawSkillFlowTask.environment_timeout_seconds`

并且 verifier 不再使用旧的 300 秒 clamp。但是 parser 在字段缺失时直接返回
1800/900/600，CLI 又允许 agent timeout 回退到 runtime 配置/600 秒，均没有显式
warning。调用方无法区分“官方 task.toml 明确给值”与“本地默认值”。

此外 `VerificationSpec.timeout_seconds` 仍有 3600 秒 schema 上限；如果官方任务
将来声明更大的 verifier timeout，加载会失败而不是原样保留。

### G5（中）：tests 被拼接后使用宿主机 Python 执行

官方 verifier 在 task 容器环境中运行，能够使用 Dockerfile 安装的依赖和 Linux
路径。当前 parser 将 `tests/` 下所有 `.py` 按文件名拼接为单个脚本，本机 Python
直接执行。它可能改变：

- 测试文件的 `__file__` 与相对目录关系；
- `/tests/...` 辅助文件访问；
- shell wrapper 的 setup/teardown；
- Linux 命令和 task-specific dependency 可用性。

reward 公式仍是二值退出码，但产生该退出码的环境与官方不同。

### G6（中）：retry 默认值已对齐，但异常分类未对齐

Setting1 配置和 `SelfEvolutionRunner` 当前默认 `max_retry=0`，默认情况下确实只
执行一次。若配置显式设置正数，`_run_client_trial()` 会捕获除
`PatchDistillationFailure` 外的所有 `Exception` 并统一重试，没有区分：

- API unavailable；
- process crash before execution；
- agent timeout；
- task execution failure；
- verifier failure。

因此“仅基础设施失败可重试”的规则尚未实现。需要注意：正常 verifier
`reward=0` 作为成功返回的 trajectory 不会触发异常重试；问题主要发生在 timeout
或执行器抛异常时。

### G7（低）：当前 checkpoint layout 不满足下一阶段产物要求

现有 task checkpoint 嵌套在 worker/round 路径下，没有
`results/{experiment}/{family}/{task_id}/workspace/`、`workspace_snapshot/` 或
`generated_files/`。这不改变 reward，但不满足后续隔离审计与验证报告要求。

## 5. 已对齐部分

- 仅选择官方 family 时，任务内容来自官方 SkillFlow benchmark。
- family 内顺序由 `ALL_TASK_DIFFICULTY_RANKING.json` 解析为 1..N。
- 每个 family 的技能库初始化为空，并有残留断言。
- Setting1 使用一个 worker，无 server/Stage1/Stage2。
- CLI 的 cwd 是每次新建的临时 workspace。
- 完整 `environment/` 输入文件树会复制到 workspace，包括二进制文件。
- agent 和 verifier timeout 的官方嵌套键路径已经接通。
- verifier 不再使用 300 秒 clamp。
- Setting1 默认 `max_retry=0`。
- reward 仍按官方测试成功/失败采用二值口径，没有改评价指标。

## 6. Phase 2-4 的最小修改边界

后续实现应只触及隔离、metadata 传播和 retry 判定，不改变 prompt、模型、harness
选择、技能蒸馏或评价指标。

### Phase 2 建议边界

1. 在每次 trial 创建唯一、受控的临时执行根目录。
2. 为 SkillFlow 的 `/root` 语义提供 trial-local 映射，确保绝对路径不能落到共享
   `C:\root`。
3. verifier 完成后、cleanup 前，把 workspace snapshot、generated files 和 manifest
   复制到 task checkpoint。
4. manifest 至少记录相对路径、文件类型、大小、SHA-256、来源和生成状态。
5. 归档成功后删除临时执行目录，并验证目录确实不存在。

仅把临时目录改到 results 下不足以解决 G1；必须同时处理 `/root` 路径语义。

### Phase 3 建议边界

1. parser 对缺失 `[agent].timeout_sec`、`[verifier].timeout_sec` 和
   `[environment]` 发出显式 warning，并记录字段来源状态。
2. converter 原样传播 agent、verifier、environment timeout 及 environment metadata。
3. CLI 和 verifier 对官方 SkillFlow task 不再静默使用仓库默认 timeout。
4. 非 SkillFlow 自建任务可保留兼容策略，但必须与官方任务路径明确区分。

### Phase 4 建议边界

1. 保持默认 `max_retry=0`。
2. 仅为明确的 API unavailable、CLI process 在 agent 执行前崩溃建立可重试类型。
3. agent timeout、正常 task failure、verifier failure 明确不可重试。
4. checkpoint 记录 retry 原因、异常分类和是否开始过 agent execution。

## 7. Phase 1 结论

当前实现具有“每次调用新建临时目录”的基础隔离，但不具备官方 Harbor 等价的
task-level filesystem isolation。Windows 对 `/root` 的解析会绕过临时目录，这是
reward 可能被跨任务文件污染的直接路径。

timeout 的官方键路径已基本接通，但缺失值仍静默 fallback，`[environment]` 也未
成为可执行 runtime 配置。retry 默认次数已对齐，失败类型分类尚未对齐。

因此在完成 Phase 2-4 前，不应把当前 Setting1 reward 声明为来自官方等价的 clean
execution。Phase 1 到此结束；本报告没有修改 benchmark task、task order、模型、
harness、skill evolution、evaluation metric，也没有启动 Setting2/3/4 或任何实验。