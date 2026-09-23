from pico.providers.clients import FakeModelClient
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


def build_agent(tmp_path, outputs, max_steps):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "solution.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "test_solution.py").write_text(
        "def test_placeholder():\n    assert True\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(workspace, repo_root_override=workspace),
        session_store=SessionStore(state / "sessions"),
        state_root=state,
        approval_policy="auto",
        commit_policy="auto",
        max_steps=max_steps,
    ), workspace


def test_explicit_test_constraint_rejects_file_tool_and_allows_solution(tmp_path):
    agent, workspace = build_agent(
        tmp_path,
        [
            (
                '<tool>{"name":"patch_file","args":{"path":"test_solution.py",'
                '"old_text":"assert True","new_text":"assert False"}}</tool>'
            ),
            (
                '<tool>{"name":"patch_file","args":{"path":"solution.py",'
                '"old_text":"value = 1","new_text":"value = 2"}}</tool>'
            ),
            '<tool>{"name":"run_verification","args":{"argv":["python","-m","pytest","-q"]}}</tool>',
            "<final>Implemented without changing tests.</final>",
        ],
        max_steps=2,
    )

    answer = agent.ask("Implement the fix in solution.py; do not modify tests.")

    assert answer == "Implemented without changing tests."
    assert (workspace / "solution.py").read_text(encoding="utf-8") == "value = 2\n"
    assert "assert True" in (workspace / "test_solution.py").read_text(encoding="utf-8")
    assert agent.last_run_outcome.successful


def test_shell_side_effect_on_protected_test_is_blocked_at_commit(tmp_path):
    agent, workspace = build_agent(
        tmp_path,
        [
            (
                '<tool>{"name":"run_shell","args":{"command":"python -c \\\"from pathlib import Path; '
                "Path('test_solution.py').write_text('assert False\\\\n', encoding='utf-8')\\\"\"}}</tool>"
            ),
            "<final>Done.</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Implement the fix; do not modify tests.")

    assert "no-modification constraint" in answer
    assert "assert True" in (workspace / "test_solution.py").read_text(encoding="utf-8")
    assert agent.last_run_outcome.status == "failed"
    assert agent.current_task_state.stop_reason == "scope_constraint_violation"
