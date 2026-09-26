"""Human-readable projection of authoritative runtime events."""

import sys


class ConsoleProgressRenderer:
    def __init__(self, stream=None, max_steps=None):
        self.stream = stream or sys.stderr
        self.max_steps = max_steps

    @staticmethod
    def _target(args):
        args = args or {}
        if args.get("path"):
            return str(args["path"])
        if args.get("paths"):
            return ", ".join(str(item) for item in list(args["paths"])[:3])
        if args.get("command"):
            command = str(args["command"]).replace("\n", " ")
            return command[:100] + ("..." if len(command) > 100 else "")
        return ""

    def __call__(self, event, payload, task_state):
        line = ""
        if event == "run_started":
            line = f"[pico] started | budget {task_state.tool_steps}/{self.max_steps or '?'}"
        elif event == "model_requested":
            line = f"[pico] model turn {payload.get('attempts', 0)} | {payload.get('purpose', 'action')}..."
        elif event == "model_parsed":
            retries = int((payload.get("completion_metadata") or {}).get("transport_retries", 0))
            retry_text = f" | transport retries {retries}" if retries else ""
            line = f"[pico] model {payload.get('duration_ms', 0) / 1000:.2f}s | {payload.get('kind', 'unknown')}{retry_text}"
        elif event == "tool_started":
            target = self._target(payload.get("args"))
            line = (
                f"[pico] step {payload.get('next_step', task_state.tool_steps + 1)} | "
                f"{payload.get('name', 'tool')}"
                f"{f' | {target}' if target else ''} | running..."
            )
        elif event == "tool_executed":
            target = self._target(payload.get("args"))
            status = payload.get("tool_status", "unknown")
            line = (
                f"[pico] step {task_state.tool_steps} | {payload.get('name', 'tool')}"
                f"{f' | {target}' if target else ''} | {status} | {payload.get('duration_ms', 0)}ms"
            )
        elif event == "progress_intervention":
            line = f"[pico] intervention | {payload.get('level', 'unknown')}"
        elif event == "model_contract_rejected":
            line = f"[pico] model response rejected | retry {payload.get('consecutive_failures', 0)}"
        elif event == "model_recovered":
            line = (
                f"[pico] model response recovered | {payload.get('kind', 'unknown')}"
                f" | after {payload.get('recovery_attempts', 1)} retry"
            )
        elif event == "model_failed":
            retry_text = (
                f" | transport retries {task_state.provider_retry_count}"
                if task_state.provider_retry_count
                else ""
            )
            line = (
                f"[pico] provider failed | {payload.get('error_code', 'model_request_failed')}"
                f"{retry_text}"
            )
        elif event == "durable_memory_admitted":
            line = f"[pico] durable memory | {len(payload.get('promoted', []))} stored"
        elif event == "run_finished":
            delivered_paths = list(getattr(task_state, "delivered_paths", []))
            staged_paths = list(getattr(task_state, "staged_paths", []))
            if delivered_paths:
                change_text = f"delivered {len(delivered_paths)}"
            elif staged_paths:
                change_text = f"staged {len(staged_paths)}"
            else:
                change_text = f"changed {len(getattr(task_state, 'changed_paths', []))}"
            line = (
                f"[pico] finished | {payload.get('stop_reason', 'unknown')}"
                f" | {change_text}"
                f" | validation {getattr(task_state, 'validation_status', 'not_run')}"
                f" | {payload.get('run_duration_ms', 0) / 1000:.2f}s"
            )
        elif event == "run_interrupted":
            line = f"[pico] interrupted | staged changes preserved | step {task_state.tool_steps}"
        if line:
            print(line, file=self.stream, flush=True)
