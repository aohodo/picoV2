# Human-Inspired Programming Loop

## Status

This document defines the design thesis of Pico V2. It is the current architecture direction, not a claim that Pico reproduces human cognition or exposes a model's private chain of thought.

## Thesis

Pico V2 treats repository coding as continuous calibration between an expected program behavior and observed execution evidence.

The Runtime borrows observable strategies used by experienced programmers:

- maintain a task-local understanding instead of trying to memorize the whole repository;
- acquire information to answer a current question or distinguish candidate explanations;
- alternate between understanding, editing, running, and interpreting results;
- give unresolved, diagnostic failures priority over ordinary history;
- retire evidence when the underlying file revision changes;
- complete one coherent engineering work unit before broadening scope;
- review the implementation against the original requirement before delivery.

The model still decides what the evidence means and which action is appropriate. The Runtime maintains the external facts that decision depends on: source ranges, revisions, failures, validation identity, transaction state, user constraints, and delivery obligations.

## Motivation

Pico V1 exposed repository tools and bounded the number of steps, but a long task could still follow this pattern:

```text
read source
  → receive tool output
  → lose or compact the relevant evidence
  → reconstruct an incomplete understanding
  → read the same or adjacent source again
  → exhaust the tool budget without a verified change
```

Adding another read counter or a larger fixed context does not repair that chain. The missing unit is the programmer's current problem state: what is already supported, what remains uncertain, what the last result changed, and what would count as completion.

Pico V2 therefore projects one evidence-driven work state into every model turn and anchors all mutations and validations in a transactional workspace.

## Empirical inspiration and engineering interpretation

The cited studies motivate engineering hypotheses. They do not prescribe a Coding Agent algorithm, and a neural correlate is not proof that a particular Runtime mechanism is correct.

### Edit and run are interleaved

Alaboudi and LaToza observed professional developers repeatedly alternating edits and executions during programming and debugging. Understanding continues to develop after implementation begins; it is not a mandatory repository-wide phase completed before the first edit.

Pico interpretation:

- do not require complete repository coverage before mutation;
- form a small runnable work unit;
- use execution results to update the next decision;
- widen verification only after the local behavior is correct.

Reference: [Edit-Run Behavior in Programming and Debugging](https://arxiv.org/abs/2109.02682).

### Effective investigation is hypothesis-directed

Research on debugging hypotheses found that an incorrect hypothesis can keep a developer investigating irrelevant information, while an earlier useful hypothesis is associated with better debugging outcomes. Knowing a location is not equivalent to knowing why that location matters.

Pico interpretation:

- a new read should answer a concrete unresolved question;
- repository graph candidates are navigation hints, not behavioral proof;
- evidence expansion and evidence-frontier reduction are different outcomes;
- the Runtime should preserve the current question after history compaction.

Reference: [Using Hypotheses as a Debugging Aid](https://arxiv.org/abs/2005.13652).

### Programmers maintain an executable local representation

Code-comprehension studies using fMRI, EEG, and eye tracking support a cautious common interpretation: code comprehension involves executive control and manipulation of program relationships, not only retention of source text. Better performance can be associated with more targeted attention and fewer repeated fixations, although these findings do not directly generalize to every repository task.

Pico interpretation:

- retain source identity, call relationships, constraints, and unresolved questions;
- do not equate `file was read` with `behavior is understood`;
- do not reward file coverage for its own sake;
- keep exact source retrievable when a later decision requires it.

References:

- [Comprehension of computer code relies primarily on domain-general executive brain regions](https://elifesciences.org/articles/58906)
- [Computer code comprehension shares neural resources with formal logical inference](https://elifesciences.org/articles/59340)
- [Correlates of Programmer Efficacy and Their Link to Experience](https://arxiv.org/html/2303.07071v1)
- [Role of the EEG Theta Network During Software Production](https://pubmed.ncbi.nlm.nih.gov/37506005/)

### Interpreting output is a distinct activity

Dynamic-debugging research distinguishes task understanding, fault localization, editing, compilation, and output comprehension. A tool returning text is not evidence that the result has influenced the next decision.

Pico interpretation:

- preserve the executed command and its real exit status;
- extract the expected-versus-observed discrepancy from actionable failures;
- retain unresolved failure evidence independently of ordinary history eviction;
- make the original failing verification the authority that resolves that failure;
- distinguish diagnostic probes from acceptance evidence.

Reference: [Towards a Cognitive Model of Dynamic Debugging](https://github.com/pasantiesteban/TSE24-fNIRS-DEBUGGING).

### Error sensitivity alone is insufficient

Studies of error-related responses support that violations can change attention and processing. They do not show that every error causes an adaptive correction. A developer can also become conservative, repeat checks, or add an unexplained workaround.

Pico interpretation:

- prioritize a failure only when it is relevant, unresolved, and diagnostically useful;
- classify transport, environment, test, compile, and behavioral failures differently;
- require the failure to change a hypothesis, action, or validation plan;
- remove the recovery priority after the corresponding discrepancy is resolved.

References:

- [Computer programmers show expertise-dependent responses to violations in code](https://www.nature.com/articles/s41598-024-56090-6)
- [Post-error arousal does not always improve subsequent performance](https://onlinelibrary.wiley.com/doi/10.1111/ejn.14947)

## Human programming loop

Pico uses five observable stages. These are a decision aid, not a rigid state machine that hides reversible tools.

```text
1. Perception
   Obtain source and runtime evidence relevant to the current goal.

2. Judgment
   Separate supported facts, unresolved questions, and candidate explanations.

3. Action
   Execute the smallest coherent engineering work unit that can advance the goal.

4. Feedback
   Run the code or tests, interpret the discrepancy, and update the local model.

5. Review
   Compare the current implementation, verification, and modified-file scope
   with the original request before delivery.
```

The loop is iterative:

```text
goal and constraints
        ↓
task-local evidence and open question
        ↓
discriminating read / experiment / mutation
        ↓
observed result
        ↓
updated evidence or unresolved discrepancy
        ↺
verified work unit
        ↓
delivery review
```

## Runtime mapping

| Programmer behavior | Runtime representation | Current implementation |
| --- | --- | --- |
| Preserve the current goal | Interaction and transaction requirements | `interaction_policy.py`, `TaskState` |
| Start from user-named targets | Explicit path grounding | `AgentLoop._ground_referenced_paths()` |
| Build a bounded local understanding | Revisioned initial source working set | `working_set.py` |
| Know what is already supported | Source ranges, revisions, graph evidence | `ExecutionLedger` |
| Keep the current uncertainty visible | Evidence frontier and `work_focus` | `ProgressController.work_focus_view()` |
| Avoid rereading visible stable evidence | Call and evidence identity | `ProgressController.preflight()` |
| Execute one coherent change | Exact single-file and atomic patch sets | `patch_file`, `apply_patch` |
| Use negative feedback to redirect work | Unresolved failure ledger | `ProgressController.observe()` |
| Distinguish experiments from acceptance | Verification purpose and evidence | `run_verification`, verification ledger |
| Invalidate conclusions after code changes | Unverified changes and path revisions | `ExecutionLedger.mark_mutation()` |
| Review before claiming completion | Delivery review | completion and progress control |
| Experiment without damaging source | Transactional Shadow Workspace | `transactional_workspace.py` |
| Continue after interruption | Checkpoint, session, transaction resume | checkpoint/session/run stores |
| Separate current work from durable convention | Working and Durable Memory | `features/memory.py`, memory admission |

## Evidence lifecycle

Source evidence is valid only for the file revision it describes.

```text
read path@revision-1
  → source range becomes visible evidence
  → model may use it for a decision

mutate path
  → path becomes revision-2
  → revision-1 source leaves the active working set
  → the mutation receipt becomes current post-edit evidence
  → validation for revision-1 is stale
```

The audit log may retain historical events. The active model context must not present stale source as current fact.

## Failure lifecycle

An actionable failure is a discrepancy, not merely a negative string.

```text
verification identity
  + expected acceptance condition
  + observed output and exit status
  + current workspace revision
        ↓
unresolved discrepancy
        ↓
targeted diagnosis or correction
        ↓
rerun the authoritative verification
        ↓
resolved or still unresolved
```

An unrelated successful command cannot erase a failure. A diagnostic probe cannot satisfy delivery. A verification that changes source cannot validate its own resulting revision.

## Human collaboration layer

Programming cognition is only one side of a useful Coding Agent. Pico also incorporates recurring human interaction requirements:

- a new user interruption has immediate priority over the Agent's current exploration;
- a conflict with durable memory must be interpreted as task-local or durable, rather than silently rewriting history;
- relative requests such as “smaller” or “stricter” should produce a proportional change, not an extreme one;
- package layout and abstraction style may be valid in more than one form, so repository convention and user preference take priority over the Agent's favorite pattern;
- a passing smoke test is not sufficient when the requested engineering chain, architecture, or regression coverage is incomplete;
- code quality includes cohesion, coupling, responsibility placement, compatibility, and modified-file scope, not only executable output.

These policies constrain delivery semantics. They do not grant the Runtime authority to invent requirements not present in the repository or user request.

## Relationship to ReAct-style agents

A linear action/observation history records what happened. Pico additionally maintains typed engineering state that must survive compaction and recovery:

- active goal and constraints;
- source evidence with revision identity;
- known and open evidence items;
- changed and unverified paths;
- unresolved validation discrepancies;
- transaction and delivery state;
- task-local and durable user preferences.

The model owns semantic judgment. The Runtime owns the external truth required to make and audit that judgment.

## Transaction boundary

The human-inspired loop runs inside TSW:

```text
Human-inspired evidence loop
             │
             ▼
Transactional Shadow Workspace
             │
      edit / run / recover
             │
             ▼
   Review / Commit / Discard
```

TSW is a code-transaction boundary, not a complete hostile-process sandbox. Host process isolation belongs to the deployment environment, such as a container or virtual machine.

## Non-goals

Pico does not attempt to:

- reproduce brain regions or claim a neuroscientific implementation;
- store or expose a model's private chain of thought;
- force every task through a fixed number of reads or a rigid cognitive state machine;
- treat every failure as equally important;
- infer that a file is understood merely because it was read;
- replace human requirement decomposition for an entire large product;
- claim production-level repository autonomy from a small set of successful demonstrations.

## Evaluation hypotheses

The design should be judged against explicit, falsifiable hypotheses.

### H1: task-local evidence reduces reconstruction work

Compared with ordinary projected history, a revisioned working set should reduce:

- steps before the first meaningful mutation;
- identical or overlapping source reads;
- broad repository searches after targets are known.

### H2: unresolved discrepancies improve recovery

Compared with retaining raw test output only, structured unresolved failures should improve:

- recovery after the first failed verification;
- rerunning the correct authoritative command;
- avoiding the same demonstrated error in the next patch;
- distinguishing source defects from environment or transport defects.

### H3: coherent work units reduce tool overhead without widening scope

Compared with sequential single-location patches, atomic patch sets should reduce:

- model turns required for one logical change;
- partial multi-file implementations;
- confirmation rereads;

while preserving exact modified-file scope and rollback behavior.

### H4: delivery authority prevents false success

Runtime delivery checks should reject:

- a final answer after an unresolved acceptance failure;
- verification evidence from an older source revision;
- a diagnostic probe used as acceptance;
- a passing command that did not execute the required changed tests;
- a transaction that violates explicit protected-file constraints.

## Required metrics

Representative Python, Java, frontend, and algorithm tasks should record:

- task success and independent verifier result;
- first meaningful mutation step;
- total model turns and tool steps;
- frontier-reducing, evidence-expanding, and no-progress actions;
- repeated stable reads;
- failed-verification recovery rate;
- recurrence of an already demonstrated failure;
- changed-file precision and unrelated diff size;
- input/output tokens, retries, latency, and cost;
- whether completion required resume;
- whether source remained unchanged in review mode.

## Ablation plan

Use the same repository revision, request, model, execution policy, and budget for each variant:

```text
A. linear/projected history only
B. A + unresolved failure retention
C. B + work_focus and evidence frontier
D. C + revisioned initial working set
E. D + atomic engineering work units and delivery review
```

The current Vue three-file case is an initial observation, not sufficient proof: adding advice alone did not reduce exploration, while adding a bounded initial working set moved the first write from step 13 to step 8; the later atomic edit path completed the verified task in 14 tool steps. The result should be repeated across tasks and seeds before attributing the improvement to one mechanism.

## Design rule

Do not add a new threshold merely because an undesirable action occurred several times. First identify which part of the programming loop lost authority:

- Was the goal or current question dropped?
- Was current evidence compacted or made stale?
- Was an execution result recorded but not interpreted?
- Did a failure remain after the validating revision changed?
- Did the tool boundary make one coherent action require many turns?
- Did delivery ignore state already recorded elsewhere?

Repair the state handoff or authority boundary. A quota is appropriate only when it represents a real external resource limit, not as a substitute for understanding the failure chain.
