# ADR 0002: Evidence assimilation at the action boundary

- Status: Accepted
- Date: 2026-09-27
- Supersedes: the tool-admission conclusion in ADR 0001

## Context

Repeated Java repository tasks showed that a model could produce a long sequence of distinct
observations without reducing the uncertainty that blocked implementation. In one failed trace,
29 of 36 tool events were classified as `NEW_EVIDENCE`, only 4 were classified as
`NO_PROGRESS`, and the largest exploration streak reached 18 steps.

The observations were not byte-for-byte duplicates. The model changed search ranges, tools,
cache probes, or plan wording, so a signature-based repeat detector correctly considered them
different. However, the active coding decision did not advance.

The failing call chain was:

```text
new observation identity
  -> NEW_EVIDENCE
  -> advisory work_focus
  -> all discovery tools remain available
  -> another novel observation or wording-only plan update
  -> the same unresolved decision remains open
```

This established that observation novelty, decision progress, and delivery progress are different
concepts. Step budgets and timeouts can terminate the trajectory, but cannot make it converge.

An earlier experiment had forced a plan update after every evidence call. It split coherent read
bundles and caused 8 of 14 accepted tool steps to become plan maintenance. That lock-step design
was rejected in ADR 0001.

## Decision

Pico models a bounded evidence episode on the active work item.

1. A work item states its requirement and, when discovery is needed, the current blocker or
   hypothesis.
2. A discovery call may bind its result to that work item.
3. Once useful evidence is bound, the work item enters `decision_due`.
4. Mutation, verification, finalization, and plan update remain available.
5. Additional repository discovery is admitted only after either:
   - the model acts on the evidence; or
   - `evidence_assessment` states what the evidence established and the plan supplies a concrete
     `candidate_action`, or a new `blocker` together with an `expected_observation`.
6. A wording-only plan change does not close the episode.

The boundary is semantic rather than numerical. It contains no “read N times” threshold and does
not force a mutation. A model may collect additional evidence whenever it identifies the next
decision that evidence is expected to resolve.

## Why this differs from the rejected lock-step design

- The boundary applies to evidence bound to the active work item, not every read in the task.
- The model may act directly; a separate plan call is not mandatory before mutation or validation.
- The model can open another evidence episode by explaining the current result and naming a
  concrete next blocker.
- The Runtime does not select the hypothesis or implementation.
- Safety, verification, and transaction invariants remain independent of this mechanism.

## Runtime mapping

| Concept | Owner |
| --- | --- |
| work item and `decision_due` | `domain/work_plan.py` |
| evidence binding and work focus | `progress/progress_controller.py` |
| model-visible interpretation request | `context/context_projection.py` |
| discovery-tool admission | `progress/progress_controller.py` |
| structured boundary error | `tools/tool_executor.py` |
| action/finalization orchestration | `runtime/action_turn_runtime.py` |

## Consequences

- Novel output is no longer sufficient to prove decision progress.
- The current evidence remains visible until it is interpreted or acted upon.
- Necessary exploration remains possible, including multiple evidence episodes.
- The model cannot clear the boundary by paraphrasing the plan.
- Rejected discovery calls are audit events but do not masquerade as useful tool execution.
- Step and wall-clock budgets remain emergency bounds, not cognitive control mechanisms.

## Verification

Regression coverage demonstrates that:

- bound evidence enters `decision_due`;
- discovery is unavailable while the active episode awaits interpretation;
- mutation and verification remain available;
- a wording-only update does not clear the episode;
- an assessment plus candidate action opens implementation;
- an assessment plus concrete blocker and expected observation opens a new evidence episode;
- mutation and authoritative verification advance the same work item to implemented and verified.

Real-task evaluation must still report first-pass success, time variance, first mutation, tool/model
calls, and failed trajectories. This ADR improves one call-chain invariant; it does not claim to
eliminate model variance.
