from pico.persistence.session_store import SessionStore
from pico.providers.clients import FakeModelClient
from pico.runtime import Pico
from pico.runtime.agent_graph_runtime import AgentGraphRuntime
from pico.runtime.agent_loop_runtime import AgentLoopRuntime
from pico.workspace import WorkspaceContext


def build_agent(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "README.md").write_text("# Demo\n", encoding="utf-8")
    state_root = tmp_path / "state"
    return Pico(
        model_client=FakeModelClient(["<final>Done.</final>"]),
        workspace=WorkspaceContext.build(
            workspace_root, repo_root_override=workspace_root
        ),
        session_store=SessionStore(state_root / "sessions"),
        state_root=state_root,
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
    )


def test_langgraph_owns_the_runtime_phase_transitions(tmp_path):
    agent = build_agent(tmp_path)
    runtime = AgentGraphRuntime(AgentLoopRuntime(agent))

    assert set(runtime.graph.get_graph().nodes) == {
        "__start__",
        "bootstrap_runtime",
        "action_turn_runtime",
        "finalization_turn_runtime",
        "delivery_runtime",
        "stop_runtime",
        "__end__",
    }

    assert agent.ask("Explain this repository without changing files") == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.last_prompt_metadata["orchestrator"] == "langgraph"
