# V1/V2 Java comparison provenance

This directory publishes a small controlled comparison reconstructed from the retained
machine-readable benchmark outputs. It does not rerun the model and does not present two
tasks as a general coding-agent success rate.

## Fixed conditions

- Historical repository: `bid-info-push_agent_only`
- Provider protocol: OpenAI-compatible
- Model: `qwen3.8-flash`
- Platform: Windows PowerShell
- Maximum tool steps: 24
- Independent verifier: `mvn -q test`
- Success requires verifier exit code 0, protected tests byte-identical, and a non-empty
  production diff. A model final answer alone cannot make a run successful.

## Systems

- Pico V1: commit `bd90c9ab4a3d8ec734cbf329639757534ebdc187`
- Pico V2 comparison point: commit `c6d87bd072324b9b929ac505a509567bdb753a7a`

The V2 comparison point predates the later human-inspired work-focus refinements. It is used
because it is an immutable commit with two directly comparable retained runs. Later Java,
Python, Vue, and algorithm runs are development evidence and are deliberately not mixed into
this controlled denominator.

## Per-task evidence

| Task | Historical task commit | V1 result | V2 result |
| --- | --- | --- | --- |
| Startup class and three environment configurations | `87f5e4c4` | Failed after 24 tool steps; no production diff; verifier failed | Passed; first write step 7; 20 tool steps; verifier passed |
| Immutable generic response envelope | `5618f92d` | Failed after 24 tool steps; no production diff; verifier failed | Passed; first write step 5; 6 tool steps; verifier passed |

The original source artifacts remain outside the repository because they include full task
workspaces, session traces, and machine-specific paths. The committed JSON files contain only
the fields consumed by Pico's public repository-run evaluator plus source case identifiers and
task commits for auditability. Unknown aggregate token and retry values are `null`, not inferred.

## Reproduce the summary

```powershell
python scripts/summarize_repository_runs.py `
  benchmarks/results/v1-v2-java-2026-09-26/pico-v1.json `
  benchmarks/results/v1-v2-java-2026-09-26/pico-v2.json `
  --output-json benchmarks/results/v1-v2-java-2026-09-26/summary.json `
  --output-report benchmarks/results/v1-v2-java-2026-09-26/REPORT.md
```
