# Real-repository coding benchmark

Pico separates deterministic Runtime regression from stochastic model evaluation. The
12-task Harness suite proves tool, context, recovery, and verifier contracts; it does not
prove that a model can reliably implement arbitrary repository changes.

Real coding evidence therefore uses repeated runs over the same task-set revision. Each
run records the repository revision, model, verifier exit code, whether protected tests
changed, first-write step, model/tool calls, duration, tokens, retries, and a failure
category. Failed attempts remain in the denominator.

## Run artifact

One JSON artifact contains `schema_version`, `task_set_id`, `task_set_revision`, and a
non-empty `runs` array. A run has this shape:

```json
{
  "system_id": "pico-v2-7c1b99d",
  "task_id": "java-response-headers",
  "run_index": 1,
  "repository_revision": "<base commit>",
  "provider": "openai-compatible",
  "model": "qwen3.8-flash",
  "success": true,
  "verifier_exit_code": 0,
  "tests_unchanged": true,
  "failure_category": "",
  "metrics": {
    "first_write_step": 11,
    "model_calls": 16,
    "tool_steps": 15,
    "total_seconds": 210.4,
    "input_tokens": 0,
    "output_tokens": 0,
    "retries": 0
  }
}
```

Unknown token values should be omitted, not replaced with invented estimates. A successful
final message does not set `success=true`; the independent verifier, protected-file hashes,
and required production diff do.

## V1/V2 comparison

Use the same task-set revision, repository base revisions, provider/model settings, and
repetition count. Do not remove malformed model turns or timeouts. Classify them so model
variance remains visible without being confused with Runtime invariant failures.

```powershell
python scripts/summarize_repository_runs.py `
  artifacts/pico-v1-runs.json artifacts/pico-v2-runs.json `
  --output-json artifacts/repository-summary.json `
  --output-report artifacts/REPOSITORY-BENCHMARK.md
```

The report publishes pass@1, all-run success rate, stable-task rate, protected-test rate,
average steps/time/tokens, task coverage, and failure categories. A comparison with missing
task coverage is marked incomplete rather than presented as a fair result.

## Interpretation

- Repeated failures with the same incorrect Runtime transition indicate a Runtime defect.
- Different, non-reproducible model choices with intact safety and verification boundaries
  are reported as model/provider variance.
- A Runtime change is justified by a concrete failing call chain and regression test, not by
  one unusually slow or malformed model turn.
