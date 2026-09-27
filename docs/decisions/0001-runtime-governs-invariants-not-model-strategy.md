# ADR 0001: Runtime governs invariants, not model strategy

- Status: Superseded by ADR 0002
- Date: 2026-09-26
- Baseline: `dev@7c1b99d`

## Context

Pico must prevent unsafe or false delivery while still allowing a model to gather the evidence
needed for a coding decision. These are different responsibilities:

- Runtime invariants include workspace boundaries, approval, transaction state, fresh evidence,
  validation identity, unresolved failures, protected files, and commit eligibility.
- Model strategy includes which relevant file to inspect next, whether a decision needs a bundle
  of files, and when the gathered evidence is sufficient to implement.

An experiment after `7c1b99d` converted the advisory work focus into a phase-specific tool
whitelist. After one evidence call, the Runtime required a plan update before another read. A Java
task consequently spent 8 of its first 14 accepted tool steps updating the plan and had not begun
implementation. Batched evidence gathering was also split because the first result changed the
phase before the second call executed.

The experiment made the Runtime internally consistent but made the programming process less
human-like: experienced developers commonly gather a coherent evidence bundle before revising a
hypothesis. The change was therefore reverted before commit.

## Decision

`work_focus`, obligations, decision questions, and failure evidence remain durable cognitive
scaffolding. They tell the model what is known, what remains uncertain, and what should change the
next decision.

They do not form a lock-step state machine and do not grant tool permission. Runtime enforcement
remains at concrete invariant boundaries:

- path and workspace containment;
- read-only and approval policy;
- transaction isolation and commit conflict checks;
- stale or duplicate evidence identity;
- provider response completeness;
- verification and protected-artifact integrity;
- unresolved-failure and delivery review semantics.

A core control-flow change requires a reproducible failing call chain and a regression test that
cannot be explained by provider/model variance. One unusually slow, malformed, or suboptimal model
turn is recorded in evaluation data rather than converted directly into a new Runtime rule.

## Consequences

- Models retain freedom to collect several related source/test files in one decision cycle.
- Occasional inefficient exploration can remain visible in benchmark metrics.
- Pico does not claim to make every model trajectory optimal.
- Safety and truthful delivery do not depend on the model choosing an optimal trajectory.
- V1/V2 comparisons must report repeated runs and failure categories instead of selecting only a
  successful trace.

This decision preserves the human-inspired loop: the Runtime maintains reliable perception and
feedback; the model performs semantic judgment; tools execute the chosen action; verification
calibrates the next judgment.

## Supersession note

The rejected experiment treated every evidence call as a phase transition and forced a plan
update before the rest of a coherent evidence bundle could be collected. Later repeated-run
evidence showed a narrower, reproducible failure: once evidence had been bound to an active work
item, wording-only plan edits and novel-but-irrelevant observations could keep an episode open
without interpreting the evidence already obtained.

ADR 0002 supersedes only the claim that work-focus state can never affect discovery-tool
admission. It preserves this ADR's central boundary: the Runtime does not choose the semantic
solution or force mutation. The new boundary closes one active evidence episode and accepts either
action or an explicit evidence assessment with a concrete next decision.
