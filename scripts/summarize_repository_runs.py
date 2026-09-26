#!/usr/bin/env python3
"""Summarize one or more comparable real-repository run artifacts."""

import argparse
import json
from pathlib import Path

from pico.evaluation.repository_runs import (
    combine_repository_run_artifacts,
    load_repository_run_artifact,
    render_repository_run_report,
    summarize_repository_runs,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Aggregate real repository coding runs without rerunning a model."
    )
    parser.add_argument(
        "artifacts", nargs="+", help="Comparable run artifact JSON files."
    )
    parser.add_argument("--output-json", required=True, help="Summary JSON path.")
    parser.add_argument("--output-report", required=True, help="Markdown report path.")
    args = parser.parse_args(argv)

    combined = combine_repository_run_artifacts(
        load_repository_run_artifact(path) for path in args.artifacts
    )
    summary = summarize_repository_runs(combined)
    output_json = Path(args.output_json)
    output_report = Path(args.output_report)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_report.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_report.write_text(render_repository_run_report(summary), encoding="utf-8")
    print(output_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
