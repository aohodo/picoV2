"""Structured tool execution for the agent runtime."""

import re
from dataclasses import dataclass

from .interaction_policy import path_matches_patterns
from .text_document import TextDecodingError, read_text_document
from .workspace import clip


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
    executed=False,
    validation=False,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
        "executed": bool(executed),
        "validation": bool(validation),
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    return result


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def _progress_args(self, name, args):
        """Normalize deterministic reads to the evidence they can actually return."""
        if name == "read_file":
            path = self.agent.path(args["path"])
            try:
                line_count = len(read_text_document(path).text.splitlines())
            except (OSError, TextDecodingError):
                line_count = 0
            start = int(args.get("start", 1))
            end = min(int(args.get("end", 200)), line_count)
            return {"path": path.relative_to(self.agent.root).as_posix(), "start": start, "end": end}
        if name == "read_files":
            files = []
            for raw_path in args.get("paths", []):
                path = self.agent.path(raw_path)
                try:
                    line_count = len(read_text_document(path).text.splitlines())
                except (OSError, TextDecodingError):
                    line_count = 0
                files.append({"path": path.relative_to(self.agent.root).as_posix(), "start": 1, "end": min(500, line_count)})
            return {"files": files}
        if name in {"list_files", "search"}:
            normalized = dict(args)
            path = self.agent.path(args.get("path", "."))
            normalized["path"] = path.relative_to(self.agent.root).as_posix() or "."
            return normalized
        return args

    def _finalize(self, name, args, result, progress_recorded=False, progress_args=None):
        controller = getattr(self.agent, "progress_controller", None)
        if controller is None:
            return result
        if not progress_recorded:
            evidence = controller.observe(name, progress_args or args, result.content, result.metadata)
            result.metadata.update(
                {
                    "progress_evidence": evidence.kind,
                    "observation_hash": evidence.observation_hash,
                    "progress_reason": evidence.reason,
                }
            )
        result.metadata.update(controller.metrics())
        return result

    def execute(self, name, args):
        agent = self.agent
        args = agent.redact_artifact(args or {})
        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return self._finalize(name, args, ToolExecutionResult(
                content=f"error: tool '{name}' is not allowed in this run",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="tool_not_allowed",
                    risk_level="high",
                    read_only=False,
                ),
            ))

        tool = agent.tools.get(name)
        if tool is None:
            return self._finalize(name, args, ToolExecutionResult(
                content=f"error: unknown tool '{name}'",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="unknown_tool",
                    risk_level="high",
                    read_only=False,
                ),
            ))

        interaction = getattr(agent, "current_interaction", {}) or {}
        if (
            name in {"write_file", "patch_file"}
            and interaction
            and not interaction.get("mutation_allowed", True)
        ):
            return self._finalize(name, args, ToolExecutionResult(
                content=(
                    f"error: interaction_read_only for {name}; the current request was classified "
                    "as explanation, review, planning, or discussion and does not authorize mutation. "
                    "Ask the user for an explicit implementation request before changing the workspace."
                ),
                metadata=_metadata(
                    "rejected",
                    tool_error_code="interaction_read_only",
                    security_event_type="read_only_block",
                    risk_level="high",
                    read_only=False,
                ),
            ))

        if name in {"write_file", "patch_file"} and path_matches_patterns(
            args.get("path", ""), interaction.get("protected_paths", [])
        ):
            return self._finalize(name, args, ToolExecutionResult(
                content=(
                    f"error: scope_constraint for {name}; {args.get('path', '')} is protected by "
                    "an explicit no-modification instruction in the current request."
                ),
                metadata=_metadata(
                    "rejected",
                    tool_error_code="scope_constraint",
                    security_event_type="scope_constraint",
                    risk_level="high",
                    read_only=False,
                    affected_paths=[str(args.get("path", ""))],
                ),
            ))

        controller = getattr(agent, "progress_controller", None)
        if controller is not None and not controller.preflight_known_error(name, args):
            return self._finalize(name, args, ToolExecutionResult(
                content=(
                    f"error: repeated_invalid_call for {name}; this exact action already failed "
                    "validation. Correct the arguments instead of retrying it."
                ),
                metadata=_metadata(
                    "rejected",
                    tool_error_code="repeated_invalid_call",
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            ), progress_recorded=True)

        try:
            agent.validate_tool(name, args)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            if controller is not None:
                controller.record_rejected_action(name, args)
            example = agent.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            return self._finalize(name, args, ToolExecutionResult(
                content=message,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            ))

        progress_args = self._progress_args(name, args)
        preflight = controller.preflight(name, progress_args) if controller is not None else {"allowed": True}
        if not preflight["allowed"]:
            evidence = preflight["evidence"]
            metadata = _metadata(
                "rejected",
                tool_error_code="repeated_no_progress",
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
            )
            metadata.update(
                {
                    "progress_evidence": evidence.kind,
                    "observation_hash": evidence.observation_hash,
                    "progress_reason": evidence.reason,
                }
            )
            return self._finalize(name, args, ToolExecutionResult(
                content=(
                    f"error: repeated_no_progress for {name}; this exact read-only call already "
                    "succeeded and the workspace has not changed. Reuse the previous result or "
                    "choose a materially different action."
                ),
                metadata=metadata,
            ), progress_recorded=True, progress_args=progress_args)

        if tool["risky"] and not agent.approve(name, args):
            return self._finalize(name, args, ToolExecutionResult(
                content=f"error: approval denied for {name}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="approval_denied",
                    security_event_type="read_only_block" if agent.read_only else "approval_denied",
                    risk_level="high",
                    read_only=False,
                ),
            ))

        before_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        try:
            content = agent.redact_text(clip(tool["run"](args)))
            if agent.transaction_context is not None:
                agent.transaction_context.workspace.enforce_storage_limit()
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            if name in {"run_shell", "run_verification"}:
                match = re.search(r"exit_code:\s*(-?\d+)", content)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            agent.update_memory_after_tool(name, args, content)
            if workspace_changed and name != "run_verification" and agent.last_verification_succeeded is not None:
                agent.last_verification_succeeded = None
                agent.last_shell_validation_succeeded = None
                agent.verification_stale = True
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
                executed=True,
                validation=(name == "run_verification"),
            )
            agent.record_process_note_for_tool(name, metadata)
            if metadata["validation"]:
                agent.last_verification_succeeded = tool_status == "ok"
                agent.last_shell_validation_succeeded = agent.last_verification_succeeded
                agent.verification_stale = False
            return self._finalize(name, args, ToolExecutionResult(content=content, metadata=metadata), progress_args=progress_args)
        except Exception as exc:  # noqa: BLE001 - arbitrary tool implementations terminate at this boundary
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            if workspace_changed and name != "run_verification" and agent.last_verification_succeeded is not None:
                agent.last_verification_succeeded = None
                agent.last_shell_validation_succeeded = None
                agent.verification_stale = True
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            explicit_error_code = str(getattr(exc, "code", "") or "")
            error_code = explicit_error_code or ("tool_partial_success" if workspace_changed else "tool_failed")
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code=error_code,
                security_event_type=security_event_type,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
                executed=True,
                validation=(name == "run_verification"),
            )
            agent.record_process_note_for_tool(name, metadata)
            if metadata["validation"]:
                agent.last_verification_succeeded = False
                agent.last_shell_validation_succeeded = False
                agent.verification_stale = False
            return self._finalize(
                name,
                args,
                ToolExecutionResult(
                    content=agent.redact_text(f"error: tool {name} failed: {exc}"),
                    metadata=metadata,
                ),
                progress_args=progress_args,
            )
