"""Checkpoint and resume-state helpers."""

import uuid

from .features import memory as memorylib
from .workspace import clip, now

CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
MAX_CHECKPOINTS = 32
MAX_CHECKPOINT_GOAL_CHARS = 600
MAX_CHECKPOINT_RESULT_CHARS = 600
MAX_CHECKPOINT_PATH_CHARS = 512
MAX_CHECKPOINT_KEY_FILES = 12
MAX_CHECKPOINT_RESULTS = 8
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
CHECKPOINT_SCHEMA_MISMATCH_STATUS = "schema-mismatch"

RUNTIME_IDENTITY_KEYS = (
    "cwd",
    "model",
    "model_client",
    "approval_policy",
    "read_only",
    "max_steps",
    "max_new_tokens",
    "feature_flags",
    "shell_env_allowlist",
    "workspace_fingerprint",
    "tool_signature",
)


def normalize_checkpoint(value):
    """Normalize persisted checkpoint data before resume or prompt use."""
    if not isinstance(value, dict):
        return None
    checkpoint = dict(value)
    checkpoint["checkpoint_id"] = clip(
        str(checkpoint.get("checkpoint_id", "")), 120
    )
    checkpoint["parent_checkpoint_id"] = clip(
        str(checkpoint.get("parent_checkpoint_id", "")), 120
    )
    checkpoint["schema_version"] = clip(
        str(checkpoint.get("schema_version", "")), 80
    )
    checkpoint["current_goal"] = clip(
        str(checkpoint.get("current_goal", "")), MAX_CHECKPOINT_GOAL_CHARS
    )
    checkpoint["current_blocker"] = clip(
        str(checkpoint.get("current_blocker", "")), 240
    )
    checkpoint["next_step"] = clip(str(checkpoint.get("next_step", "")), 240)
    checkpoint["summary"] = clip(str(checkpoint.get("summary", "")), 240)
    for field in ("completed", "excluded"):
        values = checkpoint.get(field, [])
        if not isinstance(values, list):
            values = []
        checkpoint[field] = [
            clip(str(item), MAX_CHECKPOINT_RESULT_CHARS)
            for item in values[-MAX_CHECKPOINT_RESULTS:]
        ]
    key_files = checkpoint.get("key_files", [])
    if not isinstance(key_files, list):
        key_files = []
    normalized_files = []
    for item in key_files[-MAX_CHECKPOINT_KEY_FILES:]:
        if not isinstance(item, dict):
            continue
        path = clip(str(item.get("path", "")).strip(), MAX_CHECKPOINT_PATH_CHARS)
        if not path:
            continue
        normalized_files.append(
            {"path": path, "freshness": item.get("freshness")}
        )
    checkpoint["key_files"] = normalized_files
    if not isinstance(checkpoint.get("runtime_identity"), dict):
        checkpoint["runtime_identity"] = {}
    if not isinstance(checkpoint.get("transaction"), dict):
        checkpoint["transaction"] = {}
    return checkpoint


def current_runtime_identity(agent):
    return {
        "session_id": agent.session.get("id", ""),
        "cwd": str(agent.source_root),
        "model": str(getattr(agent.model_client, "model", "")),
        "model_client": agent.model_client.__class__.__name__,
        "approval_policy": agent.approval_policy,
        "read_only": bool(agent.read_only),
        "max_steps": int(agent.max_steps),
        "max_new_tokens": (
            int(agent.max_new_tokens)
            if agent.max_new_tokens is not None
            else None
        ),
        "feature_flags": dict(agent.feature_flags),
        "shell_env_allowlist": list(agent.shell_env_allowlist),
        "workspace_fingerprint": agent.workspace.__class__.build(
            agent.source_root, repo_root_override=agent.source_root
        ).fingerprint(),
        "tool_signature": agent.tool_signature(),
    }


def checkpoint_state(agent):
    agent._ensure_session_shape()
    return agent.session["checkpoints"]


def current_checkpoint(agent):
    state = checkpoint_state(agent)
    checkpoint_id = str(state.get("current_id", "")).strip()
    if not checkpoint_id:
        return None
    return state.get("items", {}).get(checkpoint_id)


def evaluate_resume_state(agent):
    previous_resume_state = dict(agent.session.get("resume_state", {}) or {})
    invalidated = agent.invalidate_stale_memory()
    checkpoint = current_checkpoint(agent)
    status = CHECKPOINT_NONE_STATUS
    stale_paths = list(invalidated)
    mismatch_fields = []
    if checkpoint:
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
        else:
            for item in checkpoint.get("key_files", []):
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                expected = item.get("freshness")
                current = memorylib.file_freshness(path, agent.root)
                if expected != current and path not in stale_paths:
                    stale_paths.append(path)
            saved_identity = dict(checkpoint.get("runtime_identity", {}) or agent.session.get("runtime_identity", {}) or {})
            current_identity = current_runtime_identity(agent)
            for key in RUNTIME_IDENTITY_KEYS:
                if key not in saved_identity:
                    continue
                if saved_identity.get(key) != current_identity.get(key):
                    mismatch_fields.append(key)
            mismatch_fields.sort()
            if stale_paths:
                status = CHECKPOINT_PARTIAL_STALE_STATUS
            elif mismatch_fields:
                status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            else:
                status = CHECKPOINT_FULL_VALID_STATUS

    resume_state = {
        "status": status,
        "stale_paths": stale_paths,
        "runtime_identity_mismatch_fields": mismatch_fields,
        "stale_summary_invalidations": max(
            len(invalidated),
            int(previous_resume_state.get("stale_summary_invalidations", 0))
            if status == CHECKPOINT_PARTIAL_STALE_STATUS
            else 0,
        ),
    }
    agent.session["resume_state"] = resume_state
    agent.session["runtime_identity"] = current_runtime_identity(agent)
    return resume_state


def render_checkpoint_text(agent):
    checkpoint = current_checkpoint(agent)
    if not checkpoint:
        return ""
    lines = [
        "Task checkpoint:",
        f"- Resume status: {agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
        "- Current goal: "
        + clip(
            str(checkpoint.get("current_goal", "-") or "-"),
            MAX_CHECKPOINT_GOAL_CHARS,
        ),
        f"- Current blocker: {checkpoint.get('current_blocker', '-') or '-'}",
        f"- Next step: {checkpoint.get('next_step', '-') or '-'}",
    ]
    key_files = [
        clip(str(item.get("path", "")).strip(), MAX_CHECKPOINT_PATH_CHARS)
        for item in checkpoint.get("key_files", [])
        if str(item.get("path", "")).strip()
    ]
    lines.append(f"- Key files: {', '.join(key_files) or '-'}")
    if checkpoint.get("completed"):
        lines.append(
            "- Completed: "
            + " | ".join(
                clip(str(item), MAX_CHECKPOINT_RESULT_CHARS)
                for item in checkpoint.get("completed", [])[-4:]
            )
        )
    if checkpoint.get("excluded"):
        lines.append(
            "- Excluded: "
            + " | ".join(
                clip(str(item), MAX_CHECKPOINT_RESULT_CHARS)
                for item in checkpoint.get("excluded", [])[-4:]
            )
        )
    if agent.resume_state.get("stale_paths"):
        lines.append("- Stale paths: " + ", ".join(agent.resume_state["stale_paths"]))
    summary = clip(str(checkpoint.get("summary", "")).strip(), 240)
    if summary:
        lines.append(f"- Summary: {summary}")
    return "\n".join(lines)


def infer_next_step(task_state):
    if task_state.status == "completed":
        return "No next step recorded."
    if task_state.stop_reason == "step_limit_reached":
        return "Resume from the latest checkpoint and continue the task."
    if task_state.last_tool:
        return f"Decide the next action after {task_state.last_tool}."
    return "Continue the task from the latest checkpoint."


def create_checkpoint(agent, task_state, user_message, trigger):
    state = checkpoint_state(agent)
    current = current_checkpoint(agent)
    checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
    key_files = []
    freshness = {}
    for path in agent.memory.to_dict()["working"]["recent_files"]:
        file_freshness = memorylib.file_freshness(path, agent.root)
        freshness[path] = file_freshness
        key_files.append(
            {
                "path": clip(path, MAX_CHECKPOINT_PATH_CHARS),
                "freshness": file_freshness,
            }
        )
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "created_at": now(),
        "current_goal": clip(str(user_message), MAX_CHECKPOINT_GOAL_CHARS),
        "completed": (
            [clip(task_state.final_answer, MAX_CHECKPOINT_RESULT_CHARS)]
            if task_state.final_answer
            else []
        ),
        "excluded": [],
        "current_blocker": "" if str(task_state.stop_reason or "") in ("", "final_answer_returned") else str(task_state.stop_reason),
        "next_step": infer_next_step(task_state),
        "key_files": key_files,
        "freshness": freshness,
        "summary": f"{trigger}: {clip(str(user_message), 120)}",
        "runtime_identity": current_runtime_identity(agent),
        "transaction": {
            "transaction_id": getattr(task_state, "transaction_id", ""),
            "state": getattr(task_state, "transaction_state", ""),
            "source_root": str(getattr(agent, "source_root", agent.root)),
            "execution_root": str(agent.root) if getattr(agent, "transaction_context", None) else "",
            "schema_version": "tsw-v1",
        },
    }
    state["items"][checkpoint_id] = checkpoint
    state["current_id"] = checkpoint_id
    while len(state["items"]) > MAX_CHECKPOINTS:
        oldest_id = next(iter(state["items"]))
        if oldest_id == checkpoint_id:
            break
        del state["items"][oldest_id]
    task_state.checkpoint_id = checkpoint_id
    agent.session["runtime_identity"] = checkpoint["runtime_identity"]
    agent.session_path = agent.session_store.save(agent.session)
    return checkpoint
