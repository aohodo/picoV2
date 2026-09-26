from pico.interaction_policy import build_interaction_contract
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


def test_apply_patch_rejects_entire_work_unit_when_one_path_is_protected(tmp_path):
    agent, _ = build_agent(tmp_path, [], max_steps=2)
    agent.begin_transaction()
    agent.current_interaction = {
        "mutation_allowed": True,
        "protected_paths": ["test_*.py"],
    }

    result = agent.execute_tool(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "solution.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                },
                {
                    "path": "test_solution.py",
                    "old_text": "assert True",
                    "new_text": "assert False",
                },
            ]
        },
    )

    assert result.metadata["tool_error_code"] == "scope_constraint"
    assert not result.metadata["executed"]
    assert (agent.root / "solution.py").read_text(encoding="utf-8") == "value = 1\n"
    assert "assert True" in (agent.root / "test_solution.py").read_text(encoding="utf-8")


def test_existing_read_only_acceptance_tests_do_not_require_a_new_test_artifact():
    prompt = (
        "请完成真实开发需求。仓库中已经加入本需求的验收测试。"
        "请先理解现有实现和测试，再修改生产代码；禁止修改、删除或绕过任何测试。"
        "完成后运行 mvn test，只有全部测试通过才能报告完成。"
    )

    contract = build_interaction_contract(prompt, "follow_repository")

    assert contract["mutation_allowed"] is True
    assert contract["validation_required"] is True
    assert contract["test_artifact_required"] is False
    assert "src/test/**" in contract["protected_paths"]


def test_explicit_request_to_add_tests_remains_a_delivery_obligation():
    for prompt in (
        "Implement the fix and add a regression test.",
        "修复问题并新增回归测试。",
        "修复问题并添加针对超时场景的测试用例。",
    ):
        contract = build_interaction_contract(prompt, "follow_repository")
        assert contract["test_artifact_required"] is True
