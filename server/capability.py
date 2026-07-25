"""
capability.py — 能力矩阵追踪器（CapabilityTracker）

对应论文 Section 4.2.1 中的 C^t：

    'Each row is a task workflow… Each column is a client.
     Each cell in C^t records how well a client has mastered a specific
     workflow, assigning one of four states: covered, absorbing, broken, or gap.
     A workflow row is retired only when every client's cell becomes covered.'

设计职责：
  1. 维护跨 round 的矩阵状态（字典形式，便于 JSON 序列化）
  2. 从 WorkerPatch 列表自动推断初始状态（round 0 时全为 gap）
  3. 从 EvolutionPlan 接收 Stage1 更新后的矩阵
  4. 提供查询接口供 Stage2 生成 Directive
"""

from __future__ import annotations

from core.datatypes import (
    CapabilityMatrix,
    CapabilityMatrixCell,
    CapabilityState,
    WorkerPatch,
)


class CapabilityTracker:
    """
    跨轮次的能力矩阵追踪器。

    内部存储格式：
        _matrix[workflow_name][worker_id] = CapabilityState

    对应论文变量：C^t（每 round 由 Stage1 更新）
    """

    def __init__(self, family_name: str, worker_ids: list[str]) -> None:
        self._family_name = family_name
        self._worker_ids = list(worker_ids)
        # {workflow: {worker_id: CapabilityState}}
        self._matrix: dict[str, dict[str, CapabilityState]] = {}
        self._current_round: int = 0

    # ------------------------------------------------------------------
    # 状态更新
    # ------------------------------------------------------------------

    def init_from_patches(self, patches: dict[str, WorkerPatch], round_idx: int) -> None:
        """
        Round 开始时，根据本轮各 worker 提交的 patch 初始化缺失的矩阵行。

        Args:
            patches:   {worker_id: WorkerPatch} — dict key 提供路由信息
            round_idx: 当前 round 序号

        规则：若某 (workflow, worker) 组合尚未出现在矩阵中，初始化为 GAP。
        这符合论文「未掌握就是 gap」的语义。
        """
        for wid, patch in patches.items():
            # 以 summary 前 50 字符作为 workflow 标识（patch 不再携带 task_name）
            task = wid  # fallback：用 worker_id 作行 key，实际 workflow 由 Stage1 更新
            if task not in self._matrix:
                self._matrix[task] = {}
            for worker in self._worker_ids:
                if worker not in self._matrix[task]:
                    self._matrix[task][worker] = CapabilityState.GAP
        self._current_round = round_idx

    def update_from_plan_dict(
        self,
        plan_matrix: dict[str, dict[str, str]],
        round_idx: int,
    ) -> None:
        """
        从 Stage1 LLM 返回的 capability_matrix 字典更新内部状态。

        plan_matrix 格式:
            {"workflow_name": {"worker_id": "covered|absorbing|broken|gap"}}
        """
        for workflow, workers in plan_matrix.items():
            if workflow not in self._matrix:
                self._matrix[workflow] = {}
            for worker_id, state_str in workers.items():
                try:
                    state = CapabilityState(state_str)
                except ValueError:
                    state = CapabilityState.GAP  # 未知状态降级为 gap
                self._matrix[workflow][worker_id] = state
        self._current_round = round_idx
        self._prune_placeholder_rows(plan_matrix)

    def _prune_placeholder_rows(self, plan_matrix: dict[str, dict[str, str]]) -> None:
        """
        清理 init_from_patches() 遗留的、以 worker_id 本身命名的占位 workflow 行。

        真实 workflow 名称不应与 worker_id 字符串重合，因此一旦 Stage1 成功产出
        plan_matrix（本方法被调用即代表 Stage1 未走 _fallback_plan() 降级路径），
        任何仍以 worker_id 命名的矩阵行都可判定为 init_from_patches() 留下的占位
        cruft，直接删除，避免其永久残留、污染 is_workflow_retired() /
        get_open_workflows_for_worker() 等按 workflow 名称聚合的统计结果。
        若 Stage1 本轮 plan_matrix 恰好显式提到了该 key（理论上不应发生），
        则保留，不做删除，避免误删有效数据。
        """
        for wid in self._worker_ids:
            if wid in self._matrix and wid not in plan_matrix:
                del self._matrix[wid]

    def mark_covered(self, workflow: str, worker_id: str, round_idx: int) -> None:
        """手动将某单元格置为 covered（供规则驱动路径使用）。"""
        if workflow not in self._matrix:
            self._matrix[workflow] = {}
        self._matrix[workflow][worker_id] = CapabilityState.COVERED

    def clear(self) -> None:
        """
        清空能力矩阵（A1 ablation 用）。

        对应 ablation_a1_no_capability_matrix.yaml：
            Stage1 看不到历史覆盖状态，只能依赖当前 patch 的 reward 决策，
            无法利用跨 worker、跨轮次的能力汇总信息。
        """
        self._matrix.clear()

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_state(
        self, workflow: str, worker_id: str
    ) -> CapabilityState:
        return (
            self._matrix
            .get(workflow, {})
            .get(worker_id, CapabilityState.GAP)
        )

    def get_open_workflows_for_worker(
        self, worker_id: str
    ) -> list[tuple[str, CapabilityState]]:
        """返回该 worker 所有非 covered 的 (workflow, state) 对，供 Directive 生成。"""
        result = []
        for workflow, workers in self._matrix.items():
            state = workers.get(worker_id, CapabilityState.GAP)
            if state != CapabilityState.COVERED:
                result.append((workflow, state))
        return result

    def is_workflow_retired(self, workflow: str) -> bool:
        """
        论文：'A workflow row is retired only when every client's cell becomes covered.'
        """
        workers = self._matrix.get(workflow, {})
        if not workers:
            return False
        return all(s == CapabilityState.COVERED for s in workers.values())

    def to_capability_matrix(self, round_idx: int | None = None) -> CapabilityMatrix:
        """导出为 Pydantic CapabilityMatrix 对象。"""
        cells: list[CapabilityMatrixCell] = []
        for workflow, workers in self._matrix.items():
            for worker_id, state in workers.items():
                cells.append(CapabilityMatrixCell(
                    task_workflow=workflow,
                    worker_id=worker_id,
                    state=state,
                    last_updated_round=round_idx or self._current_round,
                ))
        return CapabilityMatrix(
            round_idx=round_idx or self._current_round,
            family_name=self._family_name,
            cells=cells,
        )

    def to_dict(self) -> dict[str, dict[str, str]]:
        """导出为可序列化的字典（workflow → worker → state_str）。"""
        return {
            wf: {wid: state.value for wid, state in workers.items()}
            for wf, workers in self._matrix.items()
        }

    def summary_str(self) -> str:
        """生成人类可读的矩阵摘要，用于提示词中。"""
        lines = [f"Capability Matrix (Round {self._current_round}):"]
        for workflow, workers in sorted(self._matrix.items()):
            parts = ", ".join(
                f"{wid}={state.value}" for wid, state in sorted(workers.items())
            )
            retired = " [RETIRED]" if self.is_workflow_retired(workflow) else ""
            lines.append(f"  {workflow}: {parts}{retired}")
        return "\n".join(lines)
