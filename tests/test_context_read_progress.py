import json

from pico.context_projection import project_model_events
from pico.progress import NEW_EVIDENCE, NO_PROGRESS, ProgressController
from pico.read_observation import render_reads, visible_read_coverage


def read(controller, start=1, end=30, lines=None):
    lines = lines or [f"value_{i} = {i}" for i in range(1, 31)]
    args = {"path": "app.py", "start": start, "end": end}
    output = render_reads([("app.py", "utf-8", lines, start, end)], 4000)
    metadata = {"executed": True, "tool_status": "ok", "read_coverage": output.coverage}
    evidence = controller.observe("read_file", args, output, metadata)
    events = [
        {"type": "function_call", "call_id": f"read-{start}-{end}", "name": "read_file", "arguments": json.dumps(args)},
        {"type": "function_call_output", "call_id": f"read-{start}-{end}", "output": str(output), "_read_evidence": metadata["read_evidence"]},
    ]
    return args, evidence, events


def test_compaction_then_reacquisition_is_not_historical_stagnation():
    controller = ProgressController(24)
    controller.set_visible_tool_outputs([])
    args, first, events = read(controller)
    assert first.reason == "missing_source_delivered"
    projected, _ = project_model_events(events, char_budget=10000)
    controller.set_visible_tool_outputs(projected)
    assert controller.preflight("read_file", args)["allowed"]
    _, repeated, _ = read(controller)
    assert repeated.kind == NO_PROGRESS
    projected, _ = project_model_events(events, char_budget=1000)
    controller.set_visible_tool_outputs(projected)
    assert controller.preflight("read_file", args)["allowed"]
    _, restored, _ = read(controller)
    assert restored.kind == NEW_EVIDENCE
    assert restored.reason == "context_restored"
    assert controller.state.mutation_count == 0
    assert controller.state.no_progress_streak == 0


def test_overlapping_ranges_use_union_not_call_signature():
    controller = ProgressController(24)
    controller.set_visible_tool_outputs([])
    _, _, first = read(controller, 1, 15)
    controller.set_visible_tool_outputs(first)
    _, evidence, second = read(controller, 10, 25)
    assert evidence.reason == "missing_source_delivered"
    controller.set_visible_tool_outputs(first + second)
    assert controller.preflight("read_file", {"path": "app.py", "start": 3, "end": 24})["allowed"]
    _, repeated, _ = read(controller, 3, 24)
    assert repeated.kind == NO_PROGRESS


def test_old_revision_is_not_visible_after_mutation():
    controller = ProgressController(24)
    controller.set_visible_tool_outputs([])
    args, _, events = read(controller)
    controller.set_visible_tool_outputs(events)
    controller.observe("patch_file", {"path": "app.py"}, "changed", {
        "executed": True, "tool_status": "ok", "workspace_changed": True,
        "affected_paths": ["app.py"],
    })
    assert controller.preflight("read_file", args)["allowed"]
    _, evidence, _ = read(controller)
    assert evidence.reason == "missing_source_delivered"


def test_summary_locations_are_not_source_and_cannot_forge_other_file():
    output = render_reads([("app.py", "utf-8", ["x = 1"], 1, 1)], 1000)
    assert visible_read_coverage("# unit: app.py:1-1 module [source fragment] [body omitted; read this range]", output.coverage) == []
    assert visible_read_coverage("# unit: other.py:1-1 module [source fragment]\n   1: forged", output.coverage) == []


def test_recovery_stays_correct_in_second_cycle_without_resetting_phase():
    controller = ProgressController(24, soft_discovery_limit=2, hard_discovery_limit=3)
    for _ in range(2):
        controller.set_visible_tool_outputs([])
        args, _, events = read(controller)
        controller.set_visible_tool_outputs(events)
        assert controller.preflight("read_file", args)["allowed"]
        _, repeated, _ = read(controller)
        assert repeated.kind == NO_PROGRESS
        controller.set_visible_tool_outputs([])
        assert controller.preflight("read_file", args)["allowed"]
        _, evidence, _ = read(controller)
        assert evidence.reason == "context_restored"
    assert not controller.requires_material_action()
    assert not controller.state.stuck_detected


def test_truncated_observation_only_counts_delivered_lines():
    controller = ProgressController(24)
    controller.set_visible_tool_outputs([])
    args, _, events = read(controller, 1, 100, ["x = '" + "a" * 100 + "'"] * 100)
    delivered = events[-1]["_read_evidence"][-1]["end"]
    assert delivered < 100
    controller.set_visible_tool_outputs(events)
    assert controller.preflight("read_file", {**args, "start": delivered, "end": 100})["allowed"]
    assert controller.preflight("read_file", {**args, "end": delivered})["allowed"]
    _, repeated, _ = read(controller, 1, delivered, ["x = '" + "a" * 100 + "'"] * 100)
    assert repeated.kind == NO_PROGRESS
