"""LangGraph orchestration for one Pico coding-agent run.

The graph owns phase transitions. Pico's domain services still own repository
evidence, tool execution, verification, and transactional delivery.
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class PicoAgentGraphState(TypedDict, total=False):
    """Shared state passed between explicit runtime phases."""

    user_message: str
    next_node: str
    final_answer: str
    run_started_at: float
    task_state: Any
    interaction: dict[str, Any]
    controller: Any
    native_mode: bool
    model_events: list[dict[str, Any]]
    projector: Any
    attempts: int
    max_attempts: int
    finalization_requested: bool
    stuck_final: str | None
    contract_failure_final: str | None
    contract_failures: int
    contract_failure_reason: str
    completion_failure_reason: str
    model_notice: str
    request_profile: str
    prompt_metadata_base: dict[str, Any]
    finalization_failures: int
    finalization_reason: str
    finalization_attempts: int
    max_finalization_attempts: int


class AgentGraphRuntime:
    """Compile and invoke Pico's explicit LangGraph state machine."""

    def __init__(self, loop):
        graph = StateGraph(PicoAgentGraphState)
        graph.add_node("bootstrap_runtime", loop._graph_bootstrap)
        graph.add_node("action_turn_runtime", loop._graph_action_turn)
        graph.add_node("finalization_turn_runtime", loop._graph_finalization_turn)
        graph.add_node("delivery_runtime", loop._graph_deliver)
        graph.add_node("stop_runtime", loop._graph_stop)

        graph.add_edge(START, "bootstrap_runtime")
        graph.add_conditional_edges(
            "bootstrap_runtime",
            self._next_node,
            {
                "action_turn_runtime": "action_turn_runtime",
                "stop_runtime": "stop_runtime",
            },
        )
        graph.add_conditional_edges(
            "action_turn_runtime",
            self._next_node,
            {
                "action_turn_runtime": "action_turn_runtime",
                "finalization_turn_runtime": "finalization_turn_runtime",
                "delivery_runtime": "delivery_runtime",
                "stop_runtime": "stop_runtime",
            },
        )
        graph.add_conditional_edges(
            "finalization_turn_runtime",
            self._next_node,
            {
                "finalization_turn_runtime": "finalization_turn_runtime",
                "delivery_runtime": "delivery_runtime",
                "stop_runtime": "stop_runtime",
            },
        )
        graph.add_edge("delivery_runtime", END)
        graph.add_edge("stop_runtime", END)
        self.graph = graph.compile()

    @staticmethod
    def _next_node(state):
        return state["next_node"]

    def invoke(self, user_message, recursion_limit):
        return self.graph.invoke(
            {"user_message": user_message},
            config={"recursion_limit": recursion_limit},
        )
