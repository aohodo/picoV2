from types import SimpleNamespace

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_projection import project_model_events
from pico.progress import ExecutionLedger
from pico.read_observation import render_reads
from pico.tools import tool_search, validate_tool


def test_batch_delivers_later_files_and_tracks_only_rendered_lines():
    result = render_reads(
        [
            ("large.py", "utf-8", ["x" * 80] * 500, 1, 500),
            ("executor.py", "utf-8", ["def execute(): pass"], 1, 500),
            ("provider.py", "utf-8", ["def complete_turn(): pass"], 1, 500),
        ],
        4000,
    )
    assert len(result) <= 4000
    assert "def execute(): pass" in result
    assert "def complete_turn(): pass" in result
    assert result.coverage[0]["end"] < 500
    ledger = ExecutionLedger()
    ledger.record_read("read_files", {}, result.coverage)
    assert ledger.observed_files["large.py"] == [(1, result.coverage[0]["end"])]
    assert ledger.observed_files["provider.py"] == [(1, 1)]


def test_overlong_line_is_not_recorded_as_read():
    result = render_reads([("a.py", "utf-8", ["x" * 9000], 1, 1)], 4000)
    ledger = ExecutionLedger()
    ledger.record_read(
        "read_file", {"path": "a.py", "start": 1, "end": 1}, result.coverage
    )
    assert result.coverage[0]["truncated"]
    assert not ledger.observed_files


def test_fragment_names_its_actual_enclosing_method():
    lines = [
        "class Client:",
        "    def complete_turn(self):",
        "        value = 1",
        "        return value",
    ]
    result = render_reads([("client.py", "utf-8", lines, 3, 4)], 4000)
    assert "Client.complete_turn:2-4" in result
    assert result.coverage[0]["start"] == 3
    assert result.coverage[0]["end"] == 4


def test_search_accepts_alternation_and_rejects_invalid_regex(tmp_path):
    (tmp_path / "a.py").write_text(
        "def execute(): pass\ndef complete_turn(): pass\n", encoding="utf-8"
    )
    context = SimpleNamespace(root=tmp_path, path=lambda p: tmp_path / p)
    result = tool_search(
        context, {"pattern": r"execute\(|complete_turn\(", "path": "."}
    )
    assert "a.py:1:" in result and "a.py:2:" in result
    with pytest.raises(ValueError, match="invalid regular expression"):
        validate_tool(context, "search", {"pattern": "["})


def test_latest_observation_survives_projection_when_budget_allows():
    observation = "a" * 1800 + "critical middle evidence" + "b" * 1800
    events = [
        {
            "type": "function_call",
            "call_id": "1",
            "name": "read_files",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "1", "output": observation},
    ]
    projected, _ = project_model_events(events)
    assert projected[1]["output"] == observation


def test_tool_boundary_preserves_coverage_and_repeat_identity(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.py").write_text(
        "x" * 80 + "\n" + ("y" * 80 + "\n") * 499, encoding="utf-8"
    )
    (source / "small.py").write_text("def execute(): pass\n", encoding="utf-8")
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(source, repo_root_override=source),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="never",
        semantic_index="off",
    )
    from pico.progress import ProgressController

    agent.progress_controller = ProgressController(max_steps=12, read_only=True)
    result = agent.execute_tool("read_files", {"paths": ["large.py", "small.py"]})
    assert result.metadata["tool_status"] == "ok"
    assert "def execute(): pass" in result.content
    delivered_end = result.metadata["read_coverage"][0]["end"]
    assert delivered_end < 500
    assert agent.progress_controller.ledger.observed_files["large.py"] == [
        (1, delivered_end)
    ]
    repeated = agent.execute_tool("read_files", {"paths": ["large.py", "small.py"]})
    assert repeated.metadata["tool_status"] == "ok"
    assert repeated.metadata["executed"] is True
    assert agent.progress_controller.state.no_progress_streak == 1
    continuation = agent.execute_tool(
        "read_file",
        {"path": "large.py", "start": delivered_end + 1, "end": delivered_end + 5},
    )
    assert continuation.metadata["tool_status"] == "ok"
