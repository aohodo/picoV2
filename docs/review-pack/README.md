# Pico V3 Review Pack

## Project pitch

Pico V3 is a human-inspired, evidence-driven, transactional runtime for repository coding agents. It models software development as continuous calibration between expected program behavior and observed execution evidence. The model makes semantic decisions; the Runtime preserves the revisioned source, unresolved discrepancies, validation identity, transaction state, user constraints, and delivery obligations those decisions depend on. LangGraph makes the lifecycle explicit without becoming a second persistence authority.

## Architecture map

- `pico.cli` wires configuration, provider clients, workspace context, and the runtime.
- `pico.runtime.AgentGraphRuntime` coordinates bootstrap, action, finalization, and delivery nodes.
- `pico.context` builds bounded model context and revisioned source working sets.
- `pico.domain.WorkPlanLedger` owns work items and evidence-assimilation state.
- `pico.progress` projects the evidence frontier, unresolved failures, verification identity, and delivery phase.
- `pico.tools` validates and executes the explicit tool surface and atomic patch sets.
- `pico.workspace` owns repository intelligence and transactional Shadow workspaces.
- `pico.persistence` writes sessions, checkpoints, and per-run artifacts for recovery and replay.

The design thesis and research-to-runtime mapping are documented in [Human-Inspired Programming Loop](../architecture/human-inspired-programming-loop.md).

Real-model repository evidence uses the repeated-run protocol in [Real-repository coding benchmark](../repository-coding-benchmark.md). The boundary between Runtime invariants and model strategy is recorded in [ADR 0001](../decisions/0001-runtime-governs-invariants-not-model-strategy.md); the narrower evidence-to-action boundary is recorded in [ADR 0002](../decisions/0002-evidence-assimilation-at-action-boundary.md).

## Benchmark evidence

Benchmark runs should preserve reproducibility metadata, task rows, summary counts, and failure categories so reviewers can distinguish runtime regressions from task or provider failures.

## Three-minute review route

1. Read the thesis and five-stage loop in `docs/architecture/human-inspired-programming-loop.md`.
2. Run `python -m pico --help` to inspect the public control surface.
3. Run `python scripts/run_harness_regression.py` to reproduce the 12 deterministic Runtime tasks without an API key.
4. Inspect the generated harness JSON: every task records its verifier, budget result, run artifacts, and repository revision.
5. Use the real-task table in the root README to discuss model-in-the-loop evidence separately from deterministic Runtime regression.

This separation is deliberate: scripted regression establishes Runtime semantics; real repository tasks evaluate the combined Runtime, model, provider, and environment.

## Sample run artifact list

- `<state-root>/workspaces/<workspace-id>/runs/<run_id>/task_state.json`
- `<state-root>/workspaces/<workspace-id>/runs/<run_id>/trace.jsonl`
- `<state-root>/workspaces/<workspace-id>/runs/<run_id>/report.json`
