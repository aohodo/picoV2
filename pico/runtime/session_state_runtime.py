"""Normalization for durable Pico session state.

This module owns the compatibility boundary between persisted sessions and the
live runtime.  It deliberately contains no orchestration decisions.
"""

from ..context.context_projection import project_model_events
from ..memory import memory_store as memorylib
from ..persistence import checkpoint_store as checkpointlib
from ..progress import ExecutionLedger
from ..workspace import clip


def normalize_session(
    session,
    *,
    compact_tool_args,
    event_char_budget,
    history_item_limit,
    history_content_limit,
):
    _normalize_history(
        session,
        compact_tool_args=compact_tool_args,
        item_limit=history_item_limit,
        content_limit=history_content_limit,
    )
    _normalize_memory(session)
    _normalize_model_events(session, event_char_budget)
    _normalize_usage_samples(session)
    _normalize_execution_ledger(session)
    _normalize_checkpoints(session)
    _ensure_mapping(session, "runtime_identity")
    _ensure_mapping(session, "resume_state")


def _normalize_history(session, *, compact_tool_args, item_limit, content_limit):
    history = session.setdefault("history", [])
    if not isinstance(history, list):
        session["history"] = []
        return
    normalized = []
    for raw_item in history[-item_limit:]:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        item["role"] = str(item.get("role", ""))
        item["content"] = clip(str(item.get("content", "")), content_limit)
        if item["role"] == "tool":
            _normalize_tool_history_item(item, compact_tool_args)
        normalized.append(item)
    session["history"] = normalized


def _normalize_tool_history_item(item, compact_tool_args):
    item["name"] = str(item.get("name", ""))
    raw_args = item.get("args", {})
    item["args"] = compact_tool_args(raw_args if isinstance(raw_args, dict) else {})
    raw_evidence = item.get("read_evidence", [])
    item["read_evidence"] = [
        dict(record) for record in raw_evidence if isinstance(record, dict)
    ]


def _normalize_memory(session):
    memory = session.setdefault("memory", memorylib.default_memory_state())
    if not isinstance(memory, dict):
        session["memory"] = memorylib.default_memory_state()


def _normalize_model_events(session, event_char_budget):
    events = session.setdefault("model_events", [])
    if not isinstance(events, list):
        session["model_events"] = []
        return
    session["model_events"], _ = project_model_events(
        events,
        event_limit=None,
        char_budget=event_char_budget,
    )


def _normalize_usage_samples(session):
    samples = session.setdefault("model_usage_samples", [])
    session["model_usage_samples"] = (
        [dict(item) for item in samples[-32:] if isinstance(item, dict)]
        if isinstance(samples, list)
        else []
    )


def _normalize_execution_ledger(session):
    ledger = session.setdefault("execution_ledger", {})
    session["execution_ledger"] = (
        ExecutionLedger.from_dict(ledger).to_dict() if isinstance(ledger, dict) else {}
    )


def _normalize_checkpoints(session):
    checkpoints = session.setdefault("checkpoints", {})
    if not isinstance(checkpoints, dict):
        checkpoints = {}
        session["checkpoints"] = checkpoints
    checkpoints.setdefault("current_id", "")
    items = checkpoints.setdefault("items", {})
    if not isinstance(items, dict):
        checkpoints["items"] = {}
        return
    retained = {}
    for checkpoint_id, raw_checkpoint in list(items.items())[
        -checkpointlib.MAX_CHECKPOINTS :
    ]:
        normalized = checkpointlib.normalize_checkpoint(raw_checkpoint)
        if normalized is not None:
            retained[str(checkpoint_id)] = normalized
    checkpoints["items"] = retained
    if checkpoints.get("current_id") not in retained:
        checkpoints["current_id"] = next(reversed(retained), "")


def _ensure_mapping(session, key):
    value = session.setdefault(key, {})
    if not isinstance(value, dict):
        session[key] = {}
