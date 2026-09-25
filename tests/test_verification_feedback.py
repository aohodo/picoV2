import json

import pytest

from pico.features.memory import LayeredMemory
from pico.progress import ProgressController
from pico.verification_feedback import (
    MAX_VERIFICATION_FEEDBACK_CHARS,
    completion_feedback,
    verification_observation,
)


@pytest.mark.parametrize("output,kind,detail", [
    ('Exception in thread "main" java.lang.AssertionError: expected 29, actual 30',
     "assertion", "expected 29, actual 30"),
    ("E       FileNotFoundError: sites/with/slash.json\nFAILED test_keys.py::test_names",
     "file_access", "sites/with/slash.json"),
    ("ERROR at setup of test_save\nfixture database unavailable", "test_setup", "ERROR at setup"),
    ("ERROR collecting test_service.py", "test_collection", "test_service.py"),
    ("UserService.java:12: error: cannot find symbol", "compile", "cannot find symbol"),
    ("error: verification_runtime_unavailable: no executable", "launch", "verification_runtime_unavailable"),
])
def test_observations_quote_evidence_without_inventing_root_cause(output, kind, detail):
    result = verification_observation(output)
    assert kind in result["kinds"]
    assert detail in result["evidence"]
    assert "root_cause" not in result
    assert "production_bug" not in result


def test_unrecognized_output_and_quoted_source_do_not_invent_diagnosis():
    assert verification_observation('raise AssertionError("example")')["kinds"] == ["unknown"]
    assert verification_observation("process failed for an unspecified reason") == {
        "kinds": ["unknown"], "evidence": "",
    }


def test_success_with_changed_flags_points_back_to_exact_unresolved_check():
    controller = ProgressController(24)
    original = ["java", "-cp", "build/classes", "com.hcy.UserServiceAgeTest"]
    modified = ["java", "-Dfile.encoding=UTF-8", *original[1:]]
    metadata = {"executed": True, "workspace_changed": False, "tool_status": "error"}
    controller.observe("run_verification", {"argv": original}, "AssertionError: expected 29, actual 30", metadata)
    controller.observe("run_verification", {"argv": modified}, "58 passed", {**metadata, "tool_status": "ok"})
    controller = ProgressController(24, ledger=json.loads(json.dumps(controller.ledger.to_dict())))
    feedback = completion_feedback("verification_failed", controller.ledger)
    payload = json.loads(feedback.split("\n", 1)[1])
    assert payload["retry"] == {"name": "run_verification", "args": {"argv": original}}
    assert payload["observation"]["kinds"] == ["assertion"]
    assert controller.ledger.unresolved_failure_count == 1
    controller.observe("run_verification", {"argv": original}, "58 passed", {**metadata, "tool_status": "ok"})
    assert controller.ledger.unresolved_failure_count == 0


def test_huge_retry_is_omitted_not_transformed_into_different_executable_call():
    controller = ProgressController(24)
    argv = ["python", "-c", "x" * MAX_VERIFICATION_FEEDBACK_CHARS * 2]
    controller.observe("run_verification", {"argv": argv}, "failed", {
        "executed": True, "workspace_changed": False, "tool_status": "error",
    })
    payload = json.loads(completion_feedback("verification_failed", controller.ledger).split("\n", 1)[1])
    assert "retry" not in payload
    assert "original recorded tool call" in payload["next_step"]
    assert controller.ledger.unresolved_failures[0]["argv"] == argv


def test_work_note_is_tentative_scoped_replaceable_and_not_durable():
    memory = LayeredMemory().set_work_scope("transaction-one")
    memory.set_work_note("Question: is the test fixture valid? Hypothesis: parent directory is missing.")
    restored = LayeredMemory(json.loads(json.dumps(memory.to_dict())))
    restored.set_work_scope("transaction-one")
    assert "parent directory" in restored.to_dict()["working"]["work_note"]
    assert not restored.to_dict()["episodic_notes"]
    restored.set_work_note("Observed file-access error. Next check: inspect fixture path construction.")
    assert "Hypothesis:" not in restored.to_dict()["working"]["work_note"]
    restored.set_work_scope("transaction-two")
    assert restored.to_dict()["working"]["work_note"] == ""
