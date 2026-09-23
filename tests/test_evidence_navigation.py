from pico.progress import ExecutionLedger, ProgressController
from pico.read_observation import compact_read_observation, render_reads
from pico.repository_graph import RepositoryGraph
from pico.repository_intelligence import RepositoryIntelligence


def test_late_symbol_and_callee_are_navigable(tmp_path):
    (tmp_path / "entry.py").write_text(
        "\n".join(f"def filler_{i}(): pass" for i in range(30))
        + "\ndef dispatch():\n    return deliver()\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        "def deliver():\n    return 1\n", encoding="utf-8"
    )
    bundle = RepositoryIntelligence(tmp_path, semantic_mode="off").inspect(
        "entry.dispatch"
    )
    text = bundle.render()
    assert "function:dispatch@" in text
    assert "deliver: service.py:1-2" in text
    ledger = ExecutionLedger()
    ledger.seed_repository_evidence(bundle.ledger_seed())
    assert any(
        item["path"] == "service.py"
        for item in ledger.view()["pending_definition_candidates"]
    )
    restored = ExecutionLedger.from_dict(ledger.to_dict())
    restored.record_read("read_file", {"path": "service.py", "start": 1, "end": 2})
    assert not any(
        item["path"] == "service.py"
        for item in restored.view()["pending_definition_candidates"]
    )


def test_ambiguous_definition_is_never_claimed_as_resolved(tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.py").write_text("def save(): pass\n", encoding="utf-8")
    result = RepositoryGraph(tmp_path).inspect("save")
    assert len(result.definitions) == 2
    assert "not proven dynamic dispatch" in result.render()


def test_compaction_omits_whole_fragment_without_splicing_conditions():
    lines = ["def first():", "    if allowed:"] + ["        do_work()"] * 100
    lines += ["", "def second():", "    return False"]
    observation = render_reads([("app.py", "utf-8", lines, 1, len(lines))], 10000)
    compressed = compact_read_observation(observation, 500)
    assert len(compressed) <= 500
    assert "body omitted" in compressed
    assert "do_work()" not in compressed
    assert "return False" not in compressed
    assert "app.py:" in compressed


def test_compaction_can_be_repeated_without_losing_locations():
    observation = render_reads(
        [("app.py", "utf-8", ["def work():"] + ["    step()"] * 200, 1, 201)], 4000
    )
    first = compact_read_observation(observation, 600)
    second = compact_read_observation(first, 400)
    assert "app.py:" in second
    assert len(second) <= 400


def test_reacquire_evicted_evidence_and_report_visible_duplicate_honestly():
    controller = ProgressController(max_steps=12)
    args = {"path": "app.py", "start": 1, "end": 3}
    controller.observe(
        "read_file", args, "original evidence", {"executed": True, "tool_status": "ok"}
    )
    controller.set_visible_tool_outputs(
        [{"type": "function_call_output", "output": "original evidence"}]
    )
    assert controller.preflight("read_file", args)["allowed"]
    repeated = controller.observe(
        "read_file", args, "original evidence", {"executed": True, "tool_status": "ok"}
    )
    assert repeated.kind == "NO_PROGRESS"
    controller.set_visible_tool_outputs(
        [{"type": "function_call_output", "output": "[body omitted]"}]
    )
    assert controller.preflight("read_file", args)["allowed"]
    restored = controller.observe(
        "read_file", args, "original evidence", {"executed": True, "tool_status": "ok"}
    )
    assert restored.reason == "context_restored"


def test_java_query_follows_lexical_call_candidates(tmp_path):
    (tmp_path / "Controller.java").write_text(
        'public class Controller {\n public void submit() { service.deliver("}"); }\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "Service.java").write_text(
        "public class Service {\n public void deliver(String value) { }\n}\n",
        encoding="utf-8",
    )
    graph = RepositoryGraph(tmp_path).inspect("submit")
    assert ("deliver", "Service.java", 2, 2) in graph.definitions


def test_java_long_whitespace_and_comments_do_not_become_methods(tmp_path):
    text = (
        "/* "
        + " " * 20000
        + " */\npublic class Service {\n public void save() { }\n}\n"
    )
    (tmp_path / "Service.java").write_text(text, encoding="utf-8")
    graph = RepositoryGraph(tmp_path).inspect("save")
    assert ("save", "Service.java", 3, 3) in graph.definitions


def test_index_prunes_build_and_dependency_directories(tmp_path):
    for folder in ("target", "node_modules"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "ignored.py").write_text(
            "def forbidden(): pass\n", encoding="utf-8"
        )
    (tmp_path / "source.py").write_text("def wanted(): pass\n", encoding="utf-8")
    graph = RepositoryGraph(tmp_path).inspect("wanted")
    assert graph.total_files == 1


def test_mutation_invalidates_navigation_locations():
    ledger = ExecutionLedger()
    ledger.seed_repository_evidence(
        {
            "definition_candidates": [
                {"path": "a.py", "symbol": "f", "start": 2, "end": 4}
            ]
        }
    )
    ledger.record_read("read_file", {"path": "a.py", "start": 1, "end": 4})
    ledger.mark_mutation(["a.py"])
    assert "a.py" not in ledger.observed_files
    assert not ledger.definition_candidates
