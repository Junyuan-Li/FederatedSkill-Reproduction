"""
federated_score.py — 论文 Eq.(3) 全局加权得分（Phase14 任务2新增）

论文公式（Section 3 / Eq. 3）：

    J̄^t = Σ_i q_i · J_i(L_i^t)

其中：
  - J_i(L_i^t)：worker i 在其（第 t 轮的）本地技能库 L_i^t 下的局部性能指标
    （通常取 success_rate，即 evaluation.metrics.FederatedMetrics.success_rate()
    的输出，或 benchmark/evaluator.py 里逐 worker 计算的得分）
  - q_i：worker i 的权重（论文默认按各 worker 任务量占比分配，Σ_i q_i = 1；
    退化到"各 worker 同等重要"时 q_i = 1/n）

本模块只做"加权求和"这一步的标准化实现，不重复 J_i 本身的计算逻辑
（J_i 已经分别在 evaluation/metrics.py::FederatedMetrics 和
benchmark/evaluator.py::BenchmarkEvaluator 里实现，本模块与它们都不冲突，
可以直接拿它们算出来的每个 worker 的分数作为 local_scores 输入）。
"""

from __future__ import annotations


def weighted_global_score(
    local_scores: dict[str, float],
    weights: dict[str, float] | None = None,
) -> float:
    """
    计算论文 Eq.(3)：J̄ = Σ_i q_i · J_i(L_i)。

    Args:
        local_scores: {worker_id: J_i}，每个 worker 的局部性能得分
            （如 success_rate ∈ [0, 1]，也可以是任意标量指标）
        weights: {worker_id: q_i}，每个 worker 的权重。
            默认为 None 时按 worker 数量均分权重（q_i = 1/n，对应论文
            "各 client 同等重要"的简化假设）；提供时必须覆盖
            local_scores 里的每一个 worker_id（不要求 Σq_i 恰好等于 1，
            如需归一化请先调用 normalize_weights()）

    Returns:
        J̄（加权全局得分）

    Raises:
        ValueError: local_scores 为空；或 weights 提供但缺少某些 worker 的权重
    """
    if not local_scores:
        raise ValueError("local_scores 不能为空")

    if weights is None:
        n = len(local_scores)
        weights = {worker_id: 1.0 / n for worker_id in local_scores}
    else:
        missing = set(local_scores) - set(weights)
        if missing:
            raise ValueError(f"weights 缺少以下 worker 的权重: {sorted(missing)}")

    return sum(weights[worker_id] * score for worker_id, score in local_scores.items())


def normalize_weights(raw_weights: dict[str, float]) -> dict[str, float]:
    """
    将任意正数权重归一化为 Σ_i q_i = 1（如按各 worker 任务量分配原始权重时常用，
    归一化后再传给 weighted_global_score()）。

    Args:
        raw_weights: {worker_id: 任意正数权重}

    Returns:
        {worker_id: q_i}，Σ_i q_i = 1

    Raises:
        ValueError: 权重总和 <= 0
    """
    total = sum(raw_weights.values())
    if total <= 0:
        raise ValueError("权重总和必须为正数")
    return {worker_id: w / total for worker_id, w in raw_weights.items()}


def weighted_global_score_over_rounds(
    per_round_local_scores: list[dict[str, float]],
    per_round_weights: list[dict[str, float]] | None = None,
) -> list[float]:
    """
    对多轮 {round: {worker_id: J_i}} 依次计算 J̄^t，返回逐轮序列
    （对应论文 Figure 2 里 J̄^t 随轮次 t 演化的曲线）。

    Args:
        per_round_local_scores: 长度为 T 的列表，第 t 个元素是该轮的
            {worker_id: J_i} 字典
        per_round_weights: 若提供，长度必须与 per_round_local_scores 相同，
            对应每轮各自的权重；不提供则每轮都用均分权重

    Returns:
        长度为 T 的 J̄^t 序列
    """
    if per_round_weights is not None and len(per_round_weights) != len(per_round_local_scores):
        raise ValueError("per_round_weights 长度必须与 per_round_local_scores 相同")

    return [
        weighted_global_score(
            local_scores,
            per_round_weights[t] if per_round_weights is not None else None,
        )
        for t, local_scores in enumerate(per_round_local_scores)
    ]
