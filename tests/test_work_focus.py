import json

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_projection import _project_runtime_state
from pico.interaction_policy import build_interaction_contract, extract_referenced_paths
from pico.progress import ProgressController
from pico.working_set import build_initial_working_set, project_current_working_set


def _observe_read(controller, path):
    return controller.observe(
        "read_file",
        {"path": path, "start": 1, "end": 100},
        f"source from {path}",
        {"executed": True, "tool_status": "ok", "workspace_changed": False},
    )


def test_request_paths_are_language_agnostic_navigation_evidence():
    request = (
        "Update src/views/plan/index.vue and src/views/project/index.vue, create "
        "src/utils/status.js, but see https://example.com/spec.json and ../outside.py "
        "for context."
    )

    paths = extract_referenced_paths(request)
    contract = build_interaction_contract(request, "follow_repository")

    assert paths == [
        "src/views/plan/index.vue",
        "src/views/project/index.vue",
        "src/utils/status.js",
    ]
    assert contract["referenced_paths"] == paths


def test_work_focus_moves_from_named_targets_to_implementation_without_a_quota():
    controller = ProgressController(
        24,
        delivery_requirements={
            "mutation_allowed": True,
            "requested_existing_paths": ["plan.vue", "project.vue"],
            "requested_missing_paths": ["status.js"],
        },
    )

    initial = controller.work_focus_view()
    assert initial["phase"] == "inspect_explicit_targets"
    assert initial["open"]["unread_requested_paths"] == [
        "plan.vue",
        "project.vue",
    ]

    _observe_read(controller, "plan.vue")
    after_first = controller.work_focus_view()
    assert after_first["known"]["last_action"]["value"] == "frontier_reduced"
    assert after_first["open"]["unread_requested_paths"] == ["project.vue"]

    _observe_read(controller, "project.vue")
    ready = controller.work_focus_view()
    assert ready["phase"] == "implement"
    assert ready["open"]["unread_requested_paths"] == []
    assert ready["open"]["named_missing_paths"] == ["status.js"]
    assert ready["action_value_counts"] == {
        "frontier_reducing": 2,
        "evidence_expanding": 0,
        "no_progress": 0,
    }
    assert controller.preflight("search", {"pattern": "style", "path": "."})[
        "allowed"
    ]


def test_failure_and_unverified_change_take_priority_over_more_discovery():
    controller = ProgressController(
        24,
        delivery_requirements={
            "mutation_allowed": True,
            "requested_existing_paths": ["service.py"],
        },
    )
    _observe_read(controller, "service.py")
    controller.observe(
        "patch_file",
        {"path": "service.py"},
        "patched",
        {
            "executed": True,
            "tool_status": "ok",
            "workspace_changed": True,
            "affected_paths": ["service.py"],
        },
    )
    assert controller.work_focus_view()["phase"] == "complete_and_verify_changes"

    controller.observe(
        "run_verification",
        {"argv": ["pytest", "-q"]},
        "FAILED test_service.py::test_value - AssertionError",
        {
            "executed": True,
            "tool_status": "error",
            "workspace_changed": False,
            "validation": True,
        },
    )
    focus = controller.work_focus_view()
    assert focus["phase"] == "repair_failure"
    assert focus["known"]["last_action"]["value"] == "failure_feedback"
    assert focus["open"]["unresolved_failure_count"] == 1


def test_evidence_expansion_after_grounding_gets_decision_feedback_not_a_gate():
    controller = ProgressController(
        24,
        delivery_requirements={
            "mutation_allowed": True,
            "requested_existing_paths": ["service.py"],
        },
    )
    _observe_read(controller, "service.py")
    assert controller.consume_notice() == ""

    controller.observe(
        "search",
        {"pattern": "style", "path": "."},
        "other.py:1:class Example",
        {"executed": True, "tool_status": "ok", "workspace_changed": False},
    )

    assert controller.preflight("search", {"pattern": "dependency", "path": "."})[
        "allowed"
    ]
    assert "added context but did not close" in controller.consume_notice()
    assert controller.work_focus_view()["evidence_expansion_streak"] == 1

    controller.observe(
        "patch_file",
        {"path": "service.py"},
        "patched",
        {
            "executed": True,
            "tool_status": "ok",
            "workspace_changed": True,
            "affected_paths": ["service.py"],
        },
    )
    assert controller.work_focus_view()["evidence_expansion_streak"] == 0


def test_work_focus_survives_runtime_state_compaction():
    focus = {
        "phase": "implement",
        "priority": "Implement from current evidence.",
        "known": {"observed_requested_paths": ["a.py"]},
        "open": {"unread_requested_paths": []},
    }
    state = {
        "work_focus": focus,
        "ledger": {"observed_files": {str(index): "x" * 500 for index in range(100)}},
    }

    rendered, metadata = _project_runtime_state(state)

    assert metadata["runtime_state_compacted"]
    assert json.loads(rendered)["work_focus"] == focus


def test_initial_working_set_contains_target_and_late_import_evidence(tmp_path):
    source = tmp_path / "source"
    target = source / "src/views/plan/index.vue"
    target.parent.mkdir(parents=True)
    lines = [f"<!-- line {index} -->" for index in range(1, 151)]
    lines[28] = '<el-tag :type="status === 1 ? \'success\' : \'danger\'">'
    lines[129] = "<script setup>"
    lines[130] = "import { listPlan } from '@/api/plan'"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    working_set = build_initial_working_set(
        lambda path: source / path,
        "Extract the status success/danger rule.",
        ["src/views/plan/index.vue"],
    )

    assert "status === 1" in working_set.text
    assert "import { listPlan }" in working_set.text
    assert any(item["start"] == 1 for item in working_set.coverage)
    assert any(item["start"] == 131 for item in working_set.coverage)

    controller = ProgressController(
        24,
        delivery_requirements={
            "mutation_allowed": True,
            "requested_existing_paths": ["src/views/plan/index.vue"],
        },
    )
    controller.seed_working_set(working_set.coverage)
    controller.set_visible_tool_outputs([])
    delivered = working_set.coverage[0]
    assert not controller.preflight(
        "read_file",
        {
            "path": delivered["path"],
            "start": delivered["start"],
            "end": delivered["end"],
        },
    )["allowed"]


def test_initial_working_set_drops_only_source_made_stale_by_a_mutation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("first.py").write_text("FIRST = 'old'\n", encoding="utf-8")
    source.joinpath("second.py").write_text("SECOND = 'current'\n", encoding="utf-8")
    working_set = build_initial_working_set(
        lambda path: source / path,
        "Update first.py using second.py.",
        ["first.py", "second.py"],
    )
    controller = ProgressController(24)
    coverage = controller.seed_working_set(working_set.coverage)

    controller.observe(
        "patch_file",
        {"path": "first.py"},
        "patched",
        {
            "executed": True,
            "tool_status": "ok",
            "workspace_changed": True,
            "affected_paths": ["first.py"],
        },
    )
    projected = project_current_working_set(
        working_set.text,
        coverage,
        controller.ledger.path_revision,
    )

    assert "FIRST = 'old'" not in projected
    assert "SECOND = 'current'" in projected


def test_vue_paths_ground_first_turn_when_repository_graph_has_no_parser(tmp_path):
    source = tmp_path / "source"
    (source / "src/views/plan").mkdir(parents=True)
    (source / "src/views/project").mkdir(parents=True)
    (source / "src/utils").mkdir(parents=True)
    (source / "src/views/plan/index.vue").write_text("<template />\n", encoding="utf-8")
    (source / "src/views/project/index.vue").write_text("<template />\n", encoding="utf-8")
    client = FakeModelClient(["<final>Stopped after inspecting the work focus.</final>"])
    agent = Pico(
        model_client=client,
        workspace=WorkspaceContext.build(source, repo_root_override=source),
        session_store=SessionStore(tmp_path / "state/sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
    )

    agent.ask(
        "Update src/views/plan/index.vue and src/views/project/index.vue, then "
        "create src/utils/status.js."
    )

    prompt = client.prompts[0]
    assert '"phase": "implement"' in prompt
    assert '"observed_requested_paths": ["src/views/plan/index.vue", ' in prompt
    assert '"src/views/project/index.vue"]' in prompt
    assert '"named_missing_paths": ["src/utils/status.js"]' in prompt
    assert "Initial source working set:" in prompt
    assert "<template />" in prompt
