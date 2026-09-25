from pico.progress import ProgressController


def _ok_read(controller, name, args, content="evidence"):
    return controller.observe(
        name,
        args,
        content,
        {"tool_status": "ok", "executed": True, "workspace_changed": False},
    )


def test_high_confidence_grounding_advises_without_restricting_exploration():
    controller = ProgressController(
        max_steps=12,
        repository_evidence={
            "paths": ["src/service.py", "src/controller.py"],
            "confidence": "high",
            "semantic_backend": "multilspy",
            "semantic_status": "ok",
        },
    )

    assert controller.preflight("list_files", {"path": "."})["allowed"] is True
    _ok_read(controller, "list_files", {"path": "."}, "src")
    assert controller.preflight(
        "search", {"pattern": "service", "path": "."}
    )["allowed"] is True
    _ok_read(
        controller,
        "search",
        {"pattern": "service", "path": "."},
        "src/service.py:1:class Service",
    )

    decision = controller.preflight("search", {"pattern": "controller", "path": "."})

    assert decision["allowed"] is True
    assert "candidate files" in controller.consume_notice()
    assert controller.metrics()["broad_exploration_count"] == 2


def test_targeted_read_unlocks_additional_repository_search():
    controller = ProgressController(
        max_steps=12,
        repository_evidence={
            "paths": ["src/service.py"],
            "confidence": "high",
        },
    )
    _ok_read(controller, "list_files", {"path": "."}, "src")
    _ok_read(
        controller,
        "read_file",
        {"path": "src/service.py", "start": 1, "end": 100},
        "service code",
    )

    decision = controller.preflight("search", {"pattern": "caller", "path": "."})

    assert decision["allowed"] is True
    metrics = controller.metrics()
    assert metrics["targeted_read_count"] == 1
    assert metrics["evidence_hit_rate"] == 1.0


def test_low_confidence_grounding_does_not_block_broad_exploration():
    controller = ProgressController(
        max_steps=12,
        repository_evidence={"paths": ["maybe.py"], "confidence": "low"},
    )
    _ok_read(controller, "list_files", {"path": "."}, "files")

    assert controller.preflight(
        "search", {"pattern": "unknown", "path": "."}
    )["allowed"] is True


def test_legacy_discovery_limits_leave_tool_interface_stable():
    controller = ProgressController(
        max_steps=10,
        soft_discovery_limit=2,
        hard_discovery_limit=3,
    )
    for index in range(3):
        _ok_read(controller, "read_file", {"path": f"file{index}.py"}, str(index))

    available = {"read_file", "search", "run_shell", "write_file", "patch_file"}
    assert controller.admissible_tools(available) == available
    decision = controller.preflight("run_shell", {"command": "cat file0.py"})
    assert decision["allowed"] is True


def test_explicit_total_budget_does_not_impose_a_second_discovery_budget():
    controller = ProgressController(max_steps=6)

    for index in range(3):
        _ok_read(controller, "read_file", {"path": f"file{index}.py"}, str(index))

    assert controller.requires_material_action() is False
    assert controller.admissible_tools({"read_file", "write_file", "run_verification"}) == {
        "read_file", "write_file", "run_verification"
    }


def test_discovery_metrics_restart_after_material_changes():
    controller = ProgressController(
        max_steps=14,
        soft_discovery_limit=2,
        hard_discovery_limit=3,
    )
    _ok_read(controller, "read_file", {"path": "before.py"})
    controller.observe(
        "patch_file",
        {"path": "target.py"},
        "patched",
        {
            "tool_status": "ok",
            "executed": True,
            "workspace_changed": True,
            "affected_paths": ["target.py"],
        },
    )
    for index in range(3):
        _ok_read(controller, "read_file", {"path": f"after{index}.py"}, str(index))

    assert controller.requires_material_action() is False
    assert controller.state.discovery_streak == 3
    assert controller.admissible_tools({"search", "patch_file"}) == {"search", "patch_file"}
