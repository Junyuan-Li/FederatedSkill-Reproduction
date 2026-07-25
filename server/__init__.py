"""server package — 服务端演化层公开接口"""

from server.capability import CapabilityTracker
from server.memory import EvolutionMemoryStore
from server.planner import EvolutionPlanner
from server.merge import EvolutionExecutor
from server.evolution import FederatedServer
from server.logging import (
    DecisionEntry, DecisionLogger,
    write_task_memory, read_task_memory,
)

__all__ = [
    "CapabilityTracker",
    "EvolutionMemoryStore",
    "EvolutionPlanner",
    "EvolutionExecutor",
    "FederatedServer",
    # DECISIONS.md audit logging
    "DecisionEntry", "DecisionLogger",
    "write_task_memory", "read_task_memory",
]
