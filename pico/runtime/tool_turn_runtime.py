"""Auditable execution of model-requested tool calls."""

import json
import time

from ..tools.tool_executor import ToolExecutionResult
from ..workspace import clip, now


class ToolTurnRuntime:
    """Execute one tool call and persist all resulting evidence."""

    def _execute_tool_call(
        self,
        task_state,
        user_message,
        controller,
        model_events,
        name,
        args,
        call_id="",
        deferred_reason="",
    ):
        agent = self.agent
        if call_id:
            model_events.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False, sort_keys=True),
                }
            )
        display_step = task_state.tool_steps + 1
        agent.emit_trace(
            task_state,
            "tool_started",
            {
                "name": name,
                "args": agent.compact_tool_args(args),
                "step": display_step,
            },
        )
        tool_started_at = time.monotonic()
        tool_result = (
            ToolExecutionResult(
                content=f"error: batch_call_deferred: {deferred_reason}",
                metadata={
                    "executed": False,
                    "tool_status": "rejected",
                    "tool_error_code": "batch_call_deferred",
                },
            )
            if deferred_reason
            else agent.execute_tool(name, args)
        )
        result = tool_result.content
        tool_duration_ms = int((time.monotonic() - tool_started_at) * 1000)
        task_state.record_tool_duration(tool_duration_ms)
        if call_id:
            model_events.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                    "_read_evidence": tool_result.metadata.get("read_evidence", []),
                }
            )
            agent.record_model_events(
                model_events, execution_ledger=controller.ledger.to_dict()
            )
        tool_metadata = dict(tool_result.metadata or {})
        task_state.record_tool_evidence(name, args, tool_metadata)
        if tool_metadata.get("executed"):
            task_state.record_tool(
                name,
                workspace_changed=tool_metadata.get("workspace_changed", False),
            )
        task_state.record_progress(controller.metrics())
        agent.record(
            {
                "role": "tool",
                "name": name,
                "args": args,
                "content": result,
                "read_evidence": tool_metadata.get("read_evidence", []),
                "created_at": now(),
            }
        )
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "tool_executed",
            {
                "name": name,
                "args": agent.compact_tool_args(args),
                "step": display_step,
                "result": clip(result, 500),
                "duration_ms": tool_duration_ms,
                **tool_metadata,
            },
        )
        agent.emit_trace(
            task_state,
            "progress_observed",
            {
                "name": name,
                "evidence": tool_metadata.get("progress_evidence", ""),
                "reason": tool_metadata.get("progress_reason", ""),
                "executed": bool(tool_metadata.get("executed")),
                **controller.metrics(),
            },
        )
        if tool_metadata.get("tool_error_code") == "repeated_no_progress":
            agent.emit_trace(
                task_state,
                "repeated_action_blocked",
                {"name": name, "args": args, **controller.metrics()},
            )
        checkpoint_trigger = (
            "tool_executed" if tool_metadata.get("executed") else "tool_rejected"
        )
        checkpoint = agent.create_checkpoint(
            task_state, user_message, trigger=checkpoint_trigger
        )
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": checkpoint_trigger,
            },
        )
        return tool_metadata
