import json
from types import SimpleNamespace

from pico.context_projection import (
    ContextProjector,
    _project_runtime_state,
    discard_stale_read_groups,
    project_model_events,
)
from pico.read_observation import render_reads, visible_read_coverage
from pico.workspace import MAX_TOOL_OUTPUT


def test_unknown_context_capacity_keeps_configured_work_unit_not_tiny_window():
    agent = SimpleNamespace(model_client=SimpleNamespace(), max_steps=24)
    assert ContextProjector(agent).event_char_budget == 24 * MAX_TOOL_OUTPUT


def test_unresolved_failure_diagnostic_survives_runtime_compaction():
    state = {"ledger": {"observed_files": {str(i): "x" * 400 for i in range(100)},
                        "unresolved_failures": [{"command": "pytest test_age.py",
                           "status": "error", "output": "AssertionError: expected 18, got 17"}]}}
    rendered, metadata = _project_runtime_state(state)
    assert metadata["runtime_state_compacted"]
    assert "expected 18, got 17" in rendered
    assert len(rendered) <= 8000


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
