"""
harness/ — Agent CLI Harness 抽象层（Algorithm Fidelity Fix — Real CLI Harness）

[ENGINEERING] 本包标签：工程实现细节（Agent Runtime/Harness 替换层），
不改变 core/server 里任何算法公式（skill patch schema、federated
aggregation、evaluation metric）。

背景（对应论文 arXiv:2606.03143 Section 3 / 4.1.1）：
    论文里的 client worker 由 ρ_i=(backbone_model, agent_harness) 描述，
    agent_harness ∈ {claude-code, qwen-code, kimi-cli}——这三者在官方实现
    （`FederatedSkill-main/skillfl/skillflow_adapter/`）里是**真实的 CLI
    二进制程序**（用 subprocess 调用，如 `claude --print --output-format
    stream-json`），而本仓库此前的 `executor/agent_executor.py::
    AgentWorkspaceExecutor` 只是"LLM API 直连 + 自建多文件 workspace 执行器"，
    `core.datatypes.WorkerProfile.agent_harness` 字段仅作为 prompt 风格提示
    字符串使用，从未真正 spawn 对应 CLI 进程（已在 docs/SIMPLIFICATIONS.md
    §6.4 如实披露）。

本包新增内容（只做"替换执行后端"，不重写系统）：
    - base_harness.py            统一接口 BaseAgentHarness
    - cli_utils.py                CLI 二进制检测 + subprocess 封装
    - trajectory_adapter.py       CLI event -> 论文 trajectory 字段 schema
    - claude_code_harness.py      真实 `claude` CLI（命令语法取自官方仓库
                                   skillfl/skillflow_adapter/merge.py 的
                                   make_claude_code_subprocess_runner）
    - qwen_code_harness.py        真实 `qwen-code` CLI（OpenAI 协议 env 变量
                                   取自官方仓库 cli.py 注释，具体 flag 语法
                                   官方仓库未给出强证据，已在类文档字符串中
                                   如实标注为"按 claude-code 惯例类比设计"）
    - kimi_cli_harness.py         真实 `kimi` CLI（成功标记字符串取自官方
                                   仓库 skillflow_adapter/runner.py）
    - api_workspace_harness.py    APIWorkspaceHarness——委托给既有
                                   AgentWorkspaceExecutor，**不删除、不修改**
                                   该类一行代码，作为 debug/未安装 CLI 时的
                                   显式回退（不是静默回退，由调用方显式选择
                                   mode="debug" 才会用到）
    - factory.py                  get_harness(agent_harness, mode) 统一入口

默认行为不变：本包内任何模块都不会被已有真实实验入口
（main_trainer.py / experiments/run_experiment.py / run.py）默认调用；
只有显式传入 `execution_mode="cli"`（或 run.py 的 `--execution-mode cli`）
才会切换到本包的真实 CLI 路径，默认仍是原有 API 路径
（VerificationAwareExecutor），保证向后兼容与"不破坏已有 Stage1-6 工作"。
"""

from harness.base_harness import BaseAgentHarness, HarnessExecutionResult
from harness.cli_utils import CLIBinaryNotFoundError, check_cli_binary
from harness.factory import get_harness

__all__ = [
    "BaseAgentHarness",
    "HarnessExecutionResult",
    "CLIBinaryNotFoundError",
    "check_cli_binary",
    "get_harness",
]
