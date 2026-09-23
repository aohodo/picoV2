from pico.execution_policy import ModelExecutionPolicy
from pico.model_contract import ModelCapabilities, ModelTurn
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


class RecoveringNativeClient:
    supports_native_tools = True
    supports_prompt_cache = False

    def __init__(self):
        self.turns = [
            ModelTurn(
                kind="incomplete",
                response_status="incomplete",
                incomplete_reason="max_output_tokens",
            ),
            ModelTurn(
                kind="final",
                text="The project is Pico.",
                response_status="completed",
            ),
        ]
        self.last_completion_metadata = {}
        self.policies = []

    def complete_turn(self, **kwargs):
        self.policies.append(
            (kwargs["reasoning_effort"], kwargs["max_new_tokens"])
        )
        self.last_completion_metadata = {
            "response_status": self.turns[0].response_status,
            "output_tokens": 512,
        }
        return self.turns.pop(0)


def test_truncation_recovery_uses_observed_usage_and_reports_recovered(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Pico\n", encoding="utf-8")
    client = RecoveringNativeClient()
    agent = Pico(
        model_client=client,
        workspace=WorkspaceContext.build(workspace, repo_root_override=workspace),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
    )

    answer = agent.ask("Tell me the project name.")

    assert answer == "The project is Pico."
    assert client.policies == [("low", None), ("low", 1024)]
    assert agent.current_task_state.model_incomplete_count == 1
    assert agent.current_task_state.model_recovery_count == 1
    assert agent.current_task_state.completion_quality() == "recovered"


def test_invalid_protocol_recovery_does_not_increase_output_ceiling():
    policy = ModelExecutionPolicy("adaptive")

    turn = policy.for_turn(
        purpose="action",
        max_output_tokens=512,
        recovering=True,
        recovery_reason="function call arguments are invalid JSON",
    )

    assert turn.reasoning_effort == "low"
    assert turn.max_output_tokens == 512


def test_user_output_cap_is_a_ceiling_not_an_expandable_default():
    policy = ModelExecutionPolicy("adaptive")

    turn = policy.for_turn(
        purpose="action",
        max_output_tokens=512,
        recovering=True,
        recovery_reason="max_output_tokens",
        completion_metadata={"output_tokens": 512},
        recovery_attempt=1,
    )

    assert turn.max_output_tokens == 512
    assert turn.recovery_permitted is False


def test_provider_capabilities_bound_context_without_model_name_rules():
    policy = ModelExecutionPolicy("adaptive")
    capabilities = ModelCapabilities(
        context_window=10_000,
        max_output_tokens=2_000,
        supports_native_tools=True,
    )

    turn = policy.for_turn("action", capabilities=capabilities)

    assert turn.max_output_tokens is None
    assert turn.max_input_tokens == 8_000


class FinalizationRecoveryClient:
    supports_native_tools = True
    supports_prompt_cache = False
    capabilities = ModelCapabilities(supports_native_tools=True)

    def __init__(self):
        self.turns = [
            ModelTurn(
                kind="tool",
                tool_name="read_file",
                tool_args={"path": "README.md"},
                call_id="read-1",
                response_status="completed",
            ),
            ModelTurn(
                kind="incomplete",
                response_status="incomplete",
                incomplete_reason="max_output_tokens",
            ),
            ModelTurn(
                kind="final",
                text="The project is Pico.",
                response_status="completed",
            ),
        ]
        self.requests = []
        self.last_completion_metadata = {}

    def complete_turn(self, **kwargs):
        self.requests.append(kwargs)
        turn = self.turns.pop(0)
        self.last_completion_metadata = {
            "response_status": turn.response_status,
            "output_tokens": 700 if turn.kind == "incomplete" else 100,
            "request_input_chars": 2_000,
            "input_tokens": 500,
            "incomplete_reason": turn.incomplete_reason,
        }
        return turn


def test_finalization_recovers_in_place_without_reopening_tools(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Pico\n", encoding="utf-8")
    client = FinalizationRecoveryClient()
    agent = Pico(
        model_client=client,
        workspace=WorkspaceContext.build(workspace, repo_root_override=workspace),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        max_steps=1,
        semantic_index="off",
    )

    answer = agent.ask("Inspect the project name.")

    assert answer == "The project is Pico."
    assert client.requests[0]["tools"]
    assert client.requests[1]["tools"] == []
    assert client.requests[2]["tools"] == []
    assert [item["max_new_tokens"] for item in client.requests] == [
        None,
        None,
        1_400,
    ]
    assert agent.current_task_state.model_incomplete_count == 1
    assert agent.current_task_state.model_recovery_count == 1
