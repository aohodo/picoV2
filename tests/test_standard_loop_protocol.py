"""The tool loop preserves feedback and only delivers verified changes."""

import json
import sys
from dataclasses import replace
from unittest.mock import patch

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.model_contract import ModelToolCall, ModelTurn
from pico.providers.clients import OpenAICompatibleModelClient, ProviderResponseError


class ScriptedNativeClient:
    supports_native_tools = True
    supports_prompt_cache = False

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.last_completion_metadata = {}

    def complete_turn(self, **kwargs):
        self.requests.append(kwargs)
        result = self.turns.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_agent(tmp_path, turns, max_steps=8):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    model = ScriptedNativeClient(turns)
    agent = Pico(
        model_client=model,
        workspace=WorkspaceContext.build(source, repo_root_override=source),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
        max_steps=max_steps,
    )
    return agent, model


def final(text="Done."):
    return ModelTurn(kind="final", text=text, response_status="completed")


def call(name, args, call_id):
    return ModelTurn(
        kind="tool", tool_name=name, tool_args=args,
        call_id=call_id, response_status="completed",
    )


@pytest.mark.parametrize("code", ["provider_invalid_envelope", "provider_empty_output"])
def test_invalid_native_envelope_is_recoverable_feedback(tmp_path, code):
    agent, model = make_agent(
        tmp_path, [ProviderResponseError(code, "Missing response data"), final()]
    )

    assert agent.ask("Explain this project without changing files") == "Done."
    assert agent.current_task_state.model_protocol_error_count == 1
    assert "Missing response data" in json.dumps(model.requests[1]["input_items"])
    assert agent.current_task_state.status == "completed"


def test_native_transport_failure_is_not_reclassified_as_completion(tmp_path):
    agent, _ = make_agent(
        tmp_path,
        [ProviderResponseError("provider_timeout", "Gateway request timed out")],
    )

    with pytest.raises(ProviderResponseError, match="timed out"):
        agent.ask("Explain this project")

    assert agent.current_task_state.status != "completed"
    assert agent.current_task_state.stop_reason == "model_error"


def test_failed_batch_continues_independent_read_only_calls(tmp_path):
    agent, model = make_agent(
        tmp_path,
        [
            ModelTurn(
                kind="tool_batch",
                tool_calls=(
                    ModelToolCall("read_file", {"path": "missing.py"}, "missing"),
                    ModelToolCall("read_file", {"path": "app.py"}, "deferred"),
                ),
                response_status="completed",
            ),
            final(),
        ],
    )

    assert agent.ask("Inspect missing.py and app.py") == "Done."
    outputs = {
        item["call_id"]: item["output"]
        for item in model.requests[1]["input_items"]
        if item.get("type") == "function_call_output"
    }
    assert set(outputs) == {"missing", "deferred"}
    assert "error:" in outputs["missing"]
    assert "VALUE = 1" in outputs["deferred"]
    assert len(model.requests) == 2


def test_failed_batch_defers_later_mutation(tmp_path):
    agent, model = make_agent(
        tmp_path,
        [
            ModelTurn(
                kind="tool_batch",
                tool_calls=(
                    ModelToolCall("read_file", {"path": "missing.py"}, "missing"),
                    ModelToolCall(
                        "write_file",
                        {"path": "app.py", "content": "VALUE = 2\n"},
                        "deferred-write",
                    ),
                ),
                response_status="completed",
            ),
            final(),
        ],
    )

    assert agent.ask("Update app.py only if missing.py permits it") == "Done."
    outputs = {
        item["call_id"]: item["output"]
        for item in model.requests[1]["input_items"]
        if item.get("type") == "function_call_output"
    }
    assert "error:" in outputs["missing"]
    assert "batch_call_deferred" in outputs["deferred-write"]
    assert (agent.source_root / "app.py").read_text() == "VALUE = 1\n"


def test_budget_exhaustion_keeps_batch_call_result_pairs(tmp_path):
    agent, model = make_agent(
        tmp_path,
        [
            ModelTurn(
                kind="tool_batch",
                tool_calls=(
                    ModelToolCall("read_file", {"path": "app.py"}, "first"),
                    ModelToolCall("list_files", {}, "over_budget"),
                ),
                response_status="completed",
            ),
            final(),
        ],
        max_steps=1,
    )

    assert agent.ask("Inspect the project") == "Done."
    inputs = model.requests[1]["input_items"]
    calls = {item["call_id"] for item in inputs if item.get("type") == "function_call"}
    outputs = {
        item["call_id"]: item["output"]
        for item in inputs if item.get("type") == "function_call_output"
    }
    assert calls == set(outputs) == {"first", "over_budget"}
    assert "budget was exhausted" in outputs["over_budget"]
    assert agent.current_task_state.tool_steps == 1


def test_premature_final_can_run_verification_and_then_deliver(tmp_path):
    agent, model = make_agent(
        tmp_path,
        [
            call("write_file", {"path": "app.py", "content": "VALUE = 2\n"}, "write"),
            final("Implemented."),
            call(
                "run_verification",
                {"argv": [sys.executable, "-c", "import app; assert app.VALUE == 2"]},
                "verify",
            ),
            final("Implemented and verified."),
        ],
    )

    assert agent.ask("Update app.py VALUE to 2 and run tests") == "Implemented and verified."
    assert "verification_required" in json.dumps(model.requests[2]["input_items"])
    assert (agent.source_root / "app.py").read_text() == "VALUE = 2\n"
    assert agent.current_task_state.transaction_state == "COMMITTED"
    assert agent.current_task_state.tool_steps == 2


def test_successful_verification_uses_existing_final_turn_for_delivery_review(tmp_path):
    agent, model = make_agent(
        tmp_path,
        [
            call("write_file", {"path": "app.py", "content": "VALUE = 2\n"}, "write"),
            call(
                "run_verification",
                {"argv": [sys.executable, "-c", "import app; assert app.VALUE == 2"]},
                "verify",
            ),
            final("Implemented and reviewed."),
        ],
    )

    assert agent.ask("Update app.py VALUE to 2 and run tests") == "Implemented and reviewed."
    final_input = json.dumps(model.requests[2]["input_items"])
    assert "delivery review is active" in final_input
    assert "not fetching the same evidence again" in final_input
    assert "Passing authored tests are evidence" in final_input
    assert agent.current_task_state.validation_status == "passed"
    assert agent.current_task_state.transaction_state == "COMMITTED"
    assert agent.current_task_state.to_dict()["delivery_review_status"] == "completed"


def test_step_budget_finalization_cannot_bypass_delivery_review(tmp_path):
    agent, model = make_agent(
        tmp_path,
        [
            call("write_file", {"path": "app.py", "content": "VALUE = 2\n"}, "write"),
            call(
                "run_verification",
                {"argv": [sys.executable, "-c", "import app; assert app.VALUE == 2"]},
                "verify",
            ),
            final("Implemented and reviewed at the budget boundary."),
        ],
        max_steps=2,
    )

    result = agent.ask("Update app.py VALUE to 2 and run tests")

    assert result == "Implemented and reviewed at the budget boundary."
    final_input = json.dumps(model.requests[2]["input_items"])
    assert "delivery review is active" in final_input
    assert agent.current_task_state.delivery_review_status == "completed"
    assert agent.current_task_state.transaction_state == "COMMITTED"


def test_repaired_expectation_requires_original_check_before_delivery(tmp_path):
    original = [sys.executable, "check.py"]
    alternate = [sys.executable, "-X", "utf8", "check.py"]
    repair = call(
        "write_file",
        {"path": "check.py", "content": "import app\nassert app.VALUE == 2\n"},
        "repair-test",
    )
    repair = replace(
        repair,
        text="The request requires VALUE 2; correct the test expectation and rerun it.",
    )
    agent, model = make_agent(
        tmp_path,
        [
            call("write_file", {"path": "app.py", "content": "VALUE = 2\n"}, "write"),
            call("write_file", {
                "path": "check.py", "content": "import app\nassert app.VALUE == 3\n",
            }, "wrong-test"),
            call("run_verification", {"argv": original}, "failed-check"),
            repair,
            call("run_verification", {"argv": alternate}, "different-check"),
            final("Premature completion."),
            call("run_verification", {"argv": original}, "original-check"),
            final("Corrected and verified."),
        ],
        max_steps=12,
    )

    assert agent.ask("Set VALUE to 2 and add a regression test") == "Corrected and verified."
    feedback = json.dumps(model.requests[6]["input_items"])
    assert "AssertionError" in feedback
    assert "Runtime verification feedback" in feedback
    assert "rerun" in feedback
    assert "test expectation" in json.dumps(model.requests[4]["input_items"])
    assert (agent.source_root / "app.py").read_text() == "VALUE = 2\n"
    assert (agent.source_root / "check.py").read_text().endswith("assert app.VALUE == 2\n")
    assert agent.current_task_state.transaction_state == "COMMITTED"
    assert not model.turns


@pytest.mark.parametrize(
    "calls, expected",
    [
        ([{"name": "read_file", "arguments": "[]", "call_id": "a"}], "must be an object"),
        ([{"name": "read_file", "arguments": [], "call_id": "a"}], "must be an object"),
        ([
            {"name": "read_file", "arguments": "{}", "call_id": "a"},
            {"name": "list_files", "arguments": "{}", "call_id": "a"},
        ], "duplicate call IDs"),
    ],
)
def test_invalid_batch_is_rejected_before_any_call_is_executed(calls, expected):
    client = OpenAICompatibleModelClient(
        model="test", base_url="https://example.invalid/v1", api_key="not-a-key",
        temperature=0, timeout=1,
    )
    data = {
        "id": "response", "status": "completed",
        "output": [{"type": "function_call", **item} for item in calls],
    }
    with patch.object(client, "_request_responses", return_value=data):
        turn = client.complete_turn([], [], None)

    assert turn.kind == "invalid"
    assert not turn.tool_calls
    assert expected in turn.protocol_error


def test_public_plan_accompanying_tools_reaches_the_next_model_request(tmp_path):
    client = OpenAICompatibleModelClient(
        model="test", base_url="https://example.invalid/v1", api_key="not-a-key",
        temperature=0, timeout=1,
    )
    data = {
        "id": "planned", "status": "completed", "output": [
            {"type": "reasoning", "content": [{"text": "Private reasoning must not replay"}]},
            {"type": "message", "content": [
                {"type": "output_text", "text": "First inspect app.py."},
                {"type": "output_text", "text": "Then summarize its public interface."},
            ]},
            {"type": "function_call", "name": "read_file", "call_id": "read",
             "arguments": '{"path":"app.py"}'},
        ],
    }
    with patch.object(client, "_request_responses", return_value=data):
        turn = client.complete_turn([], [], None)
    assert turn.kind == "tool"
    assert turn.text == "First inspect app.py.\nThen summarize its public interface."

    agent, model = make_agent(tmp_path, [turn, final()])
    assert agent.ask("Inspect app.py") == "Done."
    serialized = json.dumps(model.requests[1]["input_items"])
    assert "First inspect app.py." in serialized
    assert "Then summarize its public interface." in serialized
    assert "Private reasoning must not replay" not in serialized
