import json
from types import SimpleNamespace

from pico.context_projection import (
    DEFAULT_EVENT_CHAR_BUDGET,
    ContextProjector,
    _head_tail,
    _project_runtime_state,
    discard_stale_read_groups,
    project_model_events,
)
from pico.features import memory as memorylib
from pico.read_observation import render_reads, visible_read_coverage


def test_compaction_marker_cannot_exceed_tiny_remaining_budget():
    for budget in (0, 1, 10, 40, 100):
        assert len(_head_tail("diagnostic" * 100, budget)) <= budget


def test_unknown_context_capacity_uses_working_set_not_step_count():
    short = SimpleNamespace(model_client=SimpleNamespace(), max_steps=4)
    long = SimpleNamespace(model_client=SimpleNamespace(), max_steps=100)

    assert ContextProjector(short).event_char_budget == DEFAULT_EVENT_CHAR_BUDGET
    assert ContextProjector(long).event_char_budget == DEFAULT_EVENT_CHAR_BUDGET


def test_instructions_include_authoritative_execution_environment():
    agent = SimpleNamespace(
        model_client=SimpleNamespace(),
        approval_policy="auto",
        read_only=False,
        execution_profile_view=lambda: {
            "host_os": "Windows",
            "dialect": "bash",
            "path_style": "windows",
            "python_command": "python",
            "available_commands": ["git", "python", "java", "mvn"],
        },
        workspace=SimpleNamespace(text=lambda: "Workspace: test"),
    )

    instructions = ContextProjector(agent).instructions()

    assert "host OS=Windows" in instructions
    assert "shell dialect=bash" in instructions
    assert "path style=windows" in instructions
    assert "Python command=python" in instructions
    assert "git, python, java, mvn" in instructions
    assert "do not assume an unlisted" in instructions.lower()
    assert "python3 exists" in instructions.lower()


def test_unresolved_failure_diagnostic_survives_runtime_compaction():
    state = {"ledger": {"observed_files": {str(i): "x" * 400 for i in range(100)},
                        "unresolved_failures": [{"command": "pytest test_age.py",
                           "status": "error", "output": "AssertionError: expected 18, got 17"}]}}
    rendered, metadata = _project_runtime_state(state)
    assert metadata["runtime_state_compacted"]
    assert "expected 18, got 17" in rendered
    assert len(rendered) <= 8000


def test_verification_arguments_stay_bounded_without_losing_failure_diagnostic():
    argv = ["python", "-c", "assert value == 42; " * 3000, "a b"]
    failure = {"argv": argv, "command": " ".join(argv), "kind": "validation",
               "status": "error", "output": "AssertionError: expected 42, got 17"}
    state = {"ledger": {"validations": [failure], "unresolved_failures": [failure]}}

    rendered, metadata = _project_runtime_state(state)

    assert metadata["runtime_state_compacted"]
    assert len(rendered) <= 8000
    assert "expected 42, got 17" in rendered
    assert failure["argv"] == argv
    assert state["ledger"]["unresolved_failures"][0]["output"] == failure["output"]


def test_compact_failure_display_preserves_argument_boundaries():
    argv = ["check", "a b"]
    state = {"ledger": {
        "observed_files": {str(i): "x" * 400 for i in range(100)},
        "unresolved_failures": [{"argv": argv, "command": "check a b",
                                 "status": "error", "output": "failed"}],
    }}

    rendered, _ = _project_runtime_state(state)

    assert json.loads(json.loads(rendered)["recent_failures"][0]["command"]) == argv


def test_short_observations_are_not_evicted_by_call_count():
    events = []
    for index in range(20):
        events.extend([
            {"type": "function_call", "call_id": str(index), "name": "search",
             "arguments": json.dumps({"pattern": str(index)})},
            {"type": "function_call_output", "call_id": str(index),
             "output": f"app.py:{index + 1}: useful evidence"},
        ])
    projected, metadata = project_model_events(events, char_budget=12000)
    assert projected == events
    assert metadata["dropped_event_count"] == 0


def test_failed_read_is_feedback_not_stale_source(tmp_path):
    events = [
        {"type": "function_call", "call_id": "failed", "name": "read_file",
         "arguments": '{"path":"missing.py"}'},
        {"type": "function_call_output", "call_id": "failed",
         "output": "error: invalid arguments: path is not a file"},
    ]
    assert discard_stale_read_groups(events, tmp_path) == events


def test_changed_file_does_not_evict_unchanged_sibling_from_batched_read(tmp_path):
    (tmp_path / "changed.py").write_text("old = 1\n", encoding="utf-8")
    (tmp_path / "stable.py").write_text("stable = 1\n", encoding="utf-8")
    observation = render_reads(
        [
            ("changed.py", "utf-8", ["old = 1"], 1, 1),
            ("stable.py", "utf-8", ["stable = 1"], 1, 1),
        ],
        4000,
    )
    evidence = [
        {**item, "freshness": memorylib.file_freshness(item["path"], tmp_path)}
        for item in observation.coverage
    ]
    events = [
        {"type": "function_call", "call_id": "batch", "name": "read_files",
         "arguments": json.dumps({"paths": ["changed.py", "stable.py"]})},
        {"type": "function_call_output", "call_id": "batch",
         "output": str(observation), "_read_evidence": evidence},
    ]
    (tmp_path / "changed.py").write_text("new = 2\n", encoding="utf-8")

    retained = discard_stale_read_groups(events, tmp_path)

    assert len(retained) == 2
    assert "stable = 1" in retained[-1]["output"]
    assert "old = 1" not in retained[-1]["output"]
    assert [item["path"] for item in retained[-1]["_read_evidence"]] == ["stable.py"]


def test_small_module_is_delivered_not_crowded_out_by_navigation_markers():
    path = "src/main/code/site/repository/site_config_repository.py"
    lines = ["class Repository:"]
    for index in range(20):
        lines.extend([f"    def method_{index}(self):", f"        return {index}", ""])
    observation = render_reads([(path, "utf-8", lines, 1, 200)], 4000)
    assert observation.coverage[0]["end"] == len(lines)
    assert not observation.coverage[0]["truncated"]
    assert observation.count("# unit:") == 1
    assert visible_read_coverage(observation, observation.coverage)[0]["end"] == len(lines)


def test_compaction_does_not_mark_omitted_source_as_delivered():
    path = "large.py"
    observation = render_reads([(path, "utf-8", ["x = 1"] * 200, 1, 200)], 4000)
    events = [
        {"type": "function_call", "call_id": "large", "name": "read_file",
         "arguments": json.dumps({"path": path})},
        {"type": "function_call_output", "call_id": "large",
         "output": str(observation), "_read_evidence": observation.coverage},
    ]
    projected, metadata = project_model_events(events, char_budget=1500)
    assert projected[-1]["_read_evidence"] == []
    assert metadata["projected_event_chars"] <= 1500


def test_latest_current_source_survives_tool_recency_compaction():
    path = "src/service.py"
    observation = render_reads(
        [(path, "utf-8", [f"VALUE_{index} = {index}" for index in range(120)], 1, 120)],
        8000,
    )
    events = [
        {"type": "function_call", "call_id": "source", "name": "read_file",
         "arguments": json.dumps({"path": path})},
        {"type": "function_call_output", "call_id": "source",
         "output": str(observation), "_read_evidence": observation.coverage},
    ]
    for index in range(6):
        events.extend([
            {"type": "function_call", "call_id": f"later-{index}", "name": "search",
             "arguments": json.dumps({"pattern": str(index)})},
            {"type": "function_call_output", "call_id": f"later-{index}",
             "output": f"result-{index}"},
        ])

    projected, metadata = project_model_events(events)

    source_output = next(
        item for item in projected
        if item.get("type") == "function_call_output" and item.get("call_id") == "source"
    )
    assert "VALUE_119 = 119" in source_output["output"]
    assert source_output["_read_evidence"][0]["end"] == 120
    assert metadata["working_set_source_groups"] == 1
