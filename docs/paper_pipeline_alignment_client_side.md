# FederatedSkill 论文管线与复现代码逐模块对齐报告（Client 侧）

## 0. 对齐范围与方法
本报告按论文 FederatedSkill 的 client-side 主管线逐步对齐，关注五个对象：
1. Client Profile ρ_i
2. Trial Execution
3. Real Agent Harness
4. Trajectory τ_{i,t}
5. Trajectory Compaction C(τ)

每个模块都按同一结构展开：
1. 论文数学对象或原始定义
2. 你复现代码中的职责模块
3. 输入与输出
4. 关键代码摘录
5. 当前实验结果能证明什么

本报告使用的代码与结果证据文件：
- [core/datatypes.py](core/datatypes.py)
- [executor/harness_executor.py](executor/harness_executor.py)
- [harness/cli_harness_base.py](harness/cli_harness_base.py)
- [client/trajectory.py](client/trajectory.py)
- [client/distiller.py](client/distiller.py)
- [results/subset_setting1_v3/paper_tables_v1/table1_overall_performance_comparison.csv](results/subset_setting1_v3/paper_tables_v1/table1_overall_performance_comparison.csv)
- [results/subset_setting1_v3/paper_tables_v1/table2_skill_evolution_across_rounds.csv](results/subset_setting1_v3/paper_tables_v1/table2_skill_evolution_across_rounds.csv)
- [results/subset_setting1_v3/paper_tables_v1/table4_skill_reuse_trajectory_cross_format.csv](results/subset_setting1_v3/paper_tables_v1/table4_skill_reuse_trajectory_cross_format.csv)
- [results/subset_setting1_v3/paper_tables_v1/task_level_outcomes_all.csv](results/subset_setting1_v3/paper_tables_v1/task_level_outcomes_all.csv)

## 1. Client Profile ρ_i

### 1.1 论文对象
论文将每个 client 建模为静态 profile ρ_i，用于决定本地执行器与蒸馏器的行为。你给出的符号可写为：
ρ_i = (model_i, provider_i, harness_i, context_i)

其核心作用是将“谁在执行”显式化，避免把 worker 当作匿名进程。

### 1.2 代码职责映射
映射到 [core/datatypes.py](core/datatypes.py#L148) 的 WorkerProfile。

字段对应关系：
1. model_i -> backbone_model
2. provider_i -> model_provider
3. harness_i -> agent_harness
4. context_i -> generation_config / metadata / system_prompt_name / agent_kwargs

### 1.3 输入输出
输入：实验配置（worker 配置、模型路由、harness 选择）。
输出：不可变 WorkerProfile 对象（frozen），供执行与蒸馏阶段共享。

### 1.4 关键代码摘录
    class WorkerProfile(BaseModel):
        model_config = ConfigDict(frozen=True)
        client_id: str
        backbone_model: str
        agent_harness: str
        model_provider: str
        api_base: str
        api_key_env: str
        system_prompt_name: str = "default"
        agent_kwargs: dict[str, Any] = Field(default_factory=dict)
        generation_config: dict[str, Any] = Field(default_factory=dict)
        metadata: dict[str, Any] = Field(default_factory=dict)

### 1.5 目前结果能证明什么
你当前运行记录已经证明 profile 被用于异构执行路由（不同 family 的 harness/运行细节不同），并且与后续 distiller 调用绑定。

证据位置：
- [executor/harness_executor.py](executor/harness_executor.py#L68)
- [client/distiller.py](client/distiller.py#L139)


## 2. Trial Execution

### 2.1 论文对象
论文中一次 trial 的输入是任务与本地技能库，即 (x_t, L_{i,t})，输出是轨迹 τ_{i,t} 与 verifier 奖励。

流程可表达为：
Task -> Skill Retrieval -> Agent Execution -> Tool Calls -> Verification -> Reward

### 2.2 代码职责映射
你的代码不是单文件完成，而是两层：
1. 调度层：[executor/harness_executor.py](executor/harness_executor.py#L41)
2. 执行层：[harness/cli_harness_base.py](harness/cli_harness_base.py#L134)

HarnessAwareExecutor 负责按 profile.agent_harness 选择具体 harness；CLIAgentHarnessBase 负责具体试验执行。

### 2.3 输入输出
输入：
1. task (x_t)
2. library (L_{i,t})
3. profile (ρ_i)
4. round_idx

输出：Trajectory（含动作、工具调用、验证结果、奖励、成本）。

### 2.4 关键代码摘录
    def run(self, task, library, profile, round_idx=0):
        harness = self._get_or_build_harness(profile.agent_harness)
        return harness.run(task=task, library=library, profile=profile, round_idx=round_idx)

    def execute_task(self, task, library, profile, workspace):
        relevant_entries = self._helper._retrieve_skills(task, library)
        skills_text = self._helper._format_skills(relevant_entries)
        system_prompt = self._helper._build_system_prompt(profile)
        user_prompt = self._build_workspace_prompt(task, skills_text)
        cli_result = run_cli_subprocess(...)
        ...
        return HarnessExecutionResult(..., retrieved_skill_paths=retrieved_skill_paths)

### 2.5 目前结果能证明什么
当前结果可以证明这条执行链是“真实跑通”的，而不是离线伪造：
1. 每轮都有 round summary 与 task-level reward。
2. 每个任务都有 completed / completed_unsuccessful 状态。
3. 任务级结果能看到 verifier 失败类型细分。

证据文件：
- [results/subset_setting1_v3/paper_tables_v1/task_level_outcomes_all.csv](results/subset_setting1_v3/paper_tables_v1/task_level_outcomes_all.csv)


## 3. Real Agent Harness

### 3.1 论文对象
论文强调 agent 与工具环境之间通过 harness 接口连接，harness 决定了真实工具调用方式与运行时约束。

### 3.2 代码职责映射
映射到 [harness/cli_harness_base.py](harness/cli_harness_base.py#L85)。

职责包括：
1. CLI binary 检测与版本确认
2. workspace 初始化与输入文件注入
3. prompt 构建
4. subprocess 调用
5. 运行失败策略（returncode/timeout/exception）
6. 生成文件 diff 与收集

### 3.3 输入输出
输入：profile、task、library、workspace。
输出：HarnessExecutionResult（files/stdout/stderr/tokens/cost/retrieved_skill_paths）。

### 3.4 关键代码摘录
    def initialize(self, task, profile):
        self._cli_version = check_cli_binary(self.binary_name, self.version_args)
        ws = WorkspaceManager(prefix=f"{self.harness_name}_{task.task_id}_")
        ...

    def _validate_cli_result(self, cli_result, effective_timeout=None):
        if cli_result.timed_out or cli_result.exception is not None or cli_result.returncode != 0:
            raise TaskExecutionError(...)

### 3.5 目前结果能证明什么
从结果文件看，你已经不是 mock harness，而是具有真实 CLI 语义的 harness 执行路径：
1. 带有 timeout、returncode、stderr/stdout。
2. 生成文件与 workspace 快照可追溯。
3. 对执行失败会 fail-loud，不会静默当成功。

证据文件：
- [results/subset_setting1_v3/families/PPT-Formatting-Optimization/tasks/wildlife-field-guide-caption-cleanup/workspace_manifest.json](results/subset_setting1_v3/families/PPT-Formatting-Optimization/tasks/wildlife-field-guide-caption-cleanup/workspace_manifest.json)
- [results/subset_setting1_v3/families/Cross-Format-Data-Reconciliation/tasks/05-datacenter-hardware-registry-diff/workspace_manifest.json](results/subset_setting1_v3/families/Cross-Format-Data-Reconciliation/tasks/05-datacenter-hardware-registry-diff/workspace_manifest.json)


## 4. Trajectory τ_{i,t}

### 4.1 论文对象
论文把轨迹定义为本地执行序列 τ_{i,t} = (s_0, a_0, o_0, ..., r_t)，并用于后续 patch distillation。

### 4.2 代码职责映射
映射到：
1. 轨迹结构定义：[core/datatypes.py](core/datatypes.py#L255), [core/datatypes.py](core/datatypes.py#L274)
2. 轨迹收集写入：[harness/cli_harness_base.py](harness/cli_harness_base.py#L304)

### 4.3 输入输出
输入：执行过程中的步骤、工具调用、观测、异常、验证器结果。
输出：结构化 Trajectory（可被压缩、可被蒸馏、可被审计）。

### 4.4 关键代码摘录
    class TrajectoryStep(BaseModel):
        step_index: int
        role: str
        content: str
        tool_calls: list[dict[str, Any]]
        tool_results: list[dict[str, Any]]
        observation: str
        tokens_used: int

    class Trajectory(BaseModel):
        task_name: str
        worker_id: str
        round_idx: int
        steps: list[TrajectoryStep]
        reward: float | None
        verifier_output: str
        total_tokens: int
        cost_usd: float
        actions: list[dict[str, Any]]
        generated_files: list[str]
        verification: dict[str, Any]

    def collect_trajectory(...):
        collector.add_action("skill_retrieval", ...)
        collector.add_action("cli_invoke", ...)
        collector.add_step(...)
        collector.set_stdio(...)
        collector.add_tokens(...)

### 4.5 目前结果能证明什么
你的结果目录已经形成“每任务一条可重放轨迹”的证据链：
1. 有 task 级 trajectory.json。
2. 轨迹里有 skill_retrieval、cli_invoke、verify 等动作。
3. 轨迹和 reward.json 可交叉验证奖励来源。

证据文件：
- [results/subset_setting1_v3/families/Cross-Format-Data-Reconciliation/tasks/07-shipping-container-manifest-diff/trajectory/u0/trajectory.json](results/subset_setting1_v3/families/Cross-Format-Data-Reconciliation/tasks/07-shipping-container-manifest-diff/trajectory/u0/trajectory.json)
- [results/subset_setting1_v3/families/PPT-Formatting-Optimization/tasks/archive-photo-caption-cleanup/trajectory/u0/trajectory.json](results/subset_setting1_v3/families/PPT-Formatting-Optimization/tasks/archive-photo-caption-cleanup/trajectory/u0/trajectory.json)


## 5. Trajectory Compaction C(τ) -> τ_hat

### 5.1 论文对象
论文动机是 trajectory 太长且包含敏感细节，需要压缩函数 C(τ) 生成可蒸馏输入 τ_hat，同时保证隐私与 token 成本。

### 5.2 代码职责映射
映射到 [client/trajectory.py](client/trajectory.py#L29) 的 TrajectoryCompressor。

### 5.3 输入输出
输入：完整 Trajectory。
输出：CompactedTrajectory（最多 K_step 步、每步 observation 截断到 K_obs，敏感字段剥离）。

### 5.4 关键代码摘录
    class TrajectoryCompressor:
        def compress(self, trajectory: Trajectory) -> CompactedTrajectory:
            selected = self._select_steps(trajectory.steps)
            cleaned = [self._clean_step(s) for s in selected]
            ...

        def _select_steps(self, steps):
            # 初始步 + 最近 K_step-1 步

        def _clean_step(self, step):
            # 截断 observation
            # 清零 tokens_used
            # tool_calls 仅保留 function.name

### 5.5 目前结果能证明什么
你已经有压缩效果的可观测指标：trajectory_tokens 与 patch_tokens，能直接计算压缩代理指标。
例如在 subset_setting1_v3 中，存在轨迹 148103 tokens，对应 patch 1048 tokens 的案例，说明压缩+蒸馏显著降低上传与后续推理负担。

证据文件：
- [results/subset_setting1_v3/families/Cross-Format-Data-Reconciliation/round_07_summary.json](results/subset_setting1_v3/families/Cross-Format-Data-Reconciliation/round_07_summary.json)
- [results/subset_setting1_v3/paper_tables_v1/table3_performance_evolution_by_family.csv](results/subset_setting1_v3/paper_tables_v1/table3_performance_evolution_by_family.csv)


## 6. 你复现结果对这五个模块的“可证明性”结论

### 6.1 已经能证明
1. ρ_i 不是抽象概念，已落成可路由的 WorkerProfile，并参与执行与蒸馏。
2. Trial execution 是真实任务闭环，不是离线构造日志。
3. Harness 层具备真实 CLI 子进程与错误传播机制。
4. τ_{i,t} 结构化落盘，能支持 replay、审计与后续 patch 蒸馏。
5. C(τ) 的压缩逻辑明确实现，且在结果里能看到 token 规模差异。

### 6.2 仍需谨慎表述
1. 这组结果主要证明 Self-Evolution client-side 主管线跑通，不等于完整 federated aggregation 效果已复现。
2. 成功率受验证环境工件与路径兼容问题影响时，不能把全部失败都归因于算法能力。


## 7. 报告可直接引用的对应表述（建议）
我们按照论文中 client-side 机制将复现系统拆解为五个可验证对象：静态 client profile ρ_i、任务执行闭环、harness 运行时接口、轨迹 τ_{i,t} 以及压缩算子 C(τ)。从代码层面看，这五者分别由 WorkerProfile、HarnessAwareExecutor/CLIAgentHarnessBase、Trajectory/TrajectoryCollector 与 TrajectoryCompressor 显式实现；从实验产物看，对应的 task-level trajectory、reward、workspace manifest 与 token 统计已经形成可审计证据链。这说明复现已经覆盖论文核心的本地演化管线，而不仅是结果文件层面的形式对齐。
