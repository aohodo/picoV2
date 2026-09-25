import json
import sys
from types import SimpleNamespace

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_manager import ContextManager
from pico.progress import ProgressController


def verification(controller, argv, output, *, status="error", changed=False, executed=True):
    controller.observe("run_verification", {"argv": argv}, output, {
        "executed": executed,
        "tool_status": status,
        "workspace_changed": changed,
        "affected_paths": ["service.py"] if changed else [],
    })


def context_manager(controller=None, *, ledger=None, history=None):
    agent = SimpleNamespace(
        progress_controller=controller,
        session={"history": history or [], "execution_ledger": ledger or {}},
        current_interaction={},
        prefix="Use current evidence.",
        invalidate_stale_memory=lambda: None,
        memory_text=lambda: "Memory: older request",
        render_checkpoint_text=lambda: "Task checkpoint: resume repair",
    )
    return ContextManager(agent, total_budget=1800, section_budgets={"history": 80})


def test_text_failure_survives_history_eviction_with_exact_argv_and_latest_request():
    controller = ProgressController(24)
    argv = ["pytest", "tests/test names.py", "-k", "a b"]
    diagnostic = "AssertionError: expected 18, got 17"
    verification(controller, argv, diagnostic)
    history = [{"role": "tool", "name": "run_verification", "args": {"argv": argv},
                "content": diagnostic}]
    history.extend({"role": "assistant", "content": "later observation " * 100}
                   for _ in range(12))
    manager = context_manager(controller, history=history)
    request = "Only repair the age check; preserve the public interface."

    prompt, metadata = manager.build(request)

    assert diagnostic not in manager._render_history_section(80).rendered
    assert diagnostic in prompt
    assert json.dumps(argv) in prompt
    assert prompt.endswith("Current user request:\n" + request)
    assert metadata["current_request"]["text"] == request


def test_restored_text_failure_survives_without_controller_or_history():
    controller = ProgressController(24)
    argv = ["pytest", "tests/test_service.py"]
    diagnostic = "AssertionError: persisted failure is still actionable"
    verification(controller, argv, diagnostic)
    restored = json.loads(json.dumps(controller.ledger.to_dict()))

    prompt, _ = context_manager(ledger=restored).build("Continue the repair.")

    assert diagnostic in prompt
    assert json.dumps(argv) in prompt
    assert prompt.endswith("Current user request:\nContinue the repair.")


def test_resumed_ask_sends_real_failure_to_text_model_after_history_eviction(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    state_root = tmp_path / "state"
    session_store = SessionStore(state_root / "sessions")
    options = {
        "workspace": WorkspaceContext.build(source, repo_root_override=source),
        "session_store": session_store,
        "state_root": state_root,
        "approval_policy": "auto",
        "commit_policy": "auto",
        "semantic_index": "off",
        "max_steps": 4,
    }
    agent = Pico(model_client=FakeModelClient([]), **options)
    agent.begin_transaction()
    agent.current_interaction = {"mutation_allowed": True, "validation_required": True}
    agent.progress_controller = ProgressController(4)
    agent.execute_tool("write_file", {"path": "app.py", "content": "VALUE = 2\n"})
    argv = [sys.executable, "-c", (
        "from pathlib import Path; assert Path('app.py').read_text() == 'VALUE = 3\\n'"
    )]
    failure = agent.execute_tool("run_verification", {"argv": argv})
    assert failure.metadata["executed"]
    assert failure.metadata["tool_status"] == "error"
    assert "AssertionError" in failure.content
    agent.interrupt_transaction()
    agent.session["history"] = []
    session_store.save(agent.session)

    model = FakeModelClient(["<final>The verification reported an assertion failure.</final>"])
    resumed = Pico(model_client=model, session=session_store.load(agent.session["id"]),
                   **options)
    request = "Explain the latest verification failure. Do not modify any files."

    resumed.ask(request)

    assert len(model.prompts) == 1
    assert "Runtime verification feedback:" in model.prompts[0]
    assert "AssertionError" in model.prompts[0]
    assert json.dumps(argv, ensure_ascii=False) in model.prompts[0]
    assert model.prompts[0].endswith("Current user request:\n" + request)
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.parametrize("status,changed,executed", [
    ("ok", False, True),
    ("error", False, True),
    ("ok", True, True),
    ("rejected", False, False),
])
def test_text_feedback_clears_only_after_matching_success(status, changed, executed):
    controller = ProgressController(24)
    argv = ["check", "a b"]
    diagnostic = "AssertionError: original check failed"
    verification(controller, argv, diagnostic)
    manager = context_manager(controller)
    # The display string is the same, but these are different processes.
    verification(controller, ["check", "a", "b"], "passed", status="ok")
    prompt, _ = manager.build("Repair the remaining failure.")
    assert diagnostic in prompt

    verification(controller, argv, "latest diagnostic", status=status,
                 changed=changed, executed=executed)
    prompt, _ = manager.build("Continue.")

    if status == "ok" and not changed and executed:
        assert "Runtime verification feedback:" not in prompt
        assert diagnostic not in prompt
    else:
        assert "Runtime verification feedback:" in prompt
        assert ("latest diagnostic" if executed else diagnostic) in prompt


@pytest.mark.parametrize("budget", [800, 1800, 12000])
def test_text_runtime_compaction_preserves_latest_failure_and_bounds_feedback(budget):
    controller = ProgressController(24)
    for index in range(12):
        verification(controller, ["check", str(index)],
                     f"Failure {index}: " + "detail " * 1000 + f" final error {index}")

    manager = context_manager(controller)
    manager.total_budget = budget
    prompt, _ = manager.build("Repair failure 11.")
    feedback = prompt.split("Runtime verification feedback:\n", 1)[1].split(
        "\n\nTask checkpoint:", 1
    )[0]

    assert len(prompt) <= manager.total_budget
    assert "Failure 11:" in feedback
    assert "final error 11" in feedback
    assert prompt.endswith("Current user request:\nRepair failure 11.")


def test_legacy_text_failure_keeps_argument_uncertainty_visible():
    legacy = {"unresolved_failures": [{
        "command": "check a b", "kind": "validation", "status": "error",
        "output": "Legacy AssertionError",
    }], "unresolved_failure_count": 1}

    prompt, _ = context_manager(ledger=legacy).build("Continue.")

    assert "Legacy AssertionError" in prompt
    assert "argument boundaries" in prompt
    assert '"argv"' not in prompt
