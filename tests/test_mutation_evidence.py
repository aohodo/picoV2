import json
from unittest.mock import patch

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_projection import ContextProjector, discard_stale_read_groups
from pico.execution import format_shell_result
from pico.mutation_observation import _changed_ranges
from pico.progress import ProgressController
from pico.tools import native_tool_definitions


def make_agent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(source, repo_root_override=source),
        session_store=SessionStore(tmp_path / "state" / "sessions"),
        state_root=tmp_path / "state",
        approval_policy="auto",
        commit_policy="auto",
        semantic_index="off",
        max_steps=12,
    )
    agent.begin_transaction()
    agent.current_interaction = {"mutation_allowed": True}
    agent.progress_controller = ProgressController(12)
    return agent


def event_pair(call_id, name, args, result):
    return [
        {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(args),
        },
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": result.content,
            "_read_evidence": result.metadata.get("read_evidence", []),
        },
    ]


def test_patch_returns_current_source_at_the_new_revision(tmp_path):
    agent = make_agent(tmp_path)

    result = agent.execute_tool(
        "patch_file",
        {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
    )

    assert result.metadata["workspace_changed"]
    assert "mutation_applied: patch_file" in result.content
    assert "current post-edit workspace content" in result.content
    assert "VALUE = 2" in result.content
    evidence = result.metadata["read_evidence"]
    assert evidence
    assert evidence[0]["revision"] == 1
    assert evidence[0]["revision"] == agent.progress_controller.ledger.path_revision("app.py")


def test_mutation_receipt_replaces_stale_pre_edit_read(tmp_path):
    agent = make_agent(tmp_path)
    read_args = {"path": "app.py", "start": 1, "end": 1}
    old_read = agent.execute_tool("read_file", read_args)
    patch_args = {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}
    mutation = agent.execute_tool("patch_file", patch_args)
    events = (
        event_pair("read-1", "read_file", read_args, old_read)
        + event_pair("patch-1", "patch_file", patch_args, mutation)
    )

    retained = discard_stale_read_groups(events, agent.root)

    assert [item.get("call_id") for item in retained] == ["patch-1", "patch-1"]
    items, metadata = ContextProjector(agent).build(
        "Continue the implementation",
        events,
        agent.progress_controller,
        input_token_budget=100_000,
        chars_per_token=4,
    )
    rendered = json.dumps(items, ensure_ascii=False)
    assert "VALUE = 2" in rendered
    assert metadata["event_char_budget"] == ContextProjector(agent).event_char_budget


def test_second_mutation_supersedes_first_mutation_source_evidence(tmp_path):
    agent = make_agent(tmp_path)
    first_args = {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}
    first = agent.execute_tool("patch_file", first_args)
    second_args = {"path": "app.py", "old_text": "VALUE = 2", "new_text": "VALUE = 3"}
    second = agent.execute_tool("patch_file", second_args)
    events = (
        event_pair("patch-1", "patch_file", first_args, first)
        + event_pair("patch-2", "patch_file", second_args, second)
    )

    retained = discard_stale_read_groups(events, agent.root)

    assert [item.get("call_id") for item in retained] == ["patch-2", "patch-2"]
    assert retained[-1]["_read_evidence"][0]["revision"] == 2


def test_current_visible_source_is_not_executed_twice(tmp_path):
    agent = make_agent(tmp_path)
    args = {"path": "app.py", "start": 1, "end": 1}
    result = agent.execute_tool("read_file", args)
    controller = agent.progress_controller
    controller.set_visible_tool_outputs(event_pair("read-1", "read_file", args, result))

    decision = controller.preflight("read_file", args)

    assert not decision["allowed"]
    assert decision["evidence"].reason == "repeated_no_progress"


def test_read_is_allowed_after_its_visible_evidence_becomes_stale(tmp_path):
    agent = make_agent(tmp_path)
    args = {"path": "app.py", "start": 1, "end": 1}
    result = agent.execute_tool("read_file", args)
    controller = agent.progress_controller
    controller.set_visible_tool_outputs(event_pair("read-1", "read_file", args, result))
    agent.execute_tool(
        "patch_file",
        {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
    )

    assert controller.preflight("read_file", args)["allowed"]


def test_identical_visible_search_is_rejected_until_workspace_changes(tmp_path):
    agent = make_agent(tmp_path)
    args = {"path": ".", "pattern": "VALUE"}
    result = agent.execute_tool("search", args)
    controller = agent.progress_controller
    controller.set_visible_tool_outputs(event_pair("search-1", "search", args, result))

    decision = controller.preflight("search", args)

    assert not decision["allowed"]
    assert decision["evidence"].reason == "repeated_no_progress"

    agent.execute_tool(
        "patch_file",
        {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
    )
    assert controller.preflight("search", args)["allowed"]


def test_visible_batch_read_identity_tracks_each_real_paths_argument(tmp_path):
    agent = make_agent(tmp_path)
    agent.execute_tool(
        "write_file", {"path": "other.py", "content": "OTHER = 1\n"}
    )
    args = {"paths": ["app.py", "other.py"]}
    result = agent.execute_tool("read_files", args)
    controller = agent.progress_controller
    controller.set_visible_tool_outputs(
        event_pair("batch-1", "read_files", args, result)
    )

    repeated = agent.execute_tool("read_files", args)

    assert repeated.metadata["tool_error_code"] == "repeated_no_progress"
    assert not repeated.metadata["executed"]
    assert controller.state.last_action["targets"] == ["app.py", "other.py"]

    agent.execute_tool(
        "patch_file",
        {"path": "other.py", "old_text": "OTHER = 1", "new_text": "OTHER = 2"},
    )
    allowed_after_change = agent.execute_tool("read_files", args)
    assert allowed_after_change.metadata["tool_status"] == "ok"
    assert allowed_after_change.metadata["executed"]


def test_patch_receipt_shows_changed_middle_instead_of_head_only_clip(tmp_path):
    agent = make_agent(tmp_path)
    lines = [f"LINE_{index} = {index}" for index in range(1, 201)]
    agent.execute_tool(
        "write_file", {"path": "large.py", "content": "\n".join(lines) + "\n"}
    )

    result = agent.execute_tool(
        "patch_file",
        {"path": "large.py", "old_text": "LINE_100 = 100", "new_text": "LINE_100 = 999"},
    )

    assert "LINE_100 = 999" in result.content
    assert any(item["start"] <= 100 <= item["end"] for item in result.metadata["read_evidence"])


def test_patch_receipt_returns_complete_current_file_when_it_fits(tmp_path):
    agent = make_agent(tmp_path)
    agent.execute_tool(
        "write_file",
        {"path": "small.py", "content": "TOP = 1\nMIDDLE = 2\nBOTTOM = 3\n"},
    )

    result = agent.execute_tool(
        "patch_file",
        {"path": "small.py", "old_text": "MIDDLE = 2", "new_text": "MIDDLE = 20"},
    )

    assert "TOP = 1" in result.content
    assert "MIDDLE = 20" in result.content
    assert "BOTTOM = 3" in result.content
    evidence = result.metadata["read_evidence"]
    assert len(evidence) == 1
    assert (evidence[0]["start"], evidence[0]["end"]) == (1, 3)


def test_failed_patch_returns_fresh_candidate_source_without_a_reread(tmp_path):
    agent = make_agent(tmp_path)

    result = agent.execute_tool(
        "patch_file",
        {"path": "app.py", "old_text": "VALUE = 0", "new_text": "VALUE = 2"},
    )

    assert result.metadata["tool_error_code"] == "patch_match_failed"
    assert result.metadata["tool_status"] == "error"
    assert "old_text matched 0 times" in result.content
    assert "VALUE = 1" in result.content
    assert result.metadata["read_evidence"][0]["path"] == "app.py"


def test_ambiguous_patch_reports_occurrence_lines_and_current_source(tmp_path):
    agent = make_agent(tmp_path)
    agent.execute_tool(
        "write_file", {"path": "app.py", "content": "VALUE = 1\nMID = 2\nVALUE = 1\n"}
    )

    result = agent.execute_tool(
        "patch_file",
        {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
    )

    assert result.metadata["tool_error_code"] == "patch_match_failed"
    assert "matched 2 times at lines [1, 3]" in result.content
    assert "MID = 2" in result.content


def test_apply_patch_changes_multiple_files_and_locations_in_one_tool_call(tmp_path):
    agent = make_agent(tmp_path)
    agent.execute_tool(
        "write_file",
        {"path": "test_app.py", "content": "assert VALUE == 1\nassert NAME == 'old'\n"},
    )
    agent.execute_tool(
        "write_file", {"path": "app.py", "content": "VALUE = 1\nNAME = 'old'\n"}
    )

    result = agent.execute_tool(
        "apply_patch",
        {
            "edits": [
                {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                {"path": "app.py", "old_text": "NAME = 'old'", "new_text": "NAME = 'new'"},
                {
                    "path": "test_app.py",
                    "old_text": "assert VALUE == 1",
                    "new_text": "assert VALUE == 2",
                },
            ]
        },
    )

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["workspace_changed"]
    assert result.metadata["affected_paths"] == ["app.py", "test_app.py"]
    assert (agent.root / "app.py").read_text(encoding="utf-8") == "VALUE = 2\nNAME = 'new'\n"
    assert "assert VALUE == 2" in (agent.root / "test_app.py").read_text(encoding="utf-8")
    assert {item["path"] for item in result.metadata["read_evidence"]} == {
        "app.py",
        "test_app.py",
    }


def test_apply_patch_match_failure_writes_nothing_and_returns_current_source(tmp_path):
    agent = make_agent(tmp_path)
    agent.execute_tool("write_file", {"path": "other.py", "content": "OTHER = 1\n"})

    result = agent.execute_tool(
        "apply_patch",
        {
            "edits": [
                {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                {"path": "other.py", "old_text": "OTHER = 0", "new_text": "OTHER = 2"},
            ]
        },
    )

    assert result.metadata["tool_error_code"] == "patch_match_failed"
    assert not result.metadata["workspace_changed"]
    assert (agent.root / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (agent.root / "other.py").read_text(encoding="utf-8") == "OTHER = 1\n"
    assert "entire patch set was rejected before writing" in result.content
    assert "OTHER = 1" in result.content


def test_apply_patch_rejects_ambiguous_or_overlapping_edits_without_writing(tmp_path):
    agent = make_agent(tmp_path)
    agent.execute_tool(
        "write_file", {"path": "app.py", "content": "VALUE = 1\nVALUE = 1\n"}
    )
    ambiguous = agent.execute_tool(
        "apply_patch",
        {"edits": [{"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]},
    )
    assert ambiguous.metadata["tool_error_code"] == "patch_match_failed"
    assert "matched 2 times" in ambiguous.content

    agent.execute_tool(
        "write_file", {"path": "app.py", "content": "VALUE = 1\n"}
    )
    overlapping = agent.execute_tool(
        "apply_patch",
        {
            "edits": [
                {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                {"path": "app.py", "old_text": "VALUE", "new_text": "RESULT"},
            ]
        },
    )
    assert overlapping.metadata["tool_error_code"] == "patch_match_failed"
    assert "overlaps another edit" in overlapping.content
    assert (agent.root / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_apply_patch_rolls_back_earlier_file_if_a_later_write_fails(tmp_path):
    agent = make_agent(tmp_path)
    agent.execute_tool("write_file", {"path": "other.py", "content": "OTHER = 1\n"})
    from pico import patch_set

    real_write = patch_set.write_text_document
    calls = 0

    def fail_second_write(path, text, document=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            real_write(path, text, document)
            raise OSError("simulated locked file")
        return real_write(path, text, document)

    with patch("pico.patch_set.write_text_document", side_effect=fail_second_write):
        result = agent.execute_tool(
            "apply_patch",
            {
                "edits": [
                    {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                    {"path": "other.py", "old_text": "OTHER = 1", "new_text": "OTHER = 2"},
                ]
            },
        )

    assert result.metadata["tool_status"] == "error"
    assert not result.metadata["workspace_changed"]
    assert (agent.root / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (agent.root / "other.py").read_text(encoding="utf-8") == "OTHER = 1\n"


def test_apply_patch_preserves_bom_crlf_and_gb18030(tmp_path):
    agent = make_agent(tmp_path)
    bom_path = agent.root / "bom.txt"
    gbk_path = agent.root / "gbk.txt"
    bom_path.write_bytes(b"\xef\xbb\xbfA=1\r\nB=2\r\n")
    gbk_path.write_bytes("状态=旧\r\n".encode("gb18030"))

    result = agent.execute_tool(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "bom.txt",
                    "old_text": "A=1\nB=2",
                    "new_text": "A=1\nB=3",
                },
                {"path": "gbk.txt", "old_text": "状态=旧", "new_text": "状态=新"},
            ]
        },
    )

    assert result.metadata["tool_status"] == "ok"
    assert bom_path.read_bytes() == b"\xef\xbb\xbfA=1\r\nB=3\r\n"
    assert gbk_path.read_bytes() == "状态=新\r\n".encode("gb18030")


def test_patch_file_adapts_line_based_protocol_text_to_crlf(tmp_path):
    agent = make_agent(tmp_path)
    path = agent.root / "app.py"
    path.write_bytes(b"START\r\nVALUE = 1\r\nEND\r\n")

    result = agent.execute_tool(
        "patch_file",
        {
            "path": "app.py",
            "old_text": "START\nVALUE = 1\nEND",
            "new_text": "START\nVALUE = 2\nEND",
        },
    )

    assert result.metadata["tool_status"] == "ok"
    assert path.read_bytes() == b"START\r\nVALUE = 2\r\nEND\r\n"


def test_patch_file_does_not_guess_a_newline_style_for_mixed_files(tmp_path):
    agent = make_agent(tmp_path)
    path = agent.root / "app.py"
    original = b"START\r\nVALUE = 1\nEND\r\n"
    path.write_bytes(original)

    result = agent.execute_tool(
        "patch_file",
        {
            "path": "app.py",
            "old_text": "START\nVALUE = 1\nEND",
            "new_text": "START\nVALUE = 2\nEND",
        },
    )

    assert result.metadata["tool_error_code"] == "patch_match_failed"
    assert path.read_bytes() == original


def test_apply_patch_native_schema_describes_structured_edits(tmp_path):
    agent = make_agent(tmp_path)

    definitions = {
        item["name"]: item for item in native_tool_definitions(agent.tools)
    }
    schema = definitions["apply_patch"]["parameters"]

    assert schema["required"] == ["edits"]
    assert schema["properties"]["edits"]["type"] == "array"
    assert schema["properties"]["edits"]["items"]["required"] == [
        "path",
        "old_text",
        "new_text",
    ]


def test_native_tools_expose_work_plan_and_decision_links_without_changing_core_requirements(tmp_path):
    agent = make_agent(tmp_path)
    definitions = {
        item["name"]: item for item in native_tool_definitions(agent.tools)
    }

    plan = definitions["update_work_plan"]["parameters"]
    assert plan["required"] == ["items", "active_id"]
    assert plan["properties"]["items"]["items"]["required"] == [
        "id",
        "requirement",
    ]
    assert "decision_effect" in definitions["read_file"]["parameters"]["properties"]
    assert definitions["read_file"]["parameters"]["required"] == ["path"]
    assert "obligation_ids" in definitions["apply_patch"]["parameters"]["properties"]
    assert definitions["apply_patch"]["parameters"]["required"] == ["edits"]


def test_nearby_mutation_windows_are_merged_without_duplicate_source_lines():
    before = ["shared header", "old a", "shared middle", "old b", "shared tail"]
    after = ["shared header", "new a", "shared middle", "new b", "shared tail"]

    ranges = _changed_ranges(before, after, line_limit=5)

    assert len(ranges) == 1
    assert ranges[0][0] <= 2 <= ranges[0][1]
    assert ranges[0][0] <= 4 <= ranges[0][1]
    assert sum(end - start + 1 for start, end in ranges) <= 5


def test_text_protocol_receives_current_post_edit_source_without_reread(tmp_path):
    agent = make_agent(tmp_path)
    args = {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}
    result = agent.execute_tool("patch_file", args)
    agent.record(
        {
            "role": "tool",
            "name": "patch_file",
            "args": args,
            "content": result.content,
            "read_evidence": result.metadata["read_evidence"],
        }
    )

    prompt, _ = agent.context_manager.build("Continue the implementation")

    assert "[tool:patch_file]" in prompt
    assert "VALUE = 2" in prompt


def test_text_protocol_discards_superseded_mutation_receipt(tmp_path):
    agent = make_agent(tmp_path)
    for old, new in (("VALUE = 1", "VALUE = 2"), ("VALUE = 2", "VALUE = 3")):
        args = {"path": "app.py", "old_text": old, "new_text": new}
        result = agent.execute_tool("patch_file", args)
        agent.record(
            {
                "role": "tool",
                "name": "patch_file",
                "args": args,
                "content": result.content,
                "read_evidence": result.metadata["read_evidence"],
            }
        )

    prompt, _ = agent.context_manager.build("Continue the implementation")

    assert prompt.count("[tool:patch_file]") == 1
    assert "VALUE = 3" in prompt


def test_bounded_shell_feedback_keeps_diagnostic_tail():
    output = format_shell_result(
        {
            "exit_code": 1,
            "shell_profile": {"dialect": "direct"},
            "stdout": "BEGIN-OUT\n" + "x" * 10_000 + "\nEND-OUT",
            "stderr": "BEGIN-ERR\n" + "y" * 10_000 + "\nASSERTION-AT-END",
        },
        char_budget=2_000,
    )

    assert len(output) <= 2_000
    assert "BEGIN-OUT" in output
    assert "END-OUT" in output
    assert "BEGIN-ERR" in output
    assert "ASSERTION-AT-END" in output
