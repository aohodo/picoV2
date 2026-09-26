"""Prose-only finalization node for verified Pico work."""

import time

from .completion_runtime import CompletionAdmission


class FinalizationRuntime:
    """Produce a complete final answer without reopening tool execution."""

    def _graph_finalization_turn(self, state):
        agent = self.agent
        task_state = state["task_state"]
        controller = state["controller"]
        failures = state["finalization_failures"]
        reason = state["finalization_reason"]
        finalization_attempts = state["finalization_attempts"]
        if finalization_attempts >= state["max_finalization_attempts"]:
            return self._finalization_exhausted(reason)

        finalization_attempts += 1
        attempts = state["attempts"] + 1
        task_state.record_attempt()
        agent.run_store.write_task_state(task_state)
        turn_policy, recovery_notice, delivery_review_notice = (
            self._prepare_finalization_policy(state)
        )
        prompt_started_at = time.monotonic()
        prompt, input_items, prompt_metadata = self._build_finalization_input(
            state,
            turn_policy,
            recovery_notice,
        )
        if delivery_review_notice:
            controller.mark_delivery_review_presented()
            prompt_metadata["delivery_review"] = "presented"
            agent.emit_trace(
                task_state, "delivery_review_presented", controller.metrics()
            )
        prompt_metadata["finalization"] = True
        prompt_metadata["orchestrator"] = "langgraph"
        agent.emit_trace(
            task_state,
            "prompt_built",
            {
                "prompt_metadata": prompt_metadata,
                "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                "purpose": "finalization",
            },
        )
        raw, kind, payload, native_turn = self._request_finalization_decision(
            state,
            task_state,
            prompt,
            input_items,
            prompt_metadata,
            turn_policy,
        )
        if kind == "final":
            if failures:
                task_state.record_model_recovery()
                agent.emit_trace(
                    task_state,
                    "model_recovered",
                    {
                        "previous_failure": reason,
                        "recovery_attempts": failures,
                        "kind": "final",
                        "purpose": "finalization",
                    },
                )
            final = (payload or raw).strip()
            controller.complete_delivery_review()
            task_state.record_progress(controller.metrics())
            return {
                "attempts": attempts,
                "finalization_attempts": finalization_attempts,
                "final_answer": final,
                "next_node": "delivery_runtime",
            }
        return self._recover_finalization(
            state,
            attempts,
            finalization_attempts,
            task_state,
            controller,
            native_turn,
            prompt_metadata,
            payload,
        )

    @staticmethod
    def _finalization_exhausted(reason):
        return {
            "contract_failure_final": (
                "Stopped because finalization exhausted its independent request "
                f"budget: {reason}"
            ),
            "next_node": "stop_runtime",
        }

    def _prepare_finalization_policy(self, state):
        agent = self.agent
        controller = state["controller"]
        failures = state["finalization_failures"]
        reason = state["finalization_reason"]
        notices = []
        delivery_review_notice = controller.delivery_review_notice()
        if delivery_review_notice:
            notices.append(delivery_review_notice)
        if failures:
            notices.append(
                "Runtime notice: finalization was incomplete and rejected "
                f"({reason}). Regenerate one complete final answer from existing "
                "evidence. Do not call or describe another tool."
            )
        policy = agent.model_execution_policy.for_turn(
            purpose="finalization",
            max_output_tokens=agent.max_new_tokens,
            read_only=controller.read_only,
            recovering=failures > 0,
            request_profile=state["request_profile"],
            recovery_reason=reason,
            completion_metadata=agent.last_completion_metadata,
            recovery_attempt=failures,
            capabilities=getattr(agent.model_client, "capabilities", None),
        )
        return policy, "\n".join(notices), delivery_review_notice

    def _build_finalization_input(self, state, turn_policy, recovery_notice):
        user_message = state["user_message"]
        if state["native_mode"]:
            input_items, prompt_metadata = state["projector"].build(
                user_message,
                state["model_events"],
                state["controller"],
                notice=recovery_notice,
                finalization=True,
                input_token_budget=turn_policy.max_input_tokens,
                chars_per_token=self.agent.model_execution_policy.chars_per_input_token(),
            )
            return "", input_items, prompt_metadata
        prompt, prompt_metadata = self.agent._build_prompt_and_metadata(user_message)
        prompt += (
            "\n\nRuntime notice: finalization mode is active. Do not call "
            "another tool. Use existing evidence and return exactly one "
            "non-empty <final>...</final> answer."
        )
        if recovery_notice:
            prompt += "\n" + recovery_notice
        return prompt, [], prompt_metadata

    def _request_finalization_decision(
        self,
        state,
        task_state,
        prompt,
        input_items,
        prompt_metadata,
        turn_policy,
    ):
        if state["native_mode"]:
            turn = self._request_native_model(
                task_state,
                state["user_message"],
                input_items,
                prompt_metadata,
                state["run_started_at"],
                purpose="finalization",
                tools=[],
                turn_policy=turn_policy,
            )
            raw = self.agent.redact_text(turn.text)
            decision = CompletionAdmission.evaluate(turn)
            payload = (
                decision.text
                if decision.accepted
                else turn.incomplete_reason or turn.protocol_error or decision.reason
            )
            return raw, "final" if decision.accepted else "retry", payload, turn
        raw, kind, payload = self._request_model(
            task_state,
            state["user_message"],
            prompt,
            prompt_metadata,
            state["run_started_at"],
            purpose="finalization",
            turn_policy=turn_policy,
        )
        return raw, kind, payload, None

    def _recover_finalization(
        self,
        state,
        attempts,
        finalization_attempts,
        task_state,
        controller,
        native_turn,
        prompt_metadata,
        payload,
    ):
        agent = self.agent
        failures = state["finalization_failures"] + 1
        reason = str(payload)
        failure_kind = self._contract_failure_kind(native_turn, prompt_metadata)
        task_state.record_model_contract_failure(failure_kind)
        agent.emit_trace(
            task_state,
            "model_contract_rejected",
            {
                "reason": reason,
                "consecutive_failures": failures,
                "purpose": "finalization",
                "response_status": getattr(native_turn, "response_status", ""),
            },
        )
        retry_permitted = self._finalization_recovery_permitted(
            state, controller, failures, reason, failure_kind
        )
        updates = {
            "attempts": attempts,
            "finalization_attempts": finalization_attempts,
            "finalization_failures": failures,
            "finalization_reason": reason,
        }
        if not retry_permitted:
            updates.update(
                {
                    "contract_failure_final": (
                        "Stopped because finalization did not produce a complete "
                        f"typed answer after bounded recovery: {payload}"
                    ),
                    "next_node": "stop_runtime",
                }
            )
        elif finalization_attempts >= state["max_finalization_attempts"]:
            updates.update(self._finalization_exhausted(reason))
        else:
            updates["next_node"] = "finalization_turn_runtime"
        return updates

    def _finalization_recovery_permitted(
        self, state, controller, failures, reason, failure_kind
    ):
        if failure_kind != "incomplete":
            return failures == 1
        policy = self.agent.model_execution_policy.for_turn(
            purpose="finalization",
            max_output_tokens=self.agent.max_new_tokens,
            read_only=controller.read_only,
            recovering=True,
            request_profile=state["request_profile"],
            recovery_reason=reason,
            completion_metadata=self.agent.last_completion_metadata,
            recovery_attempt=failures,
            capabilities=getattr(self.agent.model_client, "capabilities", None),
        )
        return policy.recovery_permitted
