from io import StringIO

from pico.cli import run_exit_code
from pico.progress_output import ConsoleProgressRenderer
from pico.providers.clients import FakeModelClient
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.task_state import TaskState
from pico.workspace import WorkspaceContext


def build_agent(tmp_path, outputs, max_steps=2):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (workspace_root / "test_sample.py").write_text(
        "from sample import value\n\ndef test_value():\n    assert value == 2\n",
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    agent = Pico(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(workspace_root, repo_root_override=workspace_root),
        session_store=SessionStore(state_root / "sessions"),
        state_root=state_root,
        approval_policy="auto",
        commit_policy="auto",
        max_steps=max_steps,
        max_new_tokens=256,
    )
    return agent, workspace_root


def test_budget_finalization_commits_validated_changes(tmp_path):
    agent, workspace_root = build_agent(
        tmp_path,
        [
            (
                '<tool>{"name":"patch_file","args":{"path":"sample.py",'
                '"old_text":"value = 1","new_text":"value = 2"}}</tool>'
            ),
            (
                '<tool>{"name":"run_verification","args":{"argv":["python","-m","pytest","-q"],'
                '"timeout":120}}</tool>'
            ),
            "<final>Fixed and verified.</final>",
        ],
    )

    answer = agent.ask("Fix the failing test and verify the result.")

    assert answer == "Fixed and verified."
    assert (workspace_root / "sample.py").read_text(encoding="utf-8") == "value = 2\n"
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.transaction_state == "COMMITTED"
    assert agent.last_run_outcome.successful
    assert agent.last_run_outcome.delivered_paths == ("sample.py",)
    assert run_exit_code(agent) == 0


def test_failed_validation_is_not_reported_or_delivered_as_success(tmp_path):
    agent, workspace_root = build_agent(
        tmp_path,
        [
            (
                '<tool>{"name":"patch_file","args":{"path":"sample.py",'
                '"old_text":"value = 1","new_text":"value = 3"}}</tool>'
            ),
            (
                '<tool>{"name":"run_verification","args":{"argv":["python","-m","pytest","-q"],'
                '"timeout":120}}</tool>'
            ),
            "<final>Fixed and verified.</final>",
        ],
    )

    answer = agent.ask("Fix the failing test and verify the result.")

    assert "did not complete" in answer
    assert (workspace_root / "sample.py").read_text(encoding="utf-8") == "value = 1\n"
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "validation_failed"
    assert agent.current_task_state.transaction_state == "INTERRUPTED"
    assert agent.last_run_outcome.status == "validation_failed"
    assert agent.last_run_outcome.delivered_paths == ()
    assert run_exit_code(agent) == 1


def test_task_state_roundtrip_preserves_authoritative_outcome(tmp_path):
    agent, _ = build_agent(
        tmp_path,
        [
            (
                '<tool>{"name":"patch_file","args":{"path":"sample.py",'
                '"old_text":"value = 1","new_text":"value = 2"}}</tool>'
            ),
            (
                '<tool>{"name":"run_verification","args":{"argv":["python","-m","pytest","-q"],'
                '"timeout":120}}</tool>'
            ),
            "<final>Done.</final>",
        ],
    )

    agent.ask("Fix the failing test and verify the result.")
    saved = agent.run_store.load_task_state(agent.current_task_state.run_id)

    assert saved["outcome_status"] == "committed"
    assert saved["delivered_paths"] == ["sample.py"]
    assert saved["exit_code"] == 0


def test_finished_progress_uses_transaction_delivery_scope_after_resume():
    stream = StringIO()
    renderer = ConsoleProgressRenderer(stream=stream, max_steps=24)
    task = TaskState.create("task-resumed", "Continue the interrupted task.")
    task.changed_paths = ["new-test.py"]
    task.staged_paths = ["a.py", "b.py", "new-test.py"]
    task.delivered_paths = ["a.py", "b.py", "new-test.py"]
    task.validation_status = "passed"

    renderer(
        "run_finished",
        {"stop_reason": "final_answer_returned", "run_duration_ms": 1250},
        task,
    )

    output = stream.getvalue()
    assert "delivered 3" in output
    assert "changed 1" not in output


def test_report_separates_run_delta_from_transaction_paths(tmp_path):
    agent, _ = build_agent(tmp_path, ["<final>No changes.</final>"], max_steps=1)
    task = TaskState.create("task-resumed", "Continue the interrupted task.")
    task.changed_paths = ["new-test.py"]
    task.staged_paths = ["a.py", "b.py", "new-test.py"]
    task.delivered_paths = ["a.py", "b.py", "new-test.py"]

    evidence = agent.build_report(task)["evidence"]

    assert evidence["changed_paths"] == ["new-test.py"]
    assert evidence["run_changed_paths"] == ["new-test.py"]
    assert evidence["transaction_paths"] == ["a.py", "b.py", "new-test.py"]


def test_shell_text_cannot_claim_authoritative_verification(tmp_path):
    agent, _ = build_agent(tmp_path, ["<final>No changes.</final>"], max_steps=1)
    agent.begin_transaction()

    result = agent.execute_tool(
        "run_shell",
        {"command": "python -c \"raise SystemExit(7)\"; echo masked", "timeout": 30},
    )

    assert result.metadata["validation"] is False
    assert agent.last_verification_succeeded is None


def test_direct_verification_preserves_process_exit_status(tmp_path):
    agent, _ = build_agent(tmp_path, ["<final>No changes.</final>"], max_steps=1)
    agent.begin_transaction()

    result = agent.execute_tool(
        "run_verification",
        {"argv": ["python", "-c", "raise SystemExit(7)"], "timeout": 30},
    )

    assert result.metadata["validation"] is True
    assert result.metadata["tool_status"] == "error"
    assert "exit_code: 7" in result.content
    assert agent.last_verification_succeeded is False


def test_change_after_passing_verification_is_not_delivered(tmp_path):
    agent, workspace_root = build_agent(
        tmp_path,
        [
            (
                '<tool>{"name":"patch_file","args":{"path":"sample.py",'
                '"old_text":"value = 1","new_text":"value = 2"}}</tool>'
            ),
            (
                '<tool>{"name":"run_verification","args":{"argv":["python","-m","pytest","-q"],'
                '"timeout":120}}</tool>'
            ),
            (
                '<tool>{"name":"patch_file","args":{"path":"sample.py",'
                '"old_text":"value = 2","new_text":"value = 3"}}</tool>'
            ),
            "<final>Done.</final>",
        ],
        max_steps=3,
    )

    answer = agent.ask("Fix, verify, and finish the change.")

    assert "validation failed" in answer
    assert (workspace_root / "sample.py").read_text(encoding="utf-8") == "value = 1\n"
    assert agent.current_task_state.validation_status == "stale"
    assert agent.last_run_outcome.status == "validation_failed"
