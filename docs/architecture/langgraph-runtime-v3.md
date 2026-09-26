# Pico V3 LangGraph Runtime

## Goal

Pico V3 changes orchestration, not product semantics. It keeps the V2
evidence-driven programming loop, transactional shadow workspace, provider
integrity checks, verification identity, memory, checkpoint, and delivery
rules. LangGraph makes the lifecycle explicit and independently testable.

## Runtime graph

```text
START
  -> bootstrap_runtime
  -> action_turn_runtime -------+
       | model/tool turn        |
       +------------------------+
       | final requested
       v
     finalization_turn_runtime --+
       |                          |
       +--------------------------+
       | admitted                 | cannot complete
       v                          v
     delivery_runtime          stop_runtime
       |                          |
       +-------------> END <------+
```

Each graph invocation executes one complete user request. An action node owns
one model decision and, when requested, one tool observation. The graph then
routes from the resulting state instead of hiding the whole task in one opaque
node.

## State authority

LangGraph is the in-process orchestration authority. It owns the current phase
and its transition. It is not Pico's persistence authority.

Pico domain services remain authoritative for:

- repository evidence and revision identity;
- unresolved verification feedback and work focus;
- tool validation, approval, and execution;
- transactional workspace state and delivery;
- sessions, runs, checkpoints, and durable memory;
- provider response completeness and recovery policy.

V3 intentionally does not add a second LangGraph checkpointer. Pico already
has durable session, run, checkpoint, and transaction journals. Persisting the
same lifecycle through two independent systems would make recovery ambiguous.

## Runtime modules

| Module | Responsibility |
| --- | --- |
| `agent_graph_runtime.py` | Graph topology and phase routing |
| `agent_loop_runtime.py` | Small composition root for a request |
| `bootstrap_runtime.py` | Task, transaction, context, and initial graph state |
| `action_turn_runtime.py` | One action decision and its next phase |
| `model_turn_runtime.py` | Provider request lifecycle and response contract |
| `tool_turn_runtime.py` | Tool-call persistence and observation handoff |
| `finalization_runtime.py` | Final-answer recovery and completion admission |
| `delivery_runtime.py` | Transaction commit, stop, abort, and audit outcome |
| `repository_grounding_runtime.py` | Revision-aware repository evidence |
| `session_state_runtime.py` | Persisted session compatibility and normalization |
| `model_protocol_runtime.py` | Legacy tagged protocol parsing compatibility |
| `pico_runtime.py` | Dependency composition and public `Pico` API |

Files use the `_runtime.py` suffix so their responsibility remains visible in
search results and stack traces outside the package tree.

## V2 compatibility rule

The V3 migration must not redefine task completion, validation, transaction,
memory, or security semantics. Existing V2 regression tests remain the primary
behavioral contract. V3 adds graph-topology and orchestration metadata tests,
then runs the complete inherited suite.

The preserved V2 source line is recorded on the local `pico-v2-stable` branch.
V3 development is isolated on `pico-v3-langgraph`.

## Current validation

- complete inherited suite: `230 passed, 1 skipped`;
- runtime lint: no Ruff findings;
- runtime complexity audit (`C901`, `PLR0912`, `PLR0915`): no findings;
- graph topology and `orchestrator=langgraph` trace metadata: covered by tests.

This evidence establishes behavioral regression compatibility for the covered
contracts. It does not claim that every provider response or external
repository task is deterministic.

## Package boundaries

The package root contains only `__init__.py` and `__main__.py`. Product code is
grouped by responsibility under `cli`, `runtime`, `domain`, `context`,
`memory`, `progress`, `tools`, `execution`, `workspace`, `persistence`,
`providers`, `security`, `evaluation`, and `utils`.

The executable path is intentionally singular:

```text
python -m pico
  -> pico.__main__.main
  -> pico.cli.cli_runtime.main
  -> pico.runtime.pico_runtime.Pico
  -> pico.runtime.agent_graph_runtime.AgentGraphRuntime
```

The old Docker sandbox implementation and the independent mini-pico teaching
implementation are not part of V3. Deployment may still use
`docker/Dockerfile.runtime`; process isolation remains a deployment boundary,
while TSW remains Pico's transactional code-delivery boundary.
