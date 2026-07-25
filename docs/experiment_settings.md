# 实验设置说明（Experiment Settings）

对应论文 4 个实验设置。**已存在的配置文件与用户 Phase4 计划里要求的
`setting1_self_evolve.yaml` / `setting2_homo_fed.yaml` / `setting3_hetero_backbone.yaml` /
`setting4_full_hetero.yaml` 语义完全一致**，只是命名少了 `settingN_` 数字前缀——
沿用现有文件，不重复创建同语义的第二套配置。

| 论文 Setting | 描述 | 现有配置文件 |
|---|---|---|
| Setting 1: Self-Evolve | 单 client，server 关闭，agent 自己演化技能库 | `experiments/configs/setting_se.yaml`、`setting_se_family.yaml`（family curriculum 版本） |
| Setting 2: Homogeneous Federated | 3 个相同 backbone 的 client，server 开启 | `experiments/configs/setting_homo_fed.yaml` |
| Setting 3: Heterogeneous Backbone | 3 个不同 backbone、相同 agent harness 的 client | `experiments/configs/setting_hetero_backbone.yaml` |
| Setting 4: Full Heterogeneous | 不同 backbone + 不同 agent harness（如 GPT+react / Qwen+cot / DeepSeek+tool-agent） | `experiments/configs/setting_full_hetero.yaml` |

## Sprint 2 待办（不在本 Sprint 1 范围内）

这些配置目前用 mock/占位 backbone 跑通闭环（`_test_e2e.py`）。Sprint 2 需要：

1. 在 `core/llm/`（或复用现有 `llm/backbone.py` + `llm/router.py`，待 Sprint 2 决定
   具体如何reconcile，避免维护两套 LLM 客户端抽象）接入真实 provider
   （DashScope / Moonshot / Anthropic / OpenAI 等）。
2. 给上述 4 个配置文件补上真实 API key 环境变量名、真实模型名，
   使其可以真实调用（用户已确认有 key 并同意真实费用）。
3. 真实数据集接入后（Sprint 1 已搭好 `benchmark/skillflow_adapter/` 骨架，
   真实下载仍需用户显式确认），可选把这些配置的 `families_dir` 指向
   `benchmark/cache/`（真实 SkillFlow 数据转换后的缓存）。

在真正触发真实 API 调用/真实数据下载前，会再次向用户确认。
