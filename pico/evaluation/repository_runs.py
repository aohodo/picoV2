"""Aggregate comparable real-repository coding runs.

The deterministic harness benchmark and real model evaluations answer
different questions.  This module keeps real-run evidence comparable without
coupling the Pico runtime to a particular repository, provider, or verifier.
"""

import json
from collections import Counter
from pathlib import Path
from statistics import mean

REQUIRED_RUN_FIELDS = frozenset(
    {
        "system_id",
        "task_id",
        "run_index",
        "repository_revision",
        "provider",
        "model",
        "success",
        "verifier_exit_code",
        "tests_unchanged",
        "metrics",
    }
)
NUMERIC_METRICS = (
    "first_write_step",
    "model_calls",
    "tool_steps",
    "total_seconds",
    "input_tokens",
    "output_tokens",
    "retries",
)


class RepositoryRunArtifactError(ValueError):
    """Raised when run evidence cannot support a comparable report."""


def load_repository_run_artifact(path):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepositoryRunArtifactError(f"cannot load {path}: {exc}") from exc
    validate_repository_run_artifact(payload, source=str(path))
    return payload


def validate_repository_run_artifact(payload, source="artifact"):
    if not isinstance(payload, dict):
        raise RepositoryRunArtifactError(f"{source}: root must be an object")
    if payload.get("schema_version") != 1:
        raise RepositoryRunArtifactError(f"{source}: schema_version must be 1")
    for field in ("task_set_id", "task_set_revision"):
        if not str(payload.get(field, "")).strip():
            raise RepositoryRunArtifactError(f"{source}: {field} is required")
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise RepositoryRunArtifactError(f"{source}: runs must be a non-empty list")
    identities = set()
    for index, run in enumerate(runs):
        label = f"{source}: runs[{index}]"
        if not isinstance(run, dict):
            raise RepositoryRunArtifactError(f"{label} must be an object")
        missing = sorted(REQUIRED_RUN_FIELDS - set(run))
        if missing:
            raise RepositoryRunArtifactError(
                f"{label} missing required fields: {', '.join(missing)}"
            )
        for field in (
            "system_id",
            "task_id",
            "repository_revision",
            "provider",
            "model",
        ):
            if not str(run[field]).strip():
                raise RepositoryRunArtifactError(f"{label}.{field} cannot be empty")
        if not isinstance(run["success"], bool) or not isinstance(
            run["tests_unchanged"], bool
        ):
            raise RepositoryRunArtifactError(
                f"{label}: success and tests_unchanged must be booleans"
            )
        if not isinstance(run["run_index"], int) or run["run_index"] < 1:
            raise RepositoryRunArtifactError(
                f"{label}.run_index must be a positive integer"
            )
        if not isinstance(run["verifier_exit_code"], int):
            raise RepositoryRunArtifactError(
                f"{label}.verifier_exit_code must be an integer"
            )
        if not isinstance(run["metrics"], dict):
            raise RepositoryRunArtifactError(f"{label}.metrics must be an object")
        for metric in NUMERIC_METRICS:
            value = run["metrics"].get(metric)
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise RepositoryRunArtifactError(
                    f"{label}.metrics.{metric} must be a non-negative number"
                )
        identity = (run["system_id"], run["task_id"], run["run_index"])
        if identity in identities:
            raise RepositoryRunArtifactError(
                f"{label}: duplicate system/task/run identity {identity}"
            )
        identities.add(identity)


def combine_repository_run_artifacts(payloads):
    payloads = list(payloads)
    if not payloads:
        raise RepositoryRunArtifactError("at least one run artifact is required")
    for index, payload in enumerate(payloads):
        validate_repository_run_artifact(payload, source=f"artifact[{index}]")
    identity = (
        payloads[0]["task_set_id"],
        payloads[0]["task_set_revision"],
    )
    for index, payload in enumerate(payloads[1:], 1):
        candidate = (payload["task_set_id"], payload["task_set_revision"])
        if candidate != identity:
            raise RepositoryRunArtifactError(
                "cannot compare different task sets: "
                f"artifact[0]={identity}, artifact[{index}]={candidate}"
            )
    combined = {
        "schema_version": 1,
        "task_set_id": identity[0],
        "task_set_revision": identity[1],
        "runs": [run for payload in payloads for run in payload["runs"]],
    }
    validate_repository_run_artifact(combined, source="combined artifacts")
    return combined


def _average(rows, metric):
    values = [
        row["metrics"][metric] for row in rows if row["metrics"].get(metric) is not None
    ]
    return round(mean(values), 3) if values else None


def summarize_repository_runs(payload):
    validate_repository_run_artifact(payload)
    systems = {}
    task_sets = {}
    for run in payload["runs"]:
        systems.setdefault(run["system_id"], []).append(run)
        task_sets.setdefault(run["system_id"], set()).add(run["task_id"])

    system_summaries = []
    for system_id, rows in sorted(systems.items()):
        ordered = sorted(rows, key=lambda row: (row["task_id"], row["run_index"]))
        first_runs = {}
        for row in ordered:
            first_runs.setdefault(row["task_id"], row)
        task_outcomes = {
            task_id: [row["success"] for row in ordered if row["task_id"] == task_id]
            for task_id in sorted(task_sets[system_id])
        }
        summary = {
            "system_id": system_id,
            "provider_models": sorted(
                {f"{row['provider']}:{row['model']}" for row in rows}
            ),
            "task_count": len(task_outcomes),
            "run_count": len(rows),
            "success_count": sum(row["success"] for row in rows),
            "success_rate": round(sum(row["success"] for row in rows) / len(rows), 4),
            "pass_at_1": round(
                sum(row["success"] for row in first_runs.values()) / len(first_runs), 4
            ),
            "stable_task_rate": round(
                sum(all(outcomes) for outcomes in task_outcomes.values())
                / len(task_outcomes),
                4,
            ),
            "tests_unchanged_rate": round(
                sum(row["tests_unchanged"] for row in rows) / len(rows), 4
            ),
            "failure_categories": dict(
                sorted(
                    Counter(
                        str(row.get("failure_category", "unspecified"))
                        for row in rows
                        if not row["success"]
                    ).items()
                )
            ),
            "averages": {metric: _average(rows, metric) for metric in NUMERIC_METRICS},
        }
        system_summaries.append(summary)

    all_task_sets = list(task_sets.values())
    common_tasks = set.intersection(*all_task_sets) if all_task_sets else set()
    union_tasks = set.union(*all_task_sets) if all_task_sets else set()
    return {
        "schema_version": 1,
        "task_set_id": payload["task_set_id"],
        "task_set_revision": payload["task_set_revision"],
        "comparability": {
            "systems": sorted(systems),
            "common_task_ids": sorted(common_tasks),
            "all_task_ids": sorted(union_tasks),
            "complete_task_matrix": all(
                tasks == union_tasks for tasks in all_task_sets
            ),
        },
        "systems": system_summaries,
    }


def render_repository_run_report(summary):
    lines = [
        "# Repository Coding Benchmark",
        "",
        f"- Task set: `{summary['task_set_id']}`",
        f"- Task-set revision: `{summary['task_set_revision']}`",
        f"- Comparable task matrix: `{summary['comparability']['complete_task_matrix']}`",
        "",
        "| System | Tasks | Runs | pass@1 | Success rate | Stable tasks | Tests unchanged | Avg tool steps | Avg seconds |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for system in summary["systems"]:
        averages = system["averages"]
        lines.append(
            "| {system_id} | {task_count} | {run_count} | {pass_at_1:.1%} | "
            "{success_rate:.1%} | {stable_task_rate:.1%} | {tests_unchanged_rate:.1%} | "
            "{tool_steps} | {seconds} |".format(
                **system,
                tool_steps=averages["tool_steps"]
                if averages["tool_steps"] is not None
                else "n/a",
                seconds=averages["total_seconds"]
                if averages["total_seconds"] is not None
                else "n/a",
            )
        )
    lines.extend(
        [
            "",
            (
                "The report separates Runtime correctness from model variance. A failed run must retain "
                "its verifier result and failure_category; it must not be silently removed from the denominator."
            ),
            "",
        ]
    )
    return "\n".join(lines)
