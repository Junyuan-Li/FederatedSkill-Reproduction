"""
metrics.py — 论文 Section 5 全部实验指标

每个方法均注明对应的论文位置和原文语句，便于课程报告引用。

论文：FederatedSkill: Federated Learning for Agentic Skill Evolution
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 单次 trial 快照——计算指标的最小数据单元
# ---------------------------------------------------------------------------


@dataclass
class TrialSnapshot:
    """
    一次 trial 的关键度量快照。

    由 Runner 在每个 round 每个 client 执行完后填充，
    收集给 FederatedMetrics 进行指标计算。
    """

    round_idx: int
    worker_id: str
    task_id: str

    reward: float = 0.0           # R_{i,x}(τ)，论文 Eq.(1)
    soft_reward: float = 0.0      # sub-test 通过率（匹配官方 _compute_soft_reward）
    trajectory_tokens: int = 0    # 原始轨迹 token 数（隐私代理基准）
    patch_tokens: int = 0         # 上传 patch token 数（压缩比分子）
    library_size_before: int = 0  # round 前技能库技能数
    library_size_after: int = 0   # round 后技能库技能数
    cost_usd: float = 0.0         # 本次 trial 总推理费用
    # Phase14 新增（默认值，向后兼容旧调用方）：基于真实轨迹/patch 文本计算的
    # 真实 SELR（论文 Appendix E Eq.5，复用 evaluation/selr.py），与
    # trajectory_tokens/patch_tokens 推导的 token 级代理指标并存。
    selr: float = 0.0
    n_sensitive_entities: int = 0
    n_leaked_entities: int = 0


# ---------------------------------------------------------------------------
# 论文指标计算器
# ---------------------------------------------------------------------------


class FederatedMetrics:
    """
    论文 Section 5 及附录所有核心指标的静态计算集合。

    设计为纯函数（无状态），每个方法均可单独调用，
    也可通过 compute_all() 一次性获取全部指标。
    """

    # -------------------------------------------------------------------
    # 1. Success Rate  (Table 1 / Figure 2)
    # -------------------------------------------------------------------

    @staticmethod
    def success_rate(rewards: list[float]) -> float:
        """
        SR = N_success / N_total

        论文 Section 4.1.1：
            "each trajectory is then evaluated by the environment to assign
             a verification reward R_{i,x}(τ)"

        判定规则：reward >= 1.0 视为成功（二值 reward）。
        """
        if not rewards:
            return 0.0
        return sum(1 for r in rewards if r >= 1.0) / len(rewards)

    # -------------------------------------------------------------------
    # 2. Communication Compression Ratio  (Appendix C / Table 6)
    # -------------------------------------------------------------------

    @staticmethod
    def compression_ratio(patch_tokens: int, trajectory_tokens: int) -> float:
        """
        CR = 1 − |Patch| / |Trajectory|

        论文 Appendix C：
            "FederatedSkill instead exchanges only the skill patch (a lightweight
             JSON manifest and plain-text file updates), so the payload is several
             orders of magnitude smaller."

        Table 6 数据：每个 family 全程通信 99–281 KB，整体 ~3.49 MB。
        CR 越接近 1.0 → 压缩越好；CR < 0 表示 patch 反而比轨迹大（异常）。
        """
        if trajectory_tokens <= 0:
            return 0.0
        return max(0.0, 1.0 - patch_tokens / trajectory_tokens)

    # -------------------------------------------------------------------
    # 3. Privacy Gain  (Appendix E / Table 8)
    # -------------------------------------------------------------------

    @staticmethod
    def privacy_gain(trajectory_tokens: int, patch_tokens: int) -> float:
        """
        PrivacyGain = (T_traj − T_patch) / T_traj

        ⚠️ 重要澄清（代码审计后新增）：
        这个公式在数值上与 compression_ratio() **完全相同**
        （1 - patch/traj == (traj-patch)/traj），只是换了个名字。
        它衡量的是「上传 token 量减少了多少」，
        **不是**论文 Appendix E (Table 8) 里真正的 SELR
        (Sensitive Entity Leakage Rate — 5.08% vs 52.0%，按敏感实体计数，
         需要 NER/正则扫描 patch 内容才能真正计算)。

        本仓库 client/distiller.py 的 _audit_privacy() 做了敏感词正则扫描，
        但只用于拒绝/告警，没有产出一个可比的 SELR 数值。
        若要严谨复现 Table 8，需要另外实现「按实体计数」的 SELR 计算器，
        不能用这个 token 比例代替。

        论文本身也承认这只是 empirical 而非 cryptographic 隐私保证
        （Section 9 Limitations）：
            "FederatedSkill relies on empirical validation rather than hard
             cryptographic guarantees (such as formal Differential Privacy)."

        建议：报告/展示时称其为「通信压缩比（隐私代理指标，非 SELR）」，
        不要单独包装成「隐私增益」，以免被追问时说不清楚。
        """
        if trajectory_tokens <= 0:
            return 0.0
        return max(0.0, (trajectory_tokens - patch_tokens) / trajectory_tokens)

    # -------------------------------------------------------------------
    # 3.1 敏感实体泄漏率估算（Sensitive Entity Leakage Rate 近似实现）
    # -------------------------------------------------------------------

    #: 敏感实体正则模式（复用 client/distiller.py::_LEAK_PATTERNS 的检测思路，
    #: 扩展为可计数的实体级扫描，而非仅用于告警）。
    _SENSITIVE_ENTITY_PATTERNS: tuple[tuple[str, str], ...] = (
        (r"\b[A-Z]{2,6}-\d{4,}\b", "task_id"),
        (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "ip_address"),
        (r"(?i)\b(?:password|passwd|secret|token|api_key)\s*[:=]\s*\S+", "credential"),
        (r"[\w.+-]+@[\w-]+\.[\w.-]+", "email"),
        (r"\b\d{3}[-.\s]?\d{3,4}[-.\s]?\d{4}\b", "phone_number"),
        (r"/(?:home|tmp|var|etc|root)/\S+", "absolute_path"),
        (r"(?i)\b(?:confidential|internal only|top secret)\b", "sensitivity_marker"),
    )

    @classmethod
    def sensitive_entity_leakage_rate(
        cls,
        source_text: str,
        target_text: str,
    ) -> float | None:
        """
        SELR_hat(C) ≈ |{sensitive entities extracted from source_text}
                        ∩ {entities that also appear verbatim in target_text}|
                      / |{sensitive entities extracted from source_text}|

        对应论文 Appendix E Eq.(5) 的**近似**实现，用正则实体抽取 + 子串匹配替代
        论文原文使用的 LLM 语义裁判（"strict-AND" semantic judge）。

        ⚠️ 与 privacy_gain() 的关键区别（不要混用）：
          - privacy_gain()：token 数量比例，衡量"传了多少内容"，与敏感性无关。
          - 本方法：实体计数比例，衡量"传的内容里有多少可辨识敏感实体原样出现"，
            更接近论文 Table 8 的统计口径，但仍有两点简化，需在报告中说明：
              1. 实体抽取用固定正则模式（task_id/IP/邮箱/电话/凭据/绝对路径/敏感标记词），
                 不是论文里做 PII 分级（低/中/高敏感度）的完整分类体系；
              2. 泄漏判定用精确子串匹配，不含论文强调的 LLM 语义裁判
                 （例如改写、同义改述、部分信息重组等语义级泄漏不会被本方法捕获，
                 会导致本方法**低估**真实泄漏率）。
          因此本方法的输出应标注为「实体子串匹配的 SELR 近似值」，不能直接
          与论文 Table 8 的百分比做定量对比，但可用于横向比较（例如同一份轨迹
          在不同净化策略下的相对泄漏程度变化）。

        Args:
            source_text: 敏感实体的抽取来源，通常是原始轨迹 Trajectory 的
                         完整文本（system+user+assistant 拼接）。
            target_text: 被检查是否包含泄漏的目标语料，例如上传的 WorkerPatch
                         的 upserts 内容拼接、或合并后的广播 patch 内容。

        Returns:
            [0, 1] 区间的近似 SELR；若 source_text 中未抽取到任何敏感实体，
            返回 None（表示无法定义比例，而不是 0.0，避免被误读为"零泄漏"）。
        """
        import re

        entities: set[str] = set()
        for pattern, _label in cls._SENSITIVE_ENTITY_PATTERNS:
            entities.update(m.group(0) for m in re.finditer(pattern, source_text))

        if not entities:
            return None

        leaked = sum(1 for e in entities if e in target_text)
        return leaked / len(entities)

    # -------------------------------------------------------------------
    # 4. Skill Growth  (Figure 3)
    # -------------------------------------------------------------------

    @staticmethod
    def skill_growth(before: int, after: int) -> int:
        """
        ΔSkill = |L_i^{t+1}| − |L_i^t|（正=增长，负=裁剪）

        论文 Figure 3（Section 5.3 Evolution Dynamics）：
            "FederatedSkill successfully mitigates both extremes, constraining
             the libraries of all heterogeneous backbones within a tight band of
             roughly 1.0 to 1.6 skills per family throughout all 8 rounds."

        SE 下 Kimi/Qwen 出现 library bloat（≈2.5 skills），
        GLM-5 出现 stagnation（≈1.0–1.3 skills）。
        FederatedSkill 通过 cross-client deduplication 和 conservative admission 控制增长。
        """
        return after - before

    # -------------------------------------------------------------------
    # 5. Heterogeneity Gain  (Table 2 Ablation)
    # -------------------------------------------------------------------

    @staticmethod
    def heterogeneity_gain(federated_sr: float, solo_sr: float) -> float:
        """
        ΔSR = SR_federated − SR_solo

        论文 Section 5.5 Ablation Study (Table 2)：
            "the personalized evolution agent improves aggregate performance for
             every client by +9.8 to +12.2 pp."

        Kimi K2.5 获得最高增益 (+12.2 pp)，
        Qwen3.6-Plus 和 GLM-5 均提升 +9.8 pp。
        """
        return federated_sr - solo_sr

    # -------------------------------------------------------------------
    # 6. Cost Efficiency  (Figure 4)
    # -------------------------------------------------------------------

    @staticmethod
    def cost_per_solved_task(total_cost_usd: float, n_solved: int) -> float:
        """
        Cost/Solved = total_cost_usd / n_solved_tasks

        论文 Figure 4（Section 5.4 Cost Analysis）：
            "FEDERATEDSKILL lowers the per-solved-task cost by 11% for Qwen,
             33% for GLM-5, and 37% for Kimi."

        原因：FederatedSkill 避免了 SE 的 library bloat，减少了检索时
        的 prompt 膨胀，主要节省出现在较重的 backbone（GLM-5, Kimi）。
        """
        if n_solved <= 0:
            return float("inf")
        return total_cost_usd / n_solved

    # -------------------------------------------------------------------
    # 7. Family Success Curve（本复现新增，非论文原文公式）
    # -------------------------------------------------------------------

    @staticmethod
    def family_success_curve(
        snapshots: list[TrialSnapshot],
        worker_family_map: dict[str, str],
    ) -> dict[str, list[float]]:
        """
        按 task family 聚合"逐轮成功率曲线"，用于展示同一技能族
        随轮次递增难度演化时成功率的变化趋势（gap → covered → absorbing）。

        ⚠️ 这不是论文原文定义的公式，是本复现为了验证 SkillFlow 风格
        lifelong family benchmark（见 benchmark/family.py, curriculum.py）
        新增的辅助可视化指标，独立于 compute_all() / ExperimentEvaluator，
        不修改任何已有指标函数。

        Args:
            snapshots:         跨全部轮次、全部 worker 的 TrialSnapshot 平铺列表
                                （可从 ExperimentResult 收集：
                                 [s for r in result.rounds for s in r.snapshots]）
            worker_family_map: {worker_id: family_id}
                                （FamilyCurriculumSampler.family_for() 可查到）

        Returns:
            {family_id: [round0_sr, round1_sr, ...]}
            某个 family 在某轮没有 snapshot 时该轮记 0.0（不参与平均）。
        """
        from collections import defaultdict

        by_family_round: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        for snap in snapshots:
            family_id = worker_family_map.get(snap.worker_id, "unknown")
            by_family_round[family_id][snap.round_idx].append(snap.reward)

        curves: dict[str, list[float]] = {}
        for family_id, round_map in by_family_round.items():
            max_round = max(round_map.keys())
            curve: list[float] = []
            for r in range(max_round + 1):
                rewards = round_map.get(r, [])
                sr = (sum(1 for x in rewards if x >= 1.0) / len(rewards)) if rewards else 0.0
                curve.append(sr)
            curves[family_id] = curve
        return curves

    # -------------------------------------------------------------------
    # 综合计算
    # -------------------------------------------------------------------

    @classmethod
    def compute_all(cls, snapshots: list[TrialSnapshot]) -> dict[str, float]:
        """
        一次性计算一批快照的全部指标，返回 {metric_name: value} dict。

        用于 ExperimentEvaluator.record_round() 内部调用。
        """
        if not snapshots:
            return {}

        rewards = [s.reward for s in snapshots]
        patch_tok = sum(s.patch_tokens for s in snapshots)
        traj_tok = sum(s.trajectory_tokens for s in snapshots)
        total_cost = sum(s.cost_usd for s in snapshots)
        n_solved = sum(1 for s in snapshots if s.reward >= 1.0)
        mean_lib_after = sum(s.library_size_after for s in snapshots) / len(snapshots)
        mean_skill_growth = sum(
            cls.skill_growth(s.library_size_before, s.library_size_after)
            for s in snapshots
        ) / len(snapshots)
        mean_selr = sum(s.selr for s in snapshots) / len(snapshots)

        return {
            # 论文 Table 1 / Figure 2
            "success_rate": cls.success_rate(rewards),
            # 论文 Appendix C
            "compression_ratio": cls.compression_ratio(patch_tok, traj_tok),
            # 论文 Appendix E（代理）
            "privacy_gain": cls.privacy_gain(traj_tok, patch_tok),
            # 论文 Appendix E Eq.(5)：基于文本的真实 SELR 均值（Phase14 新增，
            # 若 snapshots 未填写 selr 字段则为 0.0，不影响已有读取方）
            "mean_selr": mean_selr,
            # 论文 Figure 3
            "mean_library_size": mean_lib_after,
            "mean_skill_growth": mean_skill_growth,
            # 论文 Figure 4
            "total_cost_usd": total_cost,
            "cost_per_solved_task": cls.cost_per_solved_task(total_cost, n_solved),
            # 辅助
            "n_solved": float(n_solved),
            "n_total": float(len(snapshots)),
        }
