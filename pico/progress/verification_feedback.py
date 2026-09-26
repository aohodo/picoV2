"""Observed diagnostics and actionable retries, never delivery authority."""

import json
import re

MAX_VERIFICATION_FEEDBACK_CHARS = 4_000

WORK_GUIDANCE = (
    "Work from the current question, not file coverage. Read to resolve a specific uncertainty; "
    "a small runnable change can test an idea before the whole repository is understood. "
    "Use the runtime work_focus as the shared evidence frontier: preserve facts already known, "
    "name the concrete missing fact a new read will supply, and transition to implementation "
    "when the named targets are grounded. A read that merely expands context is not the same as "
    "one that reduces an open question. When work_focus.phase is implement, the default next "
    "action is a mutation based on current evidence. Explore again only for a specific unresolved "
    "dependency that changes that mutation, and use the answer immediately. "
    "When your understanding changes, you may give a brief public work note: the current question, "
    "observed evidence, a tentative explanation, and the next discriminating check. "
    "This is optional, not a required format or a request for private reasoning. "
    "Interpret execution results before choosing the next action. An assertion failure does not "
    "prove production code is wrong: check the requested behavior and the test expectation. "
    "A setup, file-access, or launch error may occur before business behavior is exercised. "
    "Do not weaken tests merely to make them pass. Rerun unresolved checks using their exact argv; "
    "a changed invocation is not proof that a previous check passed."
)


def verification_observation(output):
    """Recognize explicit output markers; do not infer the defect's cause."""
    text = str(output)
    patterns = (
        ("test_setup", r"ERROR at setup of"),
        ("test_collection", r"ERROR collecting"),
        ("compile", r"(?m)^.*\.(?:java|c|cpp):\d+.*\berror:"),
        ("assertion", r'(?m)^\s*(?:E\s+|Exception in thread "[^\"]+" (?:[\w.]+\.)?|(?:[\w.]+\.)?)AssertionError\b|^\s*E\s+assert\b'),
        ("file_access", r"(?m)^\s*(?:E\s+)?(?:FileNotFoundError|PermissionError)\b"),
        ("launch", r"verification_runtime_unavailable"),
    )
    kinds = [kind for kind, pattern in patterns if re.search(pattern, text)]
    # Keep literal diagnostic lines. These are observations, not a declaration
    # that the implementation, test, or environment is responsible.
    evidence = list(dict.fromkeys(
        line.strip() for line in text.splitlines()
        if any(re.search(pattern, line) for _, pattern in patterns)
        or re.match(r"\s*(?:FAILED |ERROR |.*\.(?:py|java):\d+:)", line)
    ))
    return {"kinds": kinds or ["unknown"], "evidence": "\n".join(evidence)}


def completion_feedback(reason, ledger):
    """Point at one pending check without equating different invocations."""
    feedback = {
        "reason": reason,
        "message": (
            "Completion was not accepted; staged changes are preserved. Interpret the "
            "observed failure against the requirement before changing code or tests. "
            "A successful compile alone does not rerun a failed test."
        ),
        "unresolved_failure_count": ledger.unresolved_failure_count,
    }
    if ledger.unresolved_failures:
        failure = ledger.unresolved_failures[-1]
        feedback["observation"] = verification_observation(failure.get("output", ""))
        argv = failure.get("argv")
        if isinstance(argv, list) and argv:
            feedback["retry"] = {"name": "run_verification", "args": {"argv": list(argv)}}
            feedback["next_step"] = (
                "If the cause is already fixed, rerun this exact check now. Changing "
                "flags or running another passing command does not resolve this record."
            )
        else:
            feedback["next_step"] = "Original argv is missing; do not reconstruct it by splitting display text."
    else:
        feedback["next_step"] = "Run the required verification on the current code before finishing."
    if len(json.dumps(feedback, ensure_ascii=False)) > MAX_VERIFICATION_FEEDBACK_CHARS:
        feedback.pop("observation", None)
    if len(json.dumps(feedback, ensure_ascii=False)) > MAX_VERIFICATION_FEEDBACK_CHARS:
        feedback.pop("retry", None)
        feedback["next_step"] = "The exact invocation exceeds this excerpt; consult the original recorded tool call, not a truncated command."
    return "Runtime verification feedback:\n" + json.dumps(feedback, ensure_ascii=False)
