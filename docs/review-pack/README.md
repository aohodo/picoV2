# Pico Review Pack

## Project pitch

Pico V2 is a human-inspired, evidence-driven, transactional runtime for repository coding agents. It models software development as continuous calibration between expected program behavior and observed execution evidence. The model makes semantic decisions; the Runtime preserves the revisioned source, unresolved discrepancies, validation identity, transaction state, user constraints, and delivery obligations those decisions depend on.

## Architecture map

- `pico.cli` wires configuration, provider clients, workspace context, and the runtime.
- `pico.runtime.Pico` coordinates the agent control surface.
- `pico.context_manager` builds bounded model context from prefix, memory, history, and the current request.
- `pico.working_set` supplies bounded, revisioned first-turn source evidence from explicit targets.
- `pico.progress` projects the evidence frontier, unresolved failures, unverified changes, and delivery phase.
- `pico.tools` defines the explicit tool allowlist used by the runtime.
- `pico.patch_set` applies related exact multi-file edits as one rollback-capable work unit.
- `pico.transactional_workspace` isolates edit, verification, review, commit, and discard.
- `pico.run_store` writes per-run artifacts for review and replay.

The design thesis and research-to-runtime mapping are documented in [Human-Inspired Programming Loop](../architecture/human-inspired-programming-loop.md).

## Benchmark evidence

Benchmark runs should preserve reproducibility metadata, task rows, summary counts, and failure categories so reviewers can distinguish runtime regressions from task or provider failures.

## Three-minute review route

1. Read the thesis and five-stage loop in `docs/architecture/human-inspired-programming-loop.md`.
2. Run `python -m pico --help` to inspect the public control surface.
3. Run `python scripts/run_harness_regression.py` to reproduce the 12 deterministic Runtime tasks without an API key.
4. Inspect `artifacts/harness-regression-v2.json`: every task records its verifier, budget result, run artifacts, and repository revision.
5. Use the real-task table in the root README to discuss model-in-the-loop evidence separately from deterministic Runtime regression.

This separation is deliberate: scripted regression establishes Runtime semantics; real repository tasks evaluate the combined Runtime, model, provider, and environment.

## Sample run artifact list

- `<state-root>/workspaces/<workspace-id>/runs/<run_id>/task_state.json`
- `<state-root>/workspaces/<workspace-id>/runs/<run_id>/trace.jsonl`
- `<state-root>/workspaces/<workspace-id>/runs/<run_id>/report.json`
