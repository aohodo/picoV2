"""Project typed runtime state into a bounded Responses API input."""

import json

from .features import memory as memorylib
from .read_observation import compact_read_observation, visible_read_coverage
from .workspace import MAX_TOOL_OUTPUT

DEFAULT_EVENT_LIMIT = None
DEFAULT_EVENT_CHAR_BUDGET = 12_000
DEFAULT_RUNTIME_STATE_CHAR_BUDGET = 8_000
RECENT_EVENT_GROUPS = 2


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _head_tail(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = f"\n... [compacted {len(text) - limit} chars] ...\n"
    usable = max(0, limit - len(marker))
    head = (usable * 2) // 3
    tail = usable - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _event_groups(events):
    """Keep assistant work notes and complete call/output pairs in order."""
    groups = []
    pending = {}
    for raw in events:
        if not isinstance(raw, dict):
            continue
        event = dict(raw)
        event_type = str(event.get("type", ""))
        call_id = str(event.get("call_id", ""))
        if event.get("role") == "assistant" and isinstance(event.get("content"), str):
            groups.append([event])
        elif event_type == "function_call" and call_id:
            pending[call_id] = len(groups)
            groups.append([event])
        elif event_type == "function_call_output" and call_id in pending:
            index = pending.pop(call_id)
            groups[index].append(event)
    return [group for group in groups if len(group) == 2 or group[0].get("role") == "assistant"]


def discard_stale_read_groups(events, workspace_root):
    """Remove source observations whose recorded bytes no longer match disk."""
    retained = []
    for group in _event_groups(events):
        if len(group) == 1:
            retained.extend(group)
            continue
        call, output = group
        if call.get("name") not in {"read_file", "read_files"}:
            retained.extend(group)
            continue
        evidence = output.get("_read_evidence")
        if not evidence and str(output.get("output", "")).startswith("error:"):
            retained.extend(group)
            continue
        if not evidence:
            # Legacy source outputs have no freshness proof and cannot be
            # carried across requests as current code evidence.
            continue
        stale = any(
            item.get("freshness") is None
            or item.get("freshness")
            != memorylib.file_freshness(item.get("path", ""), workspace_root)
            for item in evidence
        )
        if not stale:
            retained.extend(group)
    return retained


def _compact_arguments(raw_arguments, limit, recent):
    raw_arguments = str(raw_arguments or "{}")
    if len(raw_arguments) <= limit:
        return raw_arguments
    try:
        payload = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        return json.dumps(
            {
                "_compacted": True,
                "argument_chars": len(raw_arguments),
                "excerpt": _head_tail(raw_arguments, max(80, limit - 90)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    if not isinstance(payload, dict):
        return json.dumps(
            {"_compacted": True, "argument_chars": len(raw_arguments)},
            ensure_ascii=False,
            sort_keys=True,
        )

    summary = {"_compacted": True}
    stable_keys = {
        "path",
        "paths",
        "start",
        "end",
        "pattern",
        "query",
        "limit",
        "timeout",
        "argv",
    }
    for key, value in payload.items():
        if key in stable_keys:
            summary[key] = value
        elif isinstance(value, str):
            summary[f"{key}_chars"] = len(value)
            if recent:
                summary[f"{key}_excerpt"] = _head_tail(value, 480)
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
        else:
            summary[f"{key}_type"] = type(value).__name__
    rendered = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(rendered) <= limit:
        return rendered
    minimal = {
        "_compacted": True,
        "argument_chars": len(raw_arguments),
        "path": _head_tail(str(payload.get("path", "")), 200),
    }
    rendered = json.dumps(
        minimal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(rendered) <= limit:
        return rendered
    return json.dumps({"_compacted": True}, separators=(",", ":"))


def _compact_group(group, recent, hard=False):
    if len(group) == 1:
        message = dict(group[0])
        text = str(message.get("content", ""))
        limit = 500 if hard else (len(text) if recent else 900)
        message["content"] = _head_tail(text, limit)
        return [message]
    call, output = (dict(group[0]), dict(group[1]))
    argument_limit = 500 if hard else (2_500 if recent else 900)
    # Fresh observations have already been bounded by ToolExecutor. Preserve
    # them until the aggregate context budget actually requires compaction.
    output_limit = 500 if hard else (len(str(output.get("output", ""))) if recent else 900)
    call["arguments"] = _compact_arguments(
        call.get("arguments", "{}"), argument_limit, recent and not hard
    )
    raw_output = str(output.get("output", ""))
    if len(raw_output) > output_limit:
        if call.get("name") in {"read_file", "read_files"}:
            output["output"] = compact_read_observation(raw_output, output_limit)
        else:
            excerpt = _head_tail(raw_output, max(80, output_limit - 100))
            output["output"] = (
                f"[compacted tool output; original_chars={len(raw_output)}; "
                "original retained in audit history]\n"
                + excerpt
            )
    if "_read_evidence" in output:
        output["_read_evidence"] = visible_read_coverage(
            output.get("output", ""), output["_read_evidence"]
        )
    return [call, output]


def project_model_events(
    events,
    event_limit=DEFAULT_EVENT_LIMIT,
    char_budget=DEFAULT_EVENT_CHAR_BUDGET,
):
    """Create a protocol-valid, size-bounded projection of tool history.

    Full observations remain in Trace/History. Only complete call/output pairs
    are projected into the next model request, newest first under the budget.
    """
    raw_events = [item for item in events if isinstance(item, dict)]
    groups = _event_groups(raw_events)
    if event_limit is not None:
        groups = groups[-max(1, int(event_limit) // 2):]
    selected_reversed = []
    used = 0
    compacted_events = 0
    for index in range(len(groups) - 1, -1, -1):
        projected = _compact_group(groups[index], recent=True)
        size = sum(_json_size(item) for item in projected)
        if size + used > char_budget:
            projected = _compact_group(groups[index], recent=False)
            size = sum(_json_size(item) for item in projected)
        if size + used > char_budget:
            projected = _compact_group(groups[index], recent=False, hard=True)
            size = sum(_json_size(item) for item in projected)
        if size + used > char_budget:
            break
        original_size = sum(_json_size(item) for item in groups[index])
        if size < original_size:
            compacted_events += len(projected)
        selected_reversed.append(projected)
        used += size

    selected = [item for group in reversed(selected_reversed) for item in group]
    metadata = {
        "raw_event_count": len(raw_events),
        "typed_event_count": len(selected),
        "dropped_event_count": max(0, len(raw_events) - len(selected)),
        "compacted_event_count": compacted_events,
        "raw_event_chars": sum(_json_size(item) for item in raw_events),
        "projected_event_chars": sum(_json_size(item) for item in selected),
        "event_char_budget": int(char_budget),
    }
    return selected, metadata


def _project_runtime_state(state, char_budget=DEFAULT_RUNTIME_STATE_CHAR_BUDGET):
    rendered = json.dumps(state, ensure_ascii=False, sort_keys=True)
    raw_chars = len(rendered)
    if raw_chars <= char_budget:
        return rendered, {
            "raw_runtime_state_chars": raw_chars,
            "runtime_state_chars": raw_chars,
            "runtime_state_compacted": False,
        }

    ledger = dict(state.get("ledger", {}))
    failures = list(ledger.get("unresolved_failures", []))[-4:]
    # Failed checks remain useful after their chronological tool event is
    # evicted. Keep their actual diagnostics, not just an unhelpful 'failed'.
    failure_share = max(1, char_budget // (2 * max(1, len(failures))))
    failure_notes = [
        {**{key: value for key, value in item.items() if key != "output"},
         "command": _head_tail(item.get("command", ""), failure_share // 4),
         "output": _head_tail(item.get("output", ""), failure_share * 3 // 4)}
        for item in failures
    ]
    compact = {
        "progress": state.get("progress", {}),
        "ledger": {
            key: ledger.get(key)
            for key in (
                "observed_file_count",
                "observed_directory_count",
                "search_count",
                "mutation_path_count",
                "validation_count",
                "unresolved_failure_count",
                "unverified_change_count",
                "grounding_path_count",
                "grounding_confidence",
                "semantic_backend",
                "semantic_status",
                "broad_exploration_count",
                "targeted_read_count",
                "file_read_count",
            )
            if key in ledger
        },
        "recent_mutations": list(ledger.get("mutations", []))[-8:],
        "recent_validations": list(ledger.get("validations", []))[-4:],
        "recent_failures": failure_notes,
        "pending_definition_candidates": list(ledger.get("pending_definition_candidates", []))[:8],
        "unverified_changes": list(ledger.get("unverified_changes", []))[-12:],
        "interaction": {
            key: value
            for key, value in dict(state.get("interaction", {})).items()
            if key != "repository_evidence"
        },
        "remaining_tool_steps": state.get("remaining_tool_steps", 0),
        "transaction_state": state.get("transaction_state", "NONE"),
        "context_compacted": True,
    }
    for optional in ("intervention", "instruction"):
        if optional in state:
            compact[optional] = state[optional]
    if state.get("checkpoint"):
        compact["checkpoint"] = _head_tail(state["checkpoint"], 800)
    if state.get("memory"):
        compact["memory"] = _head_tail(state["memory"], 1200)
    rendered = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    if len(rendered) > char_budget:
        compact.pop("memory", None)
        compact.pop("checkpoint", None)
        compact["context_compacted_further"] = True
        rendered = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    return rendered, {
        "raw_runtime_state_chars": raw_chars,
        "runtime_state_chars": len(rendered),
        "runtime_state_compacted": True,
    }


class ContextProjector:
    def __init__(
        self,
        agent,
        recent_event_limit=DEFAULT_EVENT_LIMIT,
        event_char_budget=None,
    ):
        self.agent = agent
        self.recent_event_limit = (
            int(recent_event_limit) if recent_event_limit is not None else None
        )
        self.event_char_budget = (
            int(event_char_budget) if event_char_budget is not None
            # Without an advertised context window, retain the bounded work
            # unit transcript. A second tiny fixed window used to evict code
            # after only a few calls and provoke repeated rediscovery.
            else max(1, int(agent.max_steps)) * MAX_TOOL_OUTPUT
        )
        capabilities = getattr(agent.model_client, "capabilities", None)
        if event_char_budget is None and getattr(capabilities, "context_window", None):
            policy = getattr(agent, "model_execution_policy", None)
            ratio = policy.chars_per_input_token() if policy is not None else None
            output_reserve = (
                getattr(agent, "max_new_tokens", None)
                or capabilities.max_output_tokens or 0
            )
            self.event_char_budget = max(
                1, int((capabilities.context_window - output_reserve) * (ratio or 1))
            )

    def instructions(self):
        approval = self.agent.approval_policy
        if self.agent.read_only:
            write_rule = "This run is read-only; mutation tools will be rejected."
        elif approval == "auto":
            write_rule = "Mutation tools execute in the Shadow workspace automatically and remain governed by TSW commit policy."
        elif approval == "never":
            write_rule = "Mutation tools require approval and will be rejected by this run's policy."
        else:
            write_rule = "Mutation tools execute only after interactive approval and remain isolated in the Shadow workspace."
        shell_profile = self.agent.execution_profile_view()
        shell_rule = (
            f"Shell commands use the {shell_profile.get('dialect', 'unavailable')} dialect. "
            "Use run_verification with argv elements for tests and builds; its direct process exit status is authoritative. "
        )
        return (
            "You are Pico, a local coding agent. Use the supplied function tools instead of inventing workspace facts. "
            "For repository changes, inspect only the files needed, make the requested change in the staged workspace, "
            "then run focused verification. Runtime read ranges describe delivered excerpts, not whole files. "
            "Read missing ranges when needed. For code analysis cite actual file paths and line ranges; "
            "distinguish observed calls from inferred relationships. If evidence is missing, state that limitation "
            "instead of inventing methods or claiming a complete call chain. "
            "Use inspect_repository when symbol or dependency navigation is useful. "
            "Keep a small work unit focused: inspect relevant code, implement, run tests, then fix failures from their output. "
            "Unresolved failures include real diagnostics: use them to test a cause, change the relevant code or test, "
            "and rerun verification. Distinguish a suspected cause from one demonstrated by evidence. "
            "Use already available source and tool results; reread only when missing information or changed files require it. "
            "Definition candidates are navigation hints, not proof of dispatch or behavior. "
            "Compacted source bodies marked omitted cannot support detailed code claims. "
            "Return a concise final answer only when the task is complete or concretely blocked. "
            "Runtime interaction requirements are delivery obligations: when test_artifact_required is true, "
            "create or update a focused test file before running the requested test/build verification. "
            "The latest user request outranks memory. Follow the selected package layout and repository conventions. "
            "Treat comparative requests proportionally, keep changes focused, preserve low coupling and high cohesion, "
            "and extract named responsibilities instead of accumulating unrelated generic utilities. "
            + write_rule
            + " "
            + shell_rule
            + "\n\n"
            + self.agent.workspace.text()
        )

    def build(
        self,
        user_message,
        events,
        controller,
        notice="",
        finalization=False,
        input_token_budget=None,
        chars_per_token=None,
    ):
        self.agent.invalidate_stale_memory()
        state = controller.runtime_state_view()
        state["memory"] = (
            self.agent.memory_text()
            + "\n"
            + self.agent.memory.retrieval_view(user_message, limit=3)
        )[:2400]
        state["interaction"] = dict(
            getattr(self.agent, "current_interaction", {}) or {}
        )
        checkpoint = self.agent.render_checkpoint_text()
        if checkpoint:
            state["checkpoint"] = checkpoint[:1600]
        state["remaining_tool_steps"] = max(
            0,
            self.agent.max_steps
            - getattr(self.agent.current_task_state, "tool_steps", 0),
        )
        state["transaction_state"] = (
            self.agent.transaction_context.workspace.state
            if self.agent.transaction_context is not None
            else "NONE"
        )
        if notice:
            state["intervention"] = notice
        if finalization:
            state["instruction"] = (
                "Finalization mode is active. Return one complete final answer from "
                "existing evidence; do not call a tool or resume repository exploration."
            )
        input_char_budget = None
        if input_token_budget:
            input_char_budget = max(
                1, int(float(input_token_budget) * float(chars_per_token or 1))
            )
        fixed_chars = len(str(user_message)) + len(self.instructions())
        runtime_char_budget = DEFAULT_RUNTIME_STATE_CHAR_BUDGET
        if input_char_budget is not None:
            runtime_char_budget = min(
                runtime_char_budget, max(1, input_char_budget - fixed_chars)
            )
        runtime_json, runtime_metadata = _project_runtime_state(
            state, runtime_char_budget
        )
        runtime_text = "Runtime state:\n" + runtime_json
        event_char_budget = self.event_char_budget
        if input_char_budget is not None:
            # A known model budget supersedes the conservative fallback; do
            # not truncate a capable model to the legacy 12k-character window.
            event_char_budget = max(
                1, input_char_budget - fixed_chars - len(runtime_text)
            )
        current_events = discard_stale_read_groups(events, self.agent.root)
        selected_events, event_metadata = project_model_events(
            current_events,
            event_limit=self.recent_event_limit,
            char_budget=event_char_budget,
        )
        controller.set_visible_tool_outputs(selected_events)
        # Evidence is runtime metadata, never an extension of the provider's
        # function_call_output schema.
        selected_events = [
            {key: value for key, value in event.items() if key != "_read_evidence"}
            for event in selected_events
        ]
        input_items = [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": str(user_message)}],
            },
            *selected_events,
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": runtime_text}],
            },
        ]
        metadata = {
            "native_tools": True,
            **event_metadata,
            **runtime_metadata,
            "runtime_state_chars": len(runtime_text),
            "request_chars": len(str(user_message)),
            "projected_input_chars": sum(_json_size(item) for item in input_items),
            "input_token_budget": input_token_budget,
            "observed_chars_per_token": chars_per_token,
        }
        return input_items, metadata
