# 真实实验环境配置指南

本文档指导在真实 LLM API 环境下复现论文 FederatedSkill 的四种实验设置。

---

## 目录

1. [环境安装](#1-环境安装)
2. [API Key 配置](#2-api-key-配置)
3. [数据集下载](#3-数据集下载)
4. [运行四种实验设置](#4-运行四种实验设置)
5. [消融实验](#5-消融实验)
6. [结果导出](#6-结果导出)
7. [常见问题](#7-常见问题)

---

## 1. 环境安装

### 1.1 系统要求

| 组件 | 要求 |
|------|------|
| Python | >= 3.10（建议 3.11）|
| Node.js | >= 18（agent harness 需要）|
| 磁盘空间 | 约 2GB（数据集 1.6GB + 输出） |
| 网络 | 可访问 DashScope / Moonshot API |

### 1.2 安装 Python 依赖

```bash
# 方法 A：一键安装脚本（Linux/macOS）
bash scripts/install_dependencies.sh

# 方法 B：手动安装
pip install -r requirements-real.txt
```

### 1.3 安装 Agent Harness

Agent Harness 是论文中各 worker 使用的代码执行代理，必须提前安装。

```bash
# 检查是否已安装（不自动执行 npm install）
bash scripts/install_harness.sh

# 手动安装
npm install -g @anthropic-ai/claude-code   # claude-code（Setting 1-4）
npm install -g @qwen-code/qwen-code        # qwen-code（Setting 4）
pip install kimi-cli                       # kimi-cli（Setting 3-4，可选）
```

> **Windows 用户**：请在 PowerShell 或 Git Bash 中执行以上命令。

### 1.4 验证环境

```bash
python scripts/preflight_check.py
```

输出示例：
```
==================================================
  FederatedSkill Preflight Check
==================================================

[Python]
  Python    ✓  3.11.9

[依赖包]
  核心依赖  ✓  litellm, pyyaml, pydantic, python-dotenv ...

[API Key]
  DASHSCOPE_KEY        ✓  sk-xxx...
  MOONSHOT_KEY         ✗  未设置 → export MOONSHOT_KEY="你的key"

[Agent Harness]
  claude-code          ✓  1.0.32
  qwen-code            ✗  未找到
  kimi-cli             ✗  未找到

结论：
  ✓ 可运行 Setting 1-2 (SE + Homo Fed)
  ⚠ Setting 3-4 需要 MOONSHOT_KEY + claude-code
```

---

## 2. API Key 配置

### 2.1 创建 .env 文件

```bash
cp .env.example .env
# 编辑 .env，填入你的 API Key
```

`.env` 内容（不要提交到 git）：

```env
DASHSCOPE_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MOONSHOT_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 2.2 获取 API Key

| Provider | 申请地址 | 用途 |
|----------|----------|------|
| DashScope（阿里云百炼）| https://bailian.console.aliyun.com/ | Qwen3.6-Plus, GLM-5（Setting 1-4 必须）|
| Moonshot（月之暗面） | https://platform.moonshot.cn/ | Kimi-K2.5（Setting 3-4）|

### 2.3 测试连接

```bash
# 测试所有可用 provider
python scripts/test_llm_connection.py

# 仅测试 DashScope
python scripts/test_llm_connection.py --provider dashscope

# 仅测试 Moonshot
python scripts/test_llm_connection.py --provider moonshot
```

期望输出：
```
[DashScope / qwen3.6-plus]
  端点: https://dashscope.aliyuncs.com/apps/anthropic
  模型: qwen3.6-plus
  响应: 'OK'  ✓ (1.23s)
```

---

## 3. 数据集下载

### 3.1 下载 SkillFlow 数据集

```bash
# 交互式下载（推荐，确认后再下载 ~1.6GB）
python scripts/download_skillflow_dataset.py

# 指定缓存目录
python scripts/download_skillflow_dataset.py --cache_dir data/skillflow

# 国内镜像加速
python scripts/download_skillflow_dataset.py --hf_endpoint https://hf-mirror.com
```

### 3.2 验证数据集

```bash
python benchmark/check_dataset.py

# 或指定路径
python benchmark/check_dataset.py --data_dir benchmark/cache/SkillFlow-Task
```

期望输出：
```
  Family 数: 20
  总任务数:  178
  ✓ Family 数量符合论文（20）
  ✓ 总任务数符合论文范围（178）
```

---

## 4. 运行四种实验设置

所有实验通过统一入口运行：

```bash
python experiments/run_experiment.py --config <配置文件> [选项]
```

### 4.1 Setting 1：自演化基线（SE）

对应论文 Table 1 "SE" 列，单 worker 独立演化，无联邦协作。

```bash
python experiments/run_experiment.py \
    --config experiments/configs/setting_se.yaml \
    --rounds 8 \
    --output results/setting1_se \
    --log-level INFO
```

### 4.2 Setting 2：同构联邦（Homo Fed）

对应论文 Table 1 "Homo Fed" 列，多 worker 使用相同 backbone（qwen3.6-plus），
通过服务器聚合共享技能。

```bash
python experiments/run_experiment.py \
    --config experiments/configs/setting_homo_fed.yaml \
    --rounds 8 \
    --output results/setting2_homo_fed
```

### 4.3 Setting 3：异构 Backbone（Hetero Backbone）

对应论文 Table 1 "Hetero-B" 列，3 个 worker 分别使用
qwen3.6-plus / glm-5 / kimi-k2.5，联邦共享演化。

**前提**：需要 MOONSHOT_KEY 和 kimi-cli。

```bash
python experiments/run_experiment.py \
    --config experiments/configs/setting_hetero_backbone.yaml \
    --rounds 8 \
    --output results/setting3_hetero_backbone
```

### 4.4 Setting 4：全异构（Full Hetero）

对应论文 Table 1 "Full Hetero" 列，backbone + harness 均异构。

**前提**：需要 MOONSHOT_KEY + claude-code + qwen-code + kimi-cli。

```bash
python experiments/run_experiment.py \
    --config experiments/configs/setting_full_hetero.yaml \
    --rounds 8 \
    --output results/setting4_full_hetero
```

### 4.5 Dry Run（验证配置，不实际调用 LLM）

```bash
python experiments/run_experiment.py \
    --config experiments/configs/setting_se.yaml \
    --dry-run
```

---

## 5. 消融实验

消融实验通过在配置文件的 `ablation:` 字段启用开关。

| 消融 | 含义 | 配置文件 |
|------|------|----------|
| A1 | 禁用 Capability Matrix | `ablation_a1_no_capability_matrix.yaml` |
| A2 | 全局共享技能库（无隐私保护）| `ablation_a2_global_library.yaml` |
| A3 | 传原始轨迹（不提炼 Patch）| `ablation_a3_full_trajectory.yaml` |

```bash
# A1
python experiments/run_experiment.py \
    --config experiments/configs/ablation_a1_no_capability_matrix.yaml \
    --rounds 8 --output results/ablation_a1

# A2
python experiments/run_experiment.py \
    --config experiments/configs/ablation_a2_global_library.yaml \
    --rounds 8 --output results/ablation_a2

# A3
python experiments/run_experiment.py \
    --config experiments/configs/ablation_a3_full_trajectory.yaml \
    --rounds 8 --output results/ablation_a3
```

---

## 6. 结果导出

### 6.1 生成 CSV 表格 + 论文图表

```bash
python experiments/generate_results.py \
    --results results/setting1_se \
    --output results/tables_and_figures
```

输出文件：

| 文件 | 内容 |
|------|------|
| `success_rate.csv` | Table 1：各设置任务成功率 |
| `communication.csv` | Table 2：通信开销（压缩比）|
| `privacy.csv` | Table 3：隐私保护指标 |
| `skill_growth.csv` | Table 4：技能库增长量 |
| `figure_success_curve.png` | Figure 2：成功率随轮次变化 |
| `figure_skill_growth.png` | Figure 3：技能增长曲线 |
| `figure_compression.png` | Figure 4：通信压缩率 |

### 6.2 批量导出所有实验

```bash
for dir in results/setting*; do
    python experiments/generate_results.py --results "$dir" --output "${dir}/export"
done
```

### 6.3 验证配置文件字段（可选）

```bash
python scripts/validate_configs.py
```

---

## 7. 常见问题

### Q：API 调用返回 AuthenticationError

确认 `.env` 文件中的 Key 填写正确，且已在对应平台开通模型访问权限。

```bash
# 重新测试连接
python scripts/test_llm_connection.py --verbose
```

### Q：Moonshot 返回 temperature 错误

Kimi 模型要求 `temperature >= 1.0`。配置文件中 kimi worker 的
`patcher.temperature` 必须 >= 1.0，代码已自动处理此限制。

### Q：huggingface_hub 下载超时 / 无法访问

使用国内镜像：

```bash
python scripts/download_skillflow_dataset.py --hf_endpoint https://hf-mirror.com
```

或设置环境变量：

```bash
export HF_ENDPOINT=https://hf-mirror.com
python scripts/download_skillflow_dataset.py
```

### Q：agent harness 命令找不到（Windows）

确认 npm 全局安装目录在 PATH 中：

```powershell
npm config get prefix     # 获取全局安装目录
# 将 <prefix>\node_modules\.bin 加入 PATH
```

### Q：dry-run 成功但真实运行报错

大多数情况是 API Key 未配置或网络不通。
运行 `python scripts/preflight_check.py` 检查环境后再试。
