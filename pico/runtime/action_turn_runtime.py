"""Action-turn nodes for Pico's LangGraph runtime."""

import time

from ..persistence.checkpoint_store import (
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from ..progress.verification_feedback import completion_feedback
from ..tools import native_tool_definitions
from ..workspace import now
from .completion_runtime import CompletionAdmission


class ActionTurnRuntime:
    """Build context, request one model decision, and apply its action."""

    def _graph_action_turn(self, state):
        agent = self.agent
        task_state = state["task_state"]
        controller = state["controller"]
        interaction = state["interaction"]
        user_message = state["user_message"]
        model_events = state["model_events"]

        if task_state.tool_steps >= agent.max_steps:
            return {"next_node": "finalization_turn_runtime"}
        if state["attempts"] >= state["max_attempts"]:
            return {"next_node": "stop_runtime"}

        controller.set_remaining_steps(agent.max_steps - task_state.tool_steps)
        attempts = state["attempts"] + 1
        task_state.record_attempt()
        agent.run_store.write_task_state(task_state)
        turn_policy, combined_notice, delivery_review_notice = (
            self._prepare_action_policy(state)
        )
        prompt_started_at = time.monotonic()
        if state["native_mode"]:
            agent.refresh_prefix()
            input_items, prompt_metadata = state["projector"].build(
                user_message,
                model_events,
                controller,
                notice=combined_notice,
                input_token_budget=turn_policy.max_input_tokens,
                chars_per_token=agent.model_execution_policy.chars_per_input_token(),
            )
            prompt = ""
        else:
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            controller.set_visible_text_context(prompt)
            input_items = []
        prompt = self._apply_action_notices(
            task_state,
            controller,
            prompt,
            prompt_metadata,
            combined_notice,
            delivery_review_notice,
            state["native_mode"],
        )
        prompt_metadata.update(state["prompt_metadata_base"])
        agent.emit_trace(
            task_state,
            "prompt_built",
            {
                "prompt_metadata": prompt_metadata,
                "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
            },
        )
        self._checkpoint_prompt_transition(task_state, user_message, prompt_metadata)
        raw, kind, payload, native_turn = self._request_action_decision(
            state,
            task_state,
            interaction,
            controller,
            prompt,
            input_items,
            prompt_metadata,
            turn_policy,
        )

        if kind in {"tool", "tool_batch"}:
            return self._graph_execute_tool_turn(
                state,
                attempts,
                task_state,
                controller,
                interaction,
                model_events,
                native_turn,
                raw,
                payload,
            )
        if kind == "retry":
            return self._graph_recover_action_turn(
                state,
                attempts,
                task_state,
                controller,
                native_turn,
                prompt_metadata,
                payload,
            )
        return self._propose_action_completion(
            state,
            attempts,
            task_state,
            controller,
            interaction,
            raw,
            payload,
        )

    def _prepare_action_policy(self, state):
        agent = self.agent
        controller = state["controller"]
        progress_notice = controller.consume_notice()
        delivery_review_notice = controller.delivery_review_notice()
        combined_notice = "\n".join(
            notice
            for notice in (
                progress_notice,
                state["model_notice"],
                delivery_review_notice,
            )
            if notice
        )
        turn_policy = agent.model_execution_policy.for_turn(
            purpose="action",
            max_output_tokens=agent.max_new_tokens,
            read_only=controller.read_only,
            recovering=state["contract_failures"] > 0,
            request_profile=state["request_profile"],
            recovery_reason=state["contract_failure_reason"],
            completion_metadata=agent.last_completion_metadata,
            recovery_attempt=state["contract_failures"],
            capabilities=getattr(agent.model_client, "capabilities", None),
        )
        return turn_policy, combined_notice, delivery_review_notice

    def _apply_action_notices(
        self,
        task_state,
        controller,
        prompt,
        prompt_metadata,
        combined_notice,
        delivery_review_notice,
        native_mode,
    ):
        agent = self.agent
        if combined_notice:
            if not native_mode:
                prompt += "\n\n" + combined_notice
            prompt_metadata["progress_intervention"] = (
                controller.state.intervention_level
            )
            agent.emit_trace(
                task_state,
                "progress_intervention",
                {"level": controller.state.intervention_level, **controller.metrics()},
            )
        if delivery_review_notice:
            controller.mark_delivery_review_presented()
            prompt_metadata["delivery_review"] = "presented"
            agent.emit_trace(
                task_state, "delivery_review_presented", controller.metrics()
            )
        return prompt

    def _request_action_decision(
        self,
        state,
        task_state,
        interaction,
        controller,
        prompt,
        input_items,
        prompt_metadata,
        turn_policy,
    ):
        agent = self.agent
        native_turn = None
        if state["native_mode"]:
            native_turn = self._request_native_model(
                task_state,
                state["user_message"],
                input_items,
                prompt_metadata,
                state["run_started_at"],
                purpose="action",
                tools=native_tool_definitions(
                    {
                        name: tool
                        for name, tool in agent.tools.items()
                        if name
                        in controller.admissible_tools(
                            {
                                candidate
                                for candidate in agent.tools
                                if interaction["mutation_allowed"]
                                or candidate
                                in {
                                    "list_files",
                                    "read_file",
                                    "read_files",
                                    "search",
                                    "inspect_repository",
                                }
                            }
                        )
                    }
                ),
                turn_policy=turn_policy,
            )
            raw = agent.redact_text(native_turn.text)
            if native_turn.kind in {"tool", "tool_batch"}:
                return (
                    raw,
                    native_turn.kind,
                    {
                        "name": native_turn.tool_name,
                        "args": agent.redact_artifact(native_turn.tool_args),
                    },
                    native_turn,
                )
            if native_turn.kind == "final":
                decision = CompletionAdmission.evaluate(native_turn)
                return (
                    raw,
                    "final" if decision.accepted else "retry",
                    decision.text if decision.accepted else decision.reason,
                    native_turn,
                )
            return (
                raw,
                "retry",
                native_turn.incomplete_reason
                or native_turn.protocol_error
                or native_turn.kind,
                native_turn,
            )
        raw, kind, payload = self._request_model(
            task_state,
            state["user_message"],
            prompt,
            prompt_metadata,
            state["run_started_at"],
            purpose="action",
            turn_policy=turn_policy,
        )
        return raw, kind, payload, native_turn

    def _propose_action_completion(
        self,
        state,
        attempts,
        task_state,
        controller,
        interaction,
        raw,
        payload,
    ):
        agent = self.agent
        contract_failure_reason = state["contract_failure_reason"]
        if contract_failure_reason:
            task_state.record_model_recovery()
            agent.emit_trace(
                task_state,
                "model_recovered",
                {
                    "previous_failure": contract_failure_reason,
                    "recovery_attempts": 1,
                    "kind": "final",
                },
            )
        final = (payload or raw).strip()
        completion_failure_reason = ""
        if interaction.get("mutation_allowed") and not agent.read_only:
            completion_failure_reason = agent.verification_failure_reason()
            if completion_failure_reason:
                model_notice = completion_feedback(
                    completion_failure_reason, controller.ledger
                )
                agent.emit_trace(
                    task_state,
                    "completion_deferred",
                    {"reason": completion_failure_reason},
                )
                agent.run_store.write_task_state(task_state)
                return {
                    "attempts": attempts,
                    "contract_failures": 0,
                    "contract_failure_reason": "",
                    "completion_failure_reason": completion_failure_reason,
                    "model_notice": model_notice,
                    "next_node": "action_turn_runtime",
                }
        controller.complete_delivery_review()
        task_state.record_progress(controller.metrics())
        return {
            "attempts": attempts,
            "contract_failures": 0,
            "contract_failure_reason": "",
            "completion_failure_reason": completion_failure_reason,
            "final_answer": final,
            "next_node": "delivery_runtime",
        }

    def _checkpoint_prompt_transition(self, task_state, user_message, prompt_metadata):
        agent = self.agent
        trigger = ""
        if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
            trigger = "freshness_mismatch"
        elif (
            prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS
        ):
            agent.emit_trace(
                task_state,
                "runtime_identity_mismatch",
                {
                    "fields": list(
                        prompt_metadata.get("runtime_identity_mismatch_fields", [])
                    ),
                },
            )
            trigger = "workspace_mismatch"
        elif prompt_metadata.get("budget_reductions"):
            trigger = "context_reduction"
        if not trigger:
            return
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=trigger)
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": trigger},
        )

    def _graph_execute_tool_turn(
        self,
        state,
        attempts,
        task_state,
        controller,
        interaction,
        model_events,
        native_turn,
        raw,
        payload,
    ):
        agent = self.agent
        if state["contract_failures"]:
            task_state.record_model_recovery()
            agent.emit_trace(
                task_state,
                "model_recovered",
                {
                    "previous_failure": state["contract_failure_reason"],
                    "recovery_attempts": state["contract_failures"],
                    "kind": "tool",
                },
            )
        public_note = (
            raw.strip() if native_turn is not None else raw.split("<tool", 1)[0].strip()
        )
        if public_note and "<" not in public_note:
            agent.memory.set_work_note(agent.redact_text(public_note))
            agent.session["memory"] = agent.memory.to_dict()
        if native_turn is not None and raw.strip():
            model_events.append({"role": "assistant", "content": raw})
        calls = self._tool_calls_for_turn(native_turn, payload, attempts)
        batch_failure_reason = ""
        finalization_requested = state["finalization_requested"]
        for name, args, call_id in calls:
            deferred_reason = self._deferred_tool_reason(
                task_state,
                name,
                batch_failure_reason,
                finalization_requested,
            )
            controller.set_remaining_steps(agent.max_steps - task_state.tool_steps)
            metadata = self._execute_tool_call(
                task_state,
                state["user_message"],
                controller,
                model_events,
                name,
                args,
                call_id=call_id,
                deferred_reason=deferred_reason,
            )
            if metadata.get("tool_status") != "ok":
                batch_failure_reason = (
                    "an earlier call failed; inspect its result before requesting "
                    "further actions"
                )
            elif (
                controller.state.delivery_review_pending
                and interaction.get("mutation_allowed")
                and not agent.read_only
                and not agent.verification_failure_reason()
            ):
                finalization_requested = True
        return self._route_after_tool_turn(
            state,
            attempts,
            task_state,
            controller,
            model_events,
            calls,
            finalization_requested,
        )

    def _tool_calls_for_turn(self, native_turn, payload, attempts):
        agent = self.agent
        if native_turn is not None and native_turn.tool_calls:
            return [
                (
                    call.name,
                    agent.redact_artifact(call.args),
                    call.call_id or f"call_{attempts}_{index}",
                )
                for index, call in enumerate(native_turn.tool_calls, 1)
            ]
        return [
            (
                payload.get("name", ""),
                payload.get("args", {}),
                (
                    native_turn.call_id or f"call_{attempts}"
                    if native_turn is not None
                    else ""
                ),
            )
        ]

    def _deferred_tool_reason(
        self,
        task_state,
        name,
        batch_failure_reason,
        finalization_requested,
    ):
        if task_state.tool_steps >= self.agent.max_steps:
            return "the configured tool budget was exhausted"
        if finalization_requested:
            return (
                "authoritative acceptance verification already passed; "
                "finalization is active"
            )
        if batch_failure_reason and self.agent.tools.get(name, {}).get("risky", True):
            return batch_failure_reason
        return ""

    def _route_after_tool_turn(
        self,
        state,
        attempts,
        task_state,
        controller,
        model_events,
        calls,
        finalization_requested,
    ):
        agent = self.agent
        updates = {
            "attempts": attempts,
            "contract_failures": 0,
            "contract_failure_reason": "",
            "model_notice": "",
            "model_events": model_events,
            "finalization_requested": finalization_requested,
        }
        if controller.state.stuck_detected:
            updates.update(
                {
                    "stuck_final": (
                        "Stopped because the agent continued without observable "
                        "progress after the forced-decision intervention."
                    ),
                    "next_node": "stop_runtime",
                }
            )
            agent.emit_trace(
                task_state,
                "agent_stuck",
                {"name": calls[-1][0], **controller.metrics()},
            )
        elif finalization_requested:
            updates["next_node"] = "finalization_turn_runtime"
            agent.emit_trace(
                task_state,
                "finalization_entered",
                {
                    "reason": "authoritative_acceptance_passed",
                    **controller.metrics(),
                },
            )
        else:
            updates["next_node"] = "action_turn_runtime"
        return updates

    def _graph_recover_action_turn(
        self,
        state,
        attempts,
        task_state,
        controller,
        native_turn,
        prompt_metadata,
        payload,
    ):
        agent = self.agent
        contract_failures = state["contract_failures"] + 1
        contract_failure_reason = str(payload)
        failure_kind = self._contract_failure_kind(native_turn, prompt_metadata)
        task_state.record_model_contract_failure(failure_kind)
        agent.emit_trace(
            task_state,
            "model_contract_rejected",
            {
                "reason": contract_failure_reason,
                "consecutive_failures": contract_failures,
                "response_status": getattr(native_turn, "response_status", ""),
            },
        )
        retry_permitted = self._action_recovery_permitted(
            state,
            controller,
            contract_failures,
            contract_failure_reason,
            failure_kind,
        )
        if not retry_permitted:
            return {
                "attempts": attempts,
                "contract_failures": contract_failures,
                "contract_failure_reason": contract_failure_reason,
                "contract_failure_final": (
                    "Stopped because the model failed to produce a complete typed "
                    f"response after bounded recovery: {payload}"
                ),
                "next_node": "stop_runtime",
            }
        model_notice = (
            "Runtime notice: the previous model response was not complete and was "
            f"rejected ({payload}). Return complete function calls with valid "
            "arguments, or a final answer."
        )
        agent.record({"role": "assistant", "content": payload, "created_at": now()})
        agent.run_store.write_task_state(task_state)
        return {
            "attempts": attempts,
            "contract_failures": contract_failures,
            "contract_failure_reason": contract_failure_reason,
            "model_notice": model_notice,
            "next_node": "action_turn_runtime",
        }

    @staticmethod
    def _contract_failure_kind(native_turn, prompt_metadata):
        if (
            native_turn is not None
            and native_turn.kind == "incomplete"
            or prompt_metadata.get("contract_failure_kind") == "incomplete"
        ):
            return "incomplete"
        return "invalid"

    def _action_recovery_permitted(
        self,
        state,
        controller,
        contract_failures,
        contract_failure_reason,
        failure_kind,
    ):
        if failure_kind != "incomplete":
            return contract_failures == 1
        policy = self.agent.model_execution_policy.for_turn(
            purpose="action",
            max_output_tokens=self.agent.max_new_tokens,
            read_only=controller.read_only,
            recovering=True,
            request_profile=state["request_profile"],
            recovery_reason=contract_failure_reason,
            completion_metadata=self.agent.last_completion_metadata,
            recovery_attempt=contract_failures,
            capabilities=getattr(self.agent.model_client, "capabilities", None),
        )
        return policy.recovery_permitted
