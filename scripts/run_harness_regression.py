#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.evaluation.evaluator import run_harness_regression_v2


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Run Pico's deterministic harness regression suite."
    )
    parser.add_argument(
        "--benchmark-path",
        default="benchmarks/coding_tasks.json",
        help="Path to the deterministic task set.",
    )
    parser.add_argument(
        "--workspace-root",
        default="artifacts/harness-workspaces",
        help="Directory for isolated fixture copies.",
    )
    parser.add_argument(
        "--artifact-path",
        default="artifacts/harness-regression-v2.json",
        help="Path for the machine-readable result.",
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    result = run_harness_regression_v2(
        benchmark_path=Path(args.benchmark_path),
        workspace_root=Path(args.workspace_root),
        artifact_path=Path(args.artifact_path),
    )
    summary = result["summary"]
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
