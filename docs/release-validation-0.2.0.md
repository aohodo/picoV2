# Pico 0.2.0 release validation

- Validation date: 2026-09-26
- Runtime baseline: `dev@7c1b99d`
- Release candidate branch: `portfolio-evidence`
- Python environment: Conda, Python 3.12
- Host: Windows

## Automated checks

```text
python -m pytest -q
222 passed, 1 skipped in 85.68s

python -m ruff check pico tests scripts
All checks passed!

python scripts/run_harness_regression.py <isolated E-drive artifact paths>
12/12 passed; verifier 12/12; within budget 12/12

python -m pico --help
CLI smoke passed

python -m pip wheel --no-deps .
pico-0.2.0-py3-none-any.whl built successfully
```

The four tests added after the Runtime baseline cover only repository-run evidence validation and
reporting. Pico's Agent loop, progress controller, tool execution, prompts, provider policy, and
transactional workspace are unchanged from the accepted Runtime baseline.

GitHub Actions is configured to run CLI smoke, Ruff, and the complete test suite on:

- Ubuntu with Python 3.10 and 3.12;
- Windows with Python 3.10 and 3.12.

## Evidence boundaries

- The committed 12-task Harness regression is deterministic and proves Runtime contracts, not
  general model coding ability.
- Real Java, Python, and Vue acceptance runs are development evidence; they are not presented as a
  statistically significant public benchmark.
- Real-repository reports must use the repeated-run format in
  `docs/repository-coding-benchmark.md` before pass@1 or V1/V2 superiority is claimed.
- TSW is a transactional code-delivery boundary, not hostile-process isolation.

## Deferred release item

No LICENSE is added automatically. The repository contains purchased baseline code, so the owner
must confirm redistribution and open-source licensing rights before selecting a license.
