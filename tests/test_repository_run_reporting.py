import json

import pytest

from pico.evaluation.repository_runs import (
    RepositoryRunArtifactError,
    combine_repository_run_artifacts,
    render_repository_run_report,
    summarize_repository_runs,
)


def artifact(system_id, outcomes):
    return {
        "schema_version": 1,
        "task_set_id": "public-repository-suite",
        "task_set_revision": "tasks-v1",
        "runs": [
            {
                "system_id": system_id,
                "task_id": task_id,
                "run_index": run_index,
                "repository_revision": f"{task_id}-base",
                "provider": "openai-compatible",
                "model": "model-under-test",
                "success": success,
                "verifier_exit_code": 0 if success else 1,
                "tests_unchanged": True,
                "failure_category": "model_protocol" if not success else "",
                "metrics": {
                    "first_write_step": 4,
                    "model_calls": 8,
                    "tool_steps": 7,
                    "total_seconds": 30 + run_index,
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "retries": 0,
                },
            }
            for task_id, run_index, success in outcomes
        ],
    }


def test_repository_report_keeps_failures_in_pass_and_stability_rates():
    payload = artifact(
        "pico-v2",
        [
            ("java-headers", 1, True),
            ("java-headers", 2, False),
            ("python-lock", 1, True),
        ],
    )

    summary = summarize_repository_runs(payload)
    system = summary["systems"][0]

    assert system["pass_at_1"] == 1.0
    assert system["success_rate"] == 0.6667
    assert system["stable_task_rate"] == 0.5
    assert system["failure_categories"] == {"model_protocol": 1}
    assert system["averages"]["tool_steps"] == 7


def test_comparison_reports_missing_task_coverage_instead_of_hiding_it():
    combined = combine_repository_run_artifacts(
        [
            artifact("pico-v1", [("java-headers", 1, False)]),
            artifact(
                "pico-v2",
                [("java-headers", 1, True), ("python-lock", 1, True)],
            ),
        ]
    )

    summary = summarize_repository_runs(combined)

    assert not summary["comparability"]["complete_task_matrix"]
    assert summary["comparability"]["common_task_ids"] == ["java-headers"]
    report = render_repository_run_report(summary)
    assert "pico-v1" in report and "pico-v2" in report


def test_different_task_set_revisions_cannot_be_compared():
    first = artifact("pico-v1", [("task", 1, True)])
    second = artifact("pico-v2", [("task", 1, True)])
    second["task_set_revision"] = "tasks-v2"

    with pytest.raises(RepositoryRunArtifactError, match="different task sets"):
        combine_repository_run_artifacts([first, second])


def test_cli_writes_machine_and_human_reports(tmp_path):
    from scripts.summarize_repository_runs import main

    source = tmp_path / "runs.json"
    output = tmp_path / "summary.json"
    report = tmp_path / "REPORT.md"
    source.write_text(
        json.dumps(artifact("pico-v2", [("task", 1, True)])), encoding="utf-8"
    )

    assert (
        main(
            [str(source), "--output-json", str(output), "--output-report", str(report)]
        )
        == 0
    )
    assert (
        json.loads(output.read_text(encoding="utf-8"))["systems"][0]["pass_at_1"] == 1.0
    )
    assert "Repository Coding Benchmark" in report.read_text(encoding="utf-8")
