import json
import sys

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.completion import CompletionAdmission
from pico.model_contract import ModelTurn
from pico.progress import ProgressController


def test_explicitly_incomplete_final_is_not_admitted():
    decision = CompletionAdmission.evaluate(
        ModelTurn(
            kind="final",
            text="Implementation is incomplete; the service update remains.",
            response_status="completed",
        )
    )

    assert decision.accepted is False
    assert decision.reason == "explicitly_incomplete_final"


def test_requested_validation_must_pass_before_auto_commit(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    agent = Pico(
        model_client=FakeModelClient(
            [
                '<tool>{"name":"write_file","args":{"path":"app.py","content":"VALUE = 2\\n"}}</tool>',
                "<final>Implemented the change.</final>",
            ]
        ),
        workspace=WorkspaceContext.build(source, repo_root_override=source),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
        max_steps=1,
    )

    answer = agent.ask("Change app.py and run the tests.")

    assert "authoritative verification" in answer
    assert agent.current_task_state.stop_reason == "validation_failed"
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_premature_final_returns_verification_feedback_then_can_complete(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    verify = {"name": "run_verification", "args": {
        "argv": [sys.executable, "-c", "from app import VALUE; assert VALUE == 2"],
    }}
    model = FakeModelClient([
        '<tool>{"name":"write_file","args":{"path":"app.py","content":"VALUE = 2\\n"}}</tool>',
        "<final>Implemented the change.</final>",
        "<tool>" + json.dumps(verify) + "</tool>",
        "<final>Implemented and verified the change.</final>",
    ])
    agent = Pico(
        model_client=model,
        workspace=WorkspaceContext.build(source, repo_root_override=source),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
        max_steps=4,
    )

    answer = agent.ask("Change app.py and run the tests.")

    assert answer == "Implemented and verified the change."
    assert "Runtime verification feedback" in model.prompts[2]
    assert "verification_required" in model.prompts[2]
    assert agent.current_task_state.tool_steps == 2
    assert agent.current_task_state.transaction_state == "COMMITTED"
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_shell_repository_read_is_not_an_exploration_policy_rejection():
    controller = ProgressController(max_steps=8)

    decision = controller.preflight(
        "run_shell",
        {"command": "cat app.py; echo ---; rg VALUE ."},
    )

    assert decision["allowed"] is True
