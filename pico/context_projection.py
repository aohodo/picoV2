"""Project typed runtime state into a bounded Responses API input."""

import json


class ContextProjector:
    def __init__(self, agent, recent_event_limit=16):
        self.agent = agent
        self.recent_event_limit = int(recent_event_limit)

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
            "Use `python -m pytest` for Python tests so the active Pico interpreter is reused. "
        )
        return (
            "You are Pico, a local coding agent. Use the supplied function tools instead of inventing workspace facts. "
            "For repository changes, inspect only the files needed, make the requested change in the staged workspace, "
            "then run focused verification. Do not reread evidence already listed in Runtime state. "
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

    def build(self, user_message, events, controller, notice="", finalization=False):
        state = controller.runtime_state_view()
        state["memory"] = self.agent.memory_text()[:2400]
        state["interaction"] = dict(getattr(self.agent, "current_interaction", {}) or {})
        checkpoint = self.agent.render_checkpoint_text()
        if checkpoint:
            state["checkpoint"] = checkpoint[:1600]
        state["remaining_tool_steps"] = max(0, self.agent.max_steps - getattr(self.agent.current_task_state, "tool_steps", 0))
        state["transaction_state"] = (
            self.agent.transaction_context.workspace.state
            if self.agent.transaction_context is not None
            else "NONE"
        )
        if notice:
            state["intervention"] = notice
        if finalization:
            state["instruction"] = "Tool budget is exhausted. Return a final answer from existing evidence; do not call a tool."
        runtime_text = "Runtime state:\n" + json.dumps(state, ensure_ascii=False, sort_keys=True)
        selected_events = list(events[-self.recent_event_limit :])
        input_items = [
            {"role": "user", "content": [{"type": "input_text", "text": str(user_message)}]},
            *selected_events,
            {"role": "developer", "content": [{"type": "input_text", "text": runtime_text}]},
        ]
        metadata = {
            "native_tools": True,
            "typed_event_count": len(selected_events),
            "runtime_state_chars": len(runtime_text),
            "request_chars": len(str(user_message)),
        }
        return input_items, metadata
