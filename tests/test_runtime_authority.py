import json
import os
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.completion import CompletionAdmission
from pico.context_projection import ContextProjector
from pico.execution import WorkspaceCommandRunner
from pico.interaction_policy import classify_interaction
from pico.model_contract import ModelTurn
from pico.progress import ProgressController
from pico.providers.clients import OpenAICompatibleModelClient, ProviderResponseError
from pico.session_store import SessionLoadError
from pico.state_root import workspace_identity


def make_agent(tmp_path, outputs=()):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(source, repo_root_override=source),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
        max_steps=12,
    )


def prepare_change(agent):
    agent.begin_transaction()
    agent.current_interaction = {
        "mutation_allowed": True,
        "validation_required": True,
    }
    agent.progress_controller = ProgressController(12)
    result = agent.execute_tool(
        "write_file", {"path": "app.py", "content": "VALUE = 2\n"}
    )
    assert result.metadata["workspace_changed"]


def verify(agent, program):
    return agent.execute_tool(
        "run_verification", {"argv": [sys.executable, "-c", program]}
    )


def test_unresolved_validation_failure_blocks_unrelated_success(tmp_path):
    agent = make_agent(tmp_path)
    prepare_change(agent)
    verify(agent, "raise SystemExit(1)")
    verify(agent, "print('unrelated success')")
    result = agent.finalize_transaction()
    assert result["state"] == "INTERRUPTED"
    assert result["conflicts"][0]["reason"] == "verification_failed"
    assert (agent.source_root / "app.py").read_text() == "VALUE = 1\n"


def test_diagnostic_probe_neither_blocks_nor_satisfies_delivery(tmp_path):
    agent = make_agent(tmp_path)
    prepare_change(agent)

    diagnostic = agent.execute_tool(
        "run_verification",
        {
            "argv": [sys.executable, "-c", "raise SystemExit(1)"],
            "purpose": "diagnostic",
        },
    )

    assert diagnostic.metadata["tool_status"] == "error"
    assert diagnostic.metadata["validation"] is False
    assert diagnostic.metadata["verification_purpose"] == "diagnostic"
    assert not agent.progress_controller.ledger.unresolved_failures
    assert agent.progress_controller.metrics()["validation_status"] == "not_run"
    assert agent.verification_failure_reason() == "verification_required"

    accepted = verify(agent, "print('real delivery check')")
    assert accepted.metadata["validation"] is True
    assert agent.finalize_transaction()["state"] == "COMMITTED"


@pytest.mark.parametrize("resume", [False, True])
def test_verifier_argv_boundaries_require_exact_repair_before_delivery(tmp_path, resume):
    agent = make_agent(tmp_path)
    prepare_change(agent)
    program = (
        "import json, sys; from pathlib import Path; ns = {}; "
        "exec(Path('app.py').read_text(), ns); "
        "print(json.dumps(sys.argv[1:])); "
        "assert sys.argv[1:] != ['a b'] or ns['VALUE'] == 3"
    )
    failed_argv = [sys.executable, "-c", program, "a b"]
    unrelated_argv = [sys.executable, "-c", program, "a", "b"]
    assert " ".join(failed_argv) == " ".join(unrelated_argv)

    failure = agent.execute_tool("run_verification", {"argv": failed_argv})
    assert failure.metadata["executed"]
    assert failure.metadata["tool_status"] == "error"
    assert '["a b"]' in failure.content

    if resume:
        agent.interrupt_transaction()
        agent = Pico(
            model_client=FakeModelClient([]),
            workspace=WorkspaceContext.build(
                agent.source_root, repo_root_override=agent.source_root,
            ),
            session_store=agent.session_store,
            session=agent.session_store.load(agent.session["id"]),
            state_root=tmp_path / "state",
            approval_policy="auto",
            commit_policy="auto",
            semantic_index="off",
        )
        assert agent.verification_failure_reason() == "verification_failed"
        agent.transaction_context.workspace.resume_editing()
        agent.current_interaction = {
            "mutation_allowed": True,
            "validation_required": True,
        }
        agent.progress_controller = ProgressController(
            12, ledger=agent.session["execution_ledger"],
        )

    success = agent.execute_tool("run_verification", {"argv": unrelated_argv})
    assert success.metadata["executed"]
    assert success.metadata["tool_status"] == "ok"
    assert '["a", "b"]' in success.content

    blocked = agent.finalize_transaction()

    assert blocked["state"] == "INTERRUPTED"
    assert blocked["conflicts"][0]["reason"] == "verification_failed"
    assert agent.progress_controller.ledger.unresolved_failure_count == 1
    assert (agent.source_root / "app.py").read_text() == "VALUE = 1\n"
    agent.transaction_context.workspace.resume_editing()
    agent.execute_tool("write_file", {"path": "app.py", "content": "VALUE = 3\n"})
    repaired = agent.execute_tool("run_verification", {"argv": failed_argv})
    assert repaired.metadata["executed"]
    assert repaired.metadata["tool_status"] == "ok"
    assert '["a b"]' in repaired.content
    assert not agent.progress_controller.ledger.unresolved_failures
    assert agent.progress_controller.ledger.unresolved_failure_count == 0
    assert agent.finalize_transaction()["state"] == "COMMITTED"
    assert (agent.source_root / "app.py").read_text() == "VALUE = 3\n"


def test_verifier_mutation_makes_validation_stale(tmp_path):
    agent = make_agent(tmp_path)
    prepare_change(agent)
    result = verify(
        agent,
        "from pathlib import Path; Path('app.py').write_text('VALUE = 3\\n')",
    )
    assert result.metadata["workspace_changed"]
    outcome = agent.finalize_transaction()
    assert outcome["state"] == "INTERRUPTED"
    assert outcome["conflicts"][0]["reason"] in {
        "verification_failed",
        "verification_stale",
    }


def test_delivery_uses_real_verification_not_test_filename_gate(tmp_path):
    agent = make_agent(tmp_path)
    agent.begin_transaction()
    agent.current_interaction = {
        "mutation_allowed": True,
        "validation_required": True,
        "test_artifact_required": True,
    }
    agent.progress_controller = ProgressController(12)
    agent.execute_tool(
        "write_file", {"path": "app.py", "content": "VALUE = 2\n"}
    )
    shadow = agent.root
    assert verify(agent, "from app import VALUE; assert VALUE == 2").metadata["tool_status"] == "ok"

    outcome = agent.finalize_transaction()

    assert outcome["state"] == "COMMITTED"
    assert (agent.source_root / "app.py").read_text() == "VALUE = 2\n"
    assert not shadow.exists()


def test_verification_feedback_does_not_close_shadow_repair_cycle(tmp_path):
    agent = make_agent(tmp_path)
    prepare_change(agent)

    assert agent.verification_failure_reason() == "verification_required"
    assert agent.transaction_context.workspace.state == "ACTIVE"
    assert verify(agent, "from app import VALUE; assert VALUE == 2").metadata["tool_status"] == "ok"
    assert agent.verification_failure_reason() == ""
    assert agent.transaction_context.workspace.state == "ACTIVE"


def test_bounded_failure_details_do_not_erase_unresolved_failure_authority(tmp_path):
    agent = make_agent(tmp_path)
    prepare_change(agent)
    verify(agent, "from app import VALUE; assert VALUE == 2")
    agent.progress_controller.ledger.unresolved_failures = []
    agent.progress_controller.ledger.unresolved_failure_count = 1

    assert agent.verification_failure_reason() == "verification_failed"
    assert agent.finalize_transaction()["state"] == "INTERRUPTED"


def test_review_refuses_shadow_changed_after_validation(tmp_path):
    agent = make_agent(tmp_path)
    agent.commit_policy = "review"
    prepare_change(agent)
    verify(agent, "from app import VALUE; assert VALUE == 2")
    assert agent.finalize_transaction()["state"] == "READY_FOR_REVIEW"
    (agent.root / "app.py").write_text("VALUE = 999\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="workspace_conflict"):
        agent.apply_transaction()

    assert agent.transaction_context.workspace.state == "CONFLICTED"
    assert (agent.source_root / "app.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize("state", ["COMMITTING", "COMMIT_FAILED", "RECOVERY_REQUIRED"])
def test_interruption_never_reopens_uncertain_source_commit(tmp_path, state):
    agent = make_agent(tmp_path)
    transaction = agent.begin_transaction().workspace
    transaction.state = state

    agent.interrupt_transaction("test_interrupt")

    assert transaction.state == state
    with pytest.raises(RuntimeError, match="cannot resume"):
        transaction.resume_editing()


def test_discard_preserves_uncertain_recovery_material(tmp_path):
    agent = make_agent(tmp_path)
    transaction = agent.begin_transaction().workspace
    transaction.state = "RECOVERY_REQUIRED"
    with pytest.raises(RuntimeError, match="cannot discard"):
        agent.discard_transaction()
    assert transaction.execution_root.exists()


def test_repository_listing_cannot_masquerade_as_verification(tmp_path):
    agent = make_agent(tmp_path)
    agent.begin_transaction()
    agent.current_interaction = {
        "mutation_allowed": True,
        "validation_required": True,
    }
    agent.progress_controller = ProgressController(12)

    result = agent.execute_tool("run_verification", {"argv": ["ls", "-R", "."]})

    assert result.metadata["tool_error_code"] == "invalid_arguments"
    assert result.metadata["executed"] is False
    assert not agent.progress_controller.ledger.validations


def test_conflicted_shadow_can_enter_a_second_repair_cycle(tmp_path):
    agent = make_agent(tmp_path)
    prepare_change(agent)
    command = "from pathlib import Path; raise SystemExit(not Path('ready.flag').exists())"
    verify(agent, command)
    assert agent.finalize_transaction()["state"] == "INTERRUPTED"
    agent.transaction_context.workspace.resume_editing()
    assert agent.transaction_context.workspace.state == "ACTIVE"
    agent.execute_tool("write_file", {"path": "ready.flag", "content": "ready\n"})
    assert verify(agent, command).metadata["tool_status"] == "ok"
    assert agent.finalize_transaction()["state"] == "COMMITTED"


@pytest.mark.parametrize("legacy_conflicted", [False, True])
def test_failed_budget_run_resumes_in_new_process_and_delivers_only_after_verification(
    tmp_path, legacy_conflicted,
):
    program = (
        "from pathlib import Path; ns={}; "
        "exec(Path('app.py').read_text(), ns); assert ns['VALUE'] == 3"
    )
    verification = '<tool>' + json.dumps({
        "name": "run_verification", "args": {"argv": [sys.executable, "-c", program]},
    }) + '</tool>'
    agent = make_agent(tmp_path, [
        '<tool>{"name":"write_file","args":{"path":"app.py","content":"VALUE = 2\\n"}}</tool>',
        verification,
        '<final>Done.</final>',
    ])
    agent.max_steps = 2
    agent.ask("Change app.py and verify the result.")
    assert agent.last_run_outcome.status == "validation_failed"
    transaction = agent.transaction_context.workspace
    assert transaction.state == "INTERRUPTED"
    assert (agent.source_root / "app.py").read_text() == "VALUE = 1\n"
    assert agent.session["execution_ledger"]["unresolved_failure_count"] == 1
    if legacy_conflicted:
        transaction.state = "CONFLICTED"
        transaction._persist(conflicts=[{"path": "", "reason": "verification_failed"}])

    resumed = Pico(
        model_client=FakeModelClient([
            '<tool>{"name":"write_file","args":{"path":"app.py","content":"VALUE = 3\\n"}}</tool>',
            verification,
            '<final>Fixed and verified.</final>',
        ]),
        workspace=WorkspaceContext.build(agent.source_root, repo_root_override=agent.source_root),
        session_store=agent.session_store,
        session=agent.session_store.load(agent.session["id"]),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
        max_steps=3,
    )
    assert resumed.transaction_context.workspace.state == "INTERRUPTED"
    assert resumed.verification_failure_reason() == "verification_failed"

    resumed.ask("continue")

    assert resumed.last_run_outcome.status == "committed"
    assert (agent.source_root / "app.py").read_text() == "VALUE = 3\n"
    assert not transaction.execution_root.exists()


@pytest.mark.parametrize("conflicts", [
    [{"path": "app.py", "reason": "source_changed"}],
    [{"path": "", "reason": "verification_failed"}, {"path": "app.py", "reason": "source_changed"}],
])
def test_legacy_source_conflicts_are_never_migrated_to_resumable(tmp_path, conflicts):
    from pico.transactional_workspace import TransactionalWorkspace

    agent = make_agent(tmp_path)
    transaction = agent.begin_transaction().workspace
    transaction.state = "CONFLICTED"
    transaction._persist(conflicts=conflicts)
    loaded = TransactionalWorkspace.load(
        agent.source_root, agent.transactions_root, transaction.transaction_id,
    )
    loaded.interrupt("attempted_resume")
    assert loaded.state == "CONFLICTED"
    with pytest.raises(RuntimeError, match="cannot resume"):
        loaded.resume_editing()


def test_read_only_interaction_rejects_every_risky_tool(tmp_path):
    agent = make_agent(tmp_path)
    agent.begin_transaction()
    agent.current_interaction = {
        "mutation_allowed": False,
        "validation_required": False,
    }
    for name, args in (
        ("write_file", {"path": "app.py", "content": "VALUE = 9\n"}),
        ("run_shell", {"command": "echo changed > app.py"}),
        ("run_verification", {"argv": [sys.executable, "-c", "print(1)"]}),
    ):
        result = agent.execute_tool(name, args)
        assert not result.metadata["executed"]
        assert result.metadata["tool_error_code"] == "interaction_read_only"


def test_read_only_followup_preserves_old_shadow_without_delivery(tmp_path):
    agent = make_agent(tmp_path)
    prepare_change(agent)
    agent.interrupt_transaction()
    resumed = Pico(
        model_client=FakeModelClient(["<final>The repository contains app.py.</final>"]),
        workspace=WorkspaceContext.build(agent.source_root, repo_root_override=agent.source_root),
        state_root=tmp_path / "state",
        session_store=agent.session_store,
        session=agent.session_store.load(agent.session["id"]),
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
    )
    resumed.ask("Explain this repository. Do not modify any files.")
    assert resumed.last_run_outcome.status == "completed_with_pending_changes"
    assert not resumed.last_run_outcome.delivered_paths
    assert (agent.source_root / "app.py").read_text() == "VALUE = 1\n"


def test_separate_clones_never_share_live_state_identity(tmp_path):
    identities = []
    for name in ("one", "two"):
        folder = tmp_path / name
        folder.mkdir()
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        subprocess.run(
            ["git", "-C", str(folder), "remote", "add", "origin", "https://example.invalid/shared.git"],
            check=True,
        )
        identities.append(workspace_identity(folder)[0])
    assert identities[0] != identities[1]


def provider():
    return OpenAICompatibleModelClient(
        "fake", "https://example.invalid/v1", "", None, 1
    )


def test_multiple_function_calls_are_preserved_as_one_auditable_batch():
    payload = {
        "status": "completed",
        "output": [
            {"type": "function_call", "name": "read_file", "call_id": "a", "arguments": '{"path":"a.py"}'},
            {"type": "function_call", "name": "read_file", "call_id": "b", "arguments": '{"path":"b.py"}'},
        ],
    }
    client = provider()
    with patch.object(client, "_request_responses", return_value=payload):
        result = client.complete_turn([], [], None)
    assert result.kind == "tool_batch"
    assert [(call.name, call.call_id) for call in result.tool_calls] == [
        ("read_file", "a"),
        ("read_file", "b"),
    ]


def test_nonobject_provider_json_uses_typed_error_boundary():
    class Response:
        def __init__(self):
            self.headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"[]"

    with (
        patch("urllib.request.urlopen", return_value=Response()),
        pytest.raises(ProviderResponseError) as error,
    ):
        provider().complete_turn([], [], None)
    assert error.value.code == "provider_invalid_envelope"


def test_completed_answer_may_describe_incomplete_input():
    decision = CompletionAdmission.evaluate(
        ModelTurn(
            kind="final",
            response_status="completed",
            text="Fixed handling of incomplete input; all tests passed.",
        )
    )
    assert decision.accepted


def test_global_read_only_instruction_overrides_mutation_word():
    intent = classify_interaction(
        "Explain how to modify the service; do not modify any files."
    )
    assert not intent.mutation_allowed


def test_timeout_terminates_descendants_holding_output_pipes(tmp_path):
    parent = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
        "time.sleep(20)"
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        WorkspaceCommandRunner._run_process(
            [sys.executable, "-c", parent], tmp_path, dict(os.environ), 1
        )
    assert time.monotonic() - started < 5


def test_retrieved_durable_fact_enters_native_request(tmp_path):
    agent = make_agent(tmp_path)
    fact = "Project convention: deployment target is AUDIT_NEPTUNE_CLUSTER."
    agent.memory.promote_durable([("project-conventions", fact)])
    items, _ = ContextProjector(agent).build(
        "What is the deployment target?", [], ProgressController(12)
    )
    assert "AUDIT_NEPTUNE_CLUSTER" in json.dumps(items)


def test_external_edit_removes_stale_source_from_native_request(tmp_path):
    agent = make_agent(tmp_path)
    agent.begin_transaction()
    controller = ProgressController(12)
    agent.progress_controller = controller
    args = {"path": "app.py", "start": 1, "end": 1}
    result = agent.execute_tool("read_file", args)
    events = [
        {"type": "function_call", "call_id": "read1", "name": "read_file", "arguments": json.dumps(args)},
        {"type": "function_call_output", "call_id": "read1", "output": result.content, "_read_evidence": result.metadata["read_evidence"]},
    ]
    agent.record_model_events(events, controller.ledger.to_dict())
    (agent.root / "app.py").write_text("VALUE = 999999\n", encoding="utf-8")
    restored = ProgressController(12, ledger=agent.session["execution_ledger"])
    items, _ = ContextProjector(agent).build(
        "Read current app.py", agent.session["model_events"], restored
    )
    assert "VALUE = 1" not in json.dumps(items)
    assert restored.preflight("read_file", args)["allowed"]


def test_external_edit_removes_stale_source_from_text_prompt(tmp_path):
    agent = make_agent(tmp_path)
    agent.begin_transaction()
    agent.progress_controller = ProgressController(12)
    args = {"path": "app.py", "start": 1, "end": 1}
    result = agent.execute_tool("read_file", args)
    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": args,
            "content": result.content,
            "read_evidence": result.metadata["read_evidence"],
        }
    )
    (agent.root / "app.py").write_text("VALUE = 999999\n", encoding="utf-8")
    prompt, _ = agent.context_manager.build("Read current app.py")
    assert "VALUE = 1" not in prompt


def test_session_id_cannot_escape_store_root(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    with pytest.raises(SessionLoadError):
        store.path("../outside")


def test_post_commit_failure_retains_delivered_paths(tmp_path, monkeypatch):
    agent = make_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"app.py","content":"VALUE = 4\\n"}}</tool>',
            "<final>Updated app.py.</final>",
        ],
    )

    def fail_restore(*args, **kwargs):
        raise OSError("injected failure after commit")

    monkeypatch.setattr(agent, "_restore_source_view", fail_restore)
    with pytest.raises(OSError):
        agent.ask("Change app.py to VALUE = 4.")
    assert agent.last_run_outcome.status == "committed_unconfirmed"
    assert agent.last_run_outcome.delivered_paths == ("app.py",)
