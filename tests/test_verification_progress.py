import json

import pytest

from pico.progress import ProgressController


def observe(controller, name, *, changed=False, status="ok"):
    return controller.observe(name, {"path": "service.py", "argv": ["pytest"]}, "result", {
        "tool_status": status, "executed": True, "workspace_changed": changed,
        "affected_paths": ["service.py"] if changed else [],
    })


def repeated_reads():
    controller = ProgressController(24, soft_discovery_limit=2, hard_discovery_limit=3)
    for _ in range(3):
        observe(controller, "read_file")
    return controller


def test_repeated_reads_keep_verification_available():
    controller = repeated_reads()
    assert "run_verification" in controller.admissible_tools({"run_verification"})
    assert controller.preflight("run_verification", {"argv": ["pytest"]})["allowed"]


def test_mutation_resets_discovery_metrics_without_hiding_tools():
    controller = repeated_reads()
    observe(controller, "patch_file", changed=True)
    assert not controller.requires_material_action()
    assert controller.state.discovery_streak == 0
    assert controller.state.steps_since_material_progress == 0
    assert controller.admissible_tools({"list_files", "read_file"}) == {"list_files", "read_file"}


def test_repeated_reads_do_not_hide_source_or_discovery_tools():
    controller = repeated_reads()
    decision = controller.preflight(
        "read_file", {"path": "new_dependency.py", "start": 1, "end": 80}
    )
    assert decision["allowed"]
    assert "list_files" in controller.admissible_tools({"list_files"})
    assert "read_file" in controller.admissible_tools({"read_file"})
    observe(controller, "patch_file", changed=True)
    assert not controller.requires_material_action()


def test_verification_feedback_supports_a_second_repair_cycle():
    controller = repeated_reads()
    observe(controller, "write_file", changed=True)
    observe(controller, "run_verification", status="error")
    assert not controller.requires_material_action()
    assert controller.ledger.unverified_changes
    for _ in range(3):
        observe(controller, "read_file")
    assert not controller.requires_material_action()
    assert controller.metrics()["validation_status"] == "failed"
    observe(controller, "run_verification")
    assert not controller.ledger.unverified_changes
    assert not controller.ledger.unresolved_failures


def test_budget_does_not_hide_tools_and_pending_verification_remains_visible():
    controller = ProgressController(24)
    observe(controller, "write_file", changed=True)
    controller.set_remaining_steps(1)
    available = {"read_file", "write_file", "run_verification"}
    assert controller.admissible_tools(available) == available
    assert controller.preflight("write_file", {"path": "service.py"})["allowed"]
    assert controller.metrics()["validation_status"] == "not_run"
    assert controller.ledger.unverified_changes == {"service.py"}


def test_requested_test_artifact_does_not_replace_verification_or_hide_tools():
    controller = ProgressController(
        24, delivery_requirements={"test_artifact_required": True}
    )
    observe(controller, "write_file", changed=True)
    controller.set_remaining_steps(2)
    available = {"read_file", "write_file", "patch_file", "run_verification"}
    assert controller.admissible_tools(available) == available

    controller.observe(
        "write_file",
        {"path": "tests/test_service.py"},
        "wrote test",
        {
            "tool_status": "ok",
            "executed": True,
            "workspace_changed": True,
            "affected_paths": ["tests/test_service.py"],
        },
    )
    controller.set_remaining_steps(1)
    assert controller.admissible_tools(available) == available
    assert controller.metrics()["validation_status"] == "not_run"
    assert controller.ledger.unverified_changes == {"service.py", "tests/test_service.py"}


def test_missing_requested_test_artifact_is_not_a_runtime_tool_restriction():
    controller = ProgressController(
        24,
        soft_discovery_limit=2,
        hard_discovery_limit=3,
        delivery_requirements={"test_artifact_required": True},
    )
    observe(controller, "write_file", changed=True)
    for _ in range(3):
        observe(controller, "read_file")

    assert controller.state.intervention_level == "SOFT_INTERVENTION"
    available = {"read_file", "write_file", "patch_file", "run_verification"}
    assert controller.admissible_tools(available) == available
    assert controller.ledger.unverified_changes == {"service.py"}
    assert not controller.state.stuck_detected


def test_verifier_that_modifies_files_does_not_clear_pending_work():
    controller = ProgressController(24)
    observe(controller, "run_verification", changed=True)
    assert controller.ledger.unverified_changes


def test_passing_one_command_does_not_erase_other_failed_checks():
    controller = ProgressController(24)
    controller.observe("run_verification", {"argv": ["other-test"]}, "failed", {
        "tool_status": "error", "executed": True, "workspace_changed": False,
    })
    observe(controller, "run_verification")
    assert controller.ledger.unresolved_failures[0]["command"] == "other-test"


def test_diagnostic_verification_is_audited_without_failure_authority():
    controller = ProgressController(24)
    controller.observe(
        "run_verification",
        {"argv": ["python", "-c", "raise SystemExit(1)"], "purpose": "diagnostic"},
        "probe failed",
        {
            "tool_status": "error",
            "executed": True,
            "workspace_changed": False,
            "validation": False,
            "verification_purpose": "diagnostic",
        },
    )

    assert not controller.ledger.unresolved_failures
    assert controller.ledger.unresolved_failure_count == 0
    assert controller.ledger.validations[-1]["kind"] == "diagnostic"
    assert controller.metrics()["validation_status"] == "not_required"


@pytest.mark.parametrize("failed_argv,other_argv", [
    (["check", "a b"], ["check", "a", "b"]),
    (["check", '"a b"'], ["check", '"a', 'b"']),
    (["check", "中文 目录", "🙂"], ["check", "中文", "目录 🙂"]),
    (["check", "a\tb c"], ["check", "a\tb", "c"]),
])
def test_argument_boundaries_distinguish_failed_verifications(failed_argv, other_argv):
    assert " ".join(failed_argv) == " ".join(other_argv)
    controller = ProgressController(24)
    metadata = {"tool_status": "error", "executed": True, "workspace_changed": False}
    for argv in (failed_argv, other_argv):
        controller.observe("run_verification", {"argv": argv}, "assertion failed", metadata)

    assert controller.ledger.unresolved_failure_count == 2
    resumed = ProgressController(24, ledger=json.loads(json.dumps(controller.ledger.to_dict())))
    resumed.observe("run_verification", {"argv": other_argv}, "passed", {
        **metadata, "tool_status": "ok",
    })
    assert resumed.ledger.unresolved_failure_count == 1
    assert resumed.ledger.unresolved_failures[0]["argv"] == failed_argv
    assert resumed.metrics()["validation_status"] == "failed"

    resumed.observe("run_verification", {"argv": failed_argv}, "passed", {
        **metadata, "tool_status": "ok",
    })
    assert not resumed.ledger.unresolved_failures
    assert resumed.ledger.unresolved_failure_count == 0


def test_verification_identity_copies_arguments_and_ignores_timeout():
    controller = ProgressController(24)
    argv = ["pytest", "tests/test_service.py"]
    controller.observe("run_verification", {"argv": argv, "timeout": 1}, "timed out", {
        "tool_status": "error", "executed": True, "workspace_changed": False,
    })
    argv.append("--changed-after-recording")
    assert controller.ledger.unresolved_failures[0]["argv"] == ["pytest", "tests/test_service.py"]
    controller.observe("run_verification", {
        "argv": ["pytest", "tests/test_service.py"], "timeout": 120,
    }, "passed", {"tool_status": "ok", "executed": True, "workspace_changed": False})
    assert not controller.ledger.unresolved_failures


def test_legacy_failure_without_arguments_is_not_resolved_by_guessing():
    controller = ProgressController(24, ledger={
        "unresolved_failures": [{
            "kind": "validation", "command": "check a b", "status": "error",
            "output": "legacy failure: parameter boundaries were not saved",
        }],
        "unresolved_failure_count": 1,
    })
    for argv in (["check", "a", "b"], ["check", "a b"]):
        controller.observe("run_verification", {"argv": argv}, "passed", {
            "tool_status": "ok", "executed": True, "workspace_changed": False,
        })
    assert controller.metrics()["validation_status"] == "failed"
    assert controller.ledger.unresolved_failure_count == 1
    assert "legacy failure" in controller.ledger.unresolved_failures[0]["output"]


def test_repeated_failure_then_success_does_not_leave_phantom_obligation():
    controller = ProgressController(100)
    for _ in range(20):
        observe(controller, "run_verification", status="error")
    assert controller.ledger.unresolved_failure_count == 1
    assert len(controller.ledger.unresolved_failures) == 1

    observe(controller, "run_verification")

    assert controller.ledger.unresolved_failure_count == 0
    assert not controller.ledger.unresolved_failures
    assert controller.metrics()["validation_status"] == "passed"


def test_failure_authority_survives_bounded_projection_and_resume():
    controller = ProgressController(100)
    for index in range(20):
        controller.observe("run_verification", {"argv": [f"test-{index}"]}, "failed", {
            "tool_status": "error", "executed": True, "workspace_changed": False,
        })
    assert len(controller.ledger.view()["unresolved_failures"]) == 12
    resumed = ProgressController(100, ledger=controller.ledger.to_dict())
    assert len(resumed.ledger.unresolved_failures) == 20
    for index in range(20):
        resumed.observe("run_verification", {"argv": [f"test-{index}"]}, "passed", {
            "tool_status": "ok", "executed": True, "workspace_changed": False,
        })
    assert resumed.ledger.unresolved_failure_count == 0
    assert not resumed.ledger.unresolved_failures
    assert resumed.metrics()["validation_status"] == "passed"


def test_unresolved_verification_output_survives_reads_and_persistence():
    controller = ProgressController(100)
    output = "exit_code: 1\nFAILED test_parser.py::test_empty - expected [], got None"
    controller.observe("run_verification", {"argv": ["pytest", "-q"]}, output, {
        "tool_status": "error", "executed": True, "workspace_changed": False,
    })
    for index in range(30):
        controller.observe("read_file", {"path": f"file{index}.py"}, "source", {
            "tool_status": "ok", "executed": True, "workspace_changed": False,
        })

    resumed = ProgressController(100, ledger=controller.ledger.to_dict())

    assert resumed.ledger.unresolved_failures[0]["output"] == output
    assert resumed.ledger.view()["unresolved_failures"][0]["output"] == output
    assert "output" not in resumed.ledger.validations[0]
    assert resumed.metrics()["validation_status"] == "failed"


def test_verification_retry_replaces_failure_detail_and_success_clears_it():
    controller = ProgressController(100)
    args = {"argv": ["pytest", "-q"]}
    metadata = {"tool_status": "error", "executed": True, "workspace_changed": False}
    controller.observe("run_verification", args, "first failure: import error", metadata)
    for index in range(12):
        controller.observe("run_verification", {"argv": [f"other-{index}"]}, "other failure", metadata)
    controller.observe("run_verification", args, "latest failure: wrong result", metadata)

    latest = controller.ledger.view()["unresolved_failures"][-1]
    assert latest["command"] == "pytest -q"
    assert latest["output"] == "latest failure: wrong result"
    assert controller.ledger.unresolved_failure_count == 13
    assert "first failure" not in str(controller.ledger.to_dict())

    controller.observe("run_verification", args, "passed", {**metadata, "tool_status": "ok"})

    assert all(item["command"] != "pytest -q" for item in controller.ledger.unresolved_failures)
    assert controller.ledger.unresolved_failure_count == 12
    assert "latest failure" not in str(controller.ledger.to_dict())


def test_missing_failure_sample_does_not_make_metrics_report_success():
    controller = ProgressController(24, ledger={
        "unresolved_failures": [], "unresolved_failure_count": 1,
        "validations": [{"kind": "validation", "command": "pytest", "status": "ok"}],
    })
    assert controller.metrics()["validation_status"] == "failed"


def test_bare_shell_uses_workspace_profile_but_explicit_path_is_preserved(tmp_path):
    from pico.execution import ExecutionProfile, WorkspaceCommandRunner
    from pico.security import SecretBoundary

    runner = WorkspaceCommandRunner(tmp_path, SecretBoundary())
    runner._profile = ExecutionProfile(("E:/tools/bash.exe", "-c"), "bash", "E:/tools/bash.exe")
    captured = []

    def capture(argv, *args):
        captured.append(argv)
        return {"exit_code": 0, "stdout": b"", "stderr": b""}

    runner._run_process = capture
    runner.run_argv(["bash", "-lc", "exit 0"])
    assert captured[-1] == ["E:/tools/bash.exe", "-lc", "exit 0"]
    runner.run_argv(["E:/other/bash.exe", "-lc", "exit 0"])
    assert captured[-1][0] == "E:/other/bash.exe"


def test_execution_profile_reports_runtime_facts_without_environment_values(tmp_path):
    from pico.execution import WorkspaceCommandRunner
    from pico.security import SecretBoundary

    profile = WorkspaceCommandRunner(tmp_path, SecretBoundary()).profile_view()

    assert profile["host_os"]
    assert profile["path_style"] in {"windows", "posix"}
    assert profile["python_command"] == "python"
    assert profile["python_executable"]
    assert "python" in profile["available_commands"]
    assert set(profile) == {
        "dialect",
        "executable",
        "host_os",
        "path_style",
        "python_command",
        "python_executable",
        "available_commands",
    }


def test_reasoning_usage_is_preserved_without_inventing_missing_values():
    from pico.providers.clients import _extract_usage_cache_details

    assert _extract_usage_cache_details({"usage": {"output_tokens_details": {"reasoning_tokens": 42}}})["reasoning_tokens"] == 42
    assert _extract_usage_cache_details({})["reasoning_tokens"] is None
