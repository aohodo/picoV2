import os

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.checkpoint import (
    MAX_CHECKPOINT_GOAL_CHARS,
    MAX_CHECKPOINTS,
    create_checkpoint,
)
from pico.features.memory import FILE_SUMMARY_LIMIT, NOTE_TAG_LIMIT, LayeredMemory
from pico.runtime import MAX_SESSION_HISTORY_ITEMS
from pico.state_root import WorkspaceState
from pico.task_state import TaskState
from pico.transactional_workspace import TransactionalWorkspace


def _agent(tmp_path):
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path, repo_root_override=tmp_path),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        semantic_index="off",
    )


def test_session_working_history_is_bounded(tmp_path):
    agent = _agent(tmp_path)

    for index in range(MAX_SESSION_HISTORY_ITEMS + 12):
        agent.record({"role": "user", "content": f"message-{index}"})

    assert len(agent.session["history"]) == MAX_SESSION_HISTORY_ITEMS
    assert agent.session["history"][0]["content"] == "message-12"


def test_checkpoint_working_set_is_bounded(tmp_path):
    agent = _agent(tmp_path)
    task_state = TaskState.create("task", "bounded checkpoints", run_id="run")

    for index in range(MAX_CHECKPOINTS + 8):
        checkpoint = create_checkpoint(
            agent,
            task_state,
            f"step-{index}",
            trigger="tool_executed",
        )

    checkpoints = agent.session["checkpoints"]
    assert len(checkpoints["items"]) == MAX_CHECKPOINTS
    assert checkpoints["current_id"] == checkpoint["checkpoint_id"]
    assert checkpoint["checkpoint_id"] in checkpoints["items"]


def test_failed_commit_restores_original_symlink(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.txt").write_text("target\n", encoding="utf-8")
    linked = source / "a-link.txt"
    try:
        linked.symlink_to("target.txt")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    (source / "z.txt").write_text("before\n", encoding="utf-8")
    tx = TransactionalWorkspace(source, tmp_path / "transactions").begin()
    staged_link = tx.execution_root / "a-link.txt"
    staged_link.unlink()
    staged_link.write_text("replacement\n", encoding="utf-8")
    (tx.execution_root / "z.txt").write_text("after\n", encoding="utf-8")
    tx.stage()
    assert tx.validate_commit() == []
    original_apply = tx._apply_change

    def fail_second_change(change):
        if change["path"] == "z.txt":
            raise OSError("injected commit failure")
        original_apply(change)

    monkeypatch.setattr(tx, "_apply_change", fail_second_change)

    with pytest.raises(OSError, match="injected commit failure"):
        tx.commit()

    assert linked.is_symlink()
    assert os.readlink(linked) == "target.txt"
    assert (source / "z.txt").read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize("terminal_state", ["COMMITTED", "DISCARDED"])
def test_resume_heals_stale_pointer_to_terminal_transaction(tmp_path, terminal_state):
    source = tmp_path / "source"
    source.mkdir()
    state_root = tmp_path / "state"
    store = SessionStore(state_root / "sessions")
    workspace_state = WorkspaceState(source, root=state_root).ensure()
    transaction = TransactionalWorkspace(source, workspace_state.transactions).begin()
    if terminal_state == "COMMITTED":
        transaction.stage()
        assert transaction.validate_commit() == []
        transaction.commit()
    else:
        transaction.discard()
    session = {
        "id": "stale-terminal-pointer",
        "history": [],
        "active_transaction_id": transaction.transaction_id,
    }
    store.save(session)

    agent = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(source, repo_root_override=source),
        session_store=store,
        session_id=session["id"],
        state_root=state_root,
        approval_policy="auto",
        semantic_index="off",
    )

    assert agent.transaction_context is None
    assert agent.root == source.resolve()
    assert "active_transaction_id" not in agent.session
    assert "active_transaction_id" not in store.load(session["id"])


def test_load_reconciles_completed_commit_journal_and_cleans_shadow(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.txt").write_text("before\n", encoding="utf-8")
    transactions = tmp_path / "transactions"
    transaction = TransactionalWorkspace(source, transactions).begin()
    (transaction.execution_root / "value.txt").write_text("after\n", encoding="utf-8")
    transaction.stage()
    assert transaction.validate_commit() == []
    changes = transaction.diff()
    for change in changes:
        transaction._apply_change(change)
    transaction._write_json(
        transaction.journal_path,
        {
            "state": "COMMITTED",
            "operations": changes,
            "completed_operations": [change["path"] for change in changes],
        },
    )
    transaction.state = "COMMITTING"
    transaction._persist()

    resumed = TransactionalWorkspace.load(
        source, transactions, transaction.transaction_id
    )

    assert resumed.state == "COMMITTED"
    assert (source / "value.txt").read_text(encoding="utf-8") == "after\n"
    assert not resumed.execution_root.exists()


def test_begin_transaction_discards_shadow_when_session_pointer_cannot_persist(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    original_save = agent.session_store.save

    def fail_active_pointer(session):
        if session.get("active_transaction_id"):
            raise OSError("injected session save failure")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_active_pointer)

    with pytest.raises(OSError, match="injected session save failure"):
        agent.begin_transaction()

    assert agent.transaction_context is None
    assert agent.root == tmp_path.resolve()
    assert "active_transaction_id" not in agent.session
    transaction_dirs = list(agent.transactions_root.glob("txn_*"))
    assert transaction_dirs
    assert all(not (path / "execution").exists() for path in transaction_dirs)


def test_working_memory_bounds_file_summaries_and_process_tags(tmp_path):
    memory = LayeredMemory(workspace_root=tmp_path)
    for index in range(FILE_SUMMARY_LIMIT + 10):
        path = f"file-{index}.py"
        (tmp_path / path).write_text(str(index), encoding="utf-8")
        memory.remember_file(path)
        memory.set_file_summary(path, f"summary-{index}")
    memory.append_note(
        "large tool change",
        tags=[f"path-{index}" for index in range(NOTE_TAG_LIMIT + 20)],
        source="run_shell",
        kind="process",
    )

    state = memory.to_dict()

    assert len(state["file_summaries"]) == FILE_SUMMARY_LIMIT
    assert len(state["episodic_notes"][-1]["tags"]) == NOTE_TAG_LIMIT


def test_task_state_bounds_telemetry_lists_without_losing_counts():
    task = TaskState.create("task", "change many files", run_id="run")
    affected = [f"file-{index}.py" for index in range(400)]
    task.record_tool_evidence(
        "run_shell",
        {"command": "generate"},
        {
            "workspace_changed": True,
            "affected_paths": affected,
            "tool_status": "ok",
        },
    )
    for index in range(40):
        task.record_tool_evidence(
            "run_verification",
            {"argv": ["python", "-m", "pytest", str(index)]},
            {"validation": True, "tool_status": "ok"},
        )

    assert task.changed_path_observations == 400
    assert len(task.changed_paths) == 256
    assert task.changed_paths_truncated is True
    assert task.validation_command_count == 40
    assert len(task.validation_commands) == 32


def test_session_history_and_checkpoints_store_bounded_working_projections(tmp_path):
    agent = _agent(tmp_path)
    large_code = "value = 1\n" * 20_000
    agent.record(
        {
            "role": "tool",
            "name": "write_file",
            "args": {"path": "large.py", "content": large_code},
            "content": "wrote large.py",
        }
    )
    task = TaskState.create("task", "x" * 20_000, run_id="run")
    checkpoint = create_checkpoint(
        agent,
        task,
        "x" * 20_000,
        trigger="tool_executed",
    )

    stored_args = agent.session["history"][-1]["args"]
    assert stored_args["content_chars"] == len(large_code)
    assert len(stored_args["content"]) < 1000
    assert len(checkpoint["current_goal"]) <= MAX_CHECKPOINT_GOAL_CHARS + 40
