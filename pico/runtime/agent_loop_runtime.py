"""Top-level LangGraph coding-agent runtime."""

from .action_turn_runtime import ActionTurnRuntime
from .agent_graph_runtime import AgentGraphRuntime
from .bootstrap_runtime import BootstrapRuntime
from .delivery_runtime import DeliveryRuntime
from .finalization_runtime import FinalizationRuntime
from .model_turn_runtime import ModelTurnRuntime
from .repository_grounding_runtime import RepositoryGroundingRuntime
from .tool_turn_runtime import ToolTurnRuntime


class AgentLoopRuntime(
    BootstrapRuntime,
    ActionTurnRuntime,
    FinalizationRuntime,
    RepositoryGroundingRuntime,
    ModelTurnRuntime,
    ToolTurnRuntime,
    DeliveryRuntime,
):
    """Compose Pico domain runtimes behind one explicit LangGraph."""

    def __init__(self, agent):
        self.agent = agent

    def _run_active(self, user_message):
        recovery_budget = self.agent.model_execution_policy.max_recoveries
        max_attempts = self.agent.max_steps + recovery_budget + 1
        max_finalization_attempts = 1 + recovery_budget
        # The framework limit includes bootstrap and terminal nodes. Agent
        # actions remain bounded independently by Pico's execution policy.
        recursion_limit = max_attempts + max_finalization_attempts + 4
        result = AgentGraphRuntime(self).invoke(
            self.agent.redact_text(user_message),
            recursion_limit=recursion_limit,
        )
        return result["final_answer"]
