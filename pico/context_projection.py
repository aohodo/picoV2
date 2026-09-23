"""Project typed runtime state into a bounded Responses API input."""

import json

DEFAULT_EVENT_LIMIT = 16
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
    """Return complete function-call/output pairs in chronological order."""
    groups = []
    pending = {}
    for raw in events:
        if not isinstance(raw, dict):
            continue
        event = dict(raw)
        event_type = str(event.get("type", ""))
        call_id = str(event.get("call_id", ""))
        if event_type == "function_call" and call_id:
            pending[call_id] = len(groups)
            groups.append([event])
        elif event_type == "function_call_output" and call_id in pending:
            index = pending.pop(call_id)
            groups[index].append(event)
    return [group for group in groups if len(group) == 2]


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
    call, output = (dict(group[0]), dict(group[1]))
    argument_limit = 500 if hard else (2_500 if recent else 900)
    output_limit = 500 if hard else (3_500 if recent else 900)
    call["arguments"] = _compact_arguments(
        call.get("arguments", "{}"), argument_limit, recent and not hard
    )
    raw_output = str(output.get("output", ""))
    if len(raw_output) > output_limit:
        excerpt = _head_tail(raw_output, max(80, output_limit - 100))
        output["output"] = (
            f"[compacted tool output; original_chars={len(raw_output)}; "
            "original retained in audit history]\n"
            + excerpt
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
    max_groups = max(1, int(event_limit) // 2)
    groups = groups[-max_groups:]
    selected_reversed = []
    used = 0
    compacted_events = 0
    for index in range(len(groups) - 1, -1, -1):
        recent = index >= len(groups) - RECENT_EVENT_GROUPS
        projected = _compact_group(groups[index], recent=recent)
        size = sum(_json_size(item) for item in projected)
        if size + used > char_budget:
            projected = _compact_group(groups[index], recent=False, hard=True)
            size = sum(_json_size(item) for item in projected)
        if size + used > char_budget:
            break
        original_size = sum(_json_size(item) for item in groups[index])
        if size < original_size:
            compacted_events += 2
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
        "recent_failures": list(ledger.get("unresolved_failures", []))[-4:],
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
        event_char_budget=DEFAULT_EVENT_CHAR_BUDGET,
    ):
        self.agent = agent
        self.recent_event_limit = int(recent_event_limit)
        self.event_char_budget = int(event_char_budget)

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
            "then run focused verification. Do not reread evidence already listed in Runtime state. "
            "For cross-file Python or Java work, query inspect_repository for symbols and dependency direction before broad listing. "
            "Return a concise final answer only when the task is complete or concretely blocked. "
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
        state = controller.runtime_state_view()
        state["memory"] = self.agent.memory_text()[:2400]
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
        if input_token_budget and chars_per_token:
            input_char_budget = max(
                1, int(float(input_token_budget) * float(chars_per_token))
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
            event_char_budget = min(
                event_char_budget,
                max(1, input_char_budget - fixed_chars - len(runtime_text)),
            )
        selected_events, event_metadata = project_model_events(
            events,
            event_limit=self.recent_event_limit,
            char_budget=event_char_budget,
        )
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
