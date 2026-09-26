"""Provider request lifecycle for Pico model turns."""

import time

from ..context.context_projection import ContextProjector
from ..domain.model_contract import ModelTurn
from ..providers.clients import ProviderResponseError


class ModelTurnRuntime:
    """Request, measure, validate, and recover model turns."""

    def _persist_model_failure(
        self,
        task_state,
        user_message,
        exc,
        run_started_at,
        prompt_metadata,
    ):
        agent = self.agent
        error_code = str(getattr(exc, "code", "") or "model_request_failed")
        if error_code == "provider_transport_incomplete":
            task_state.record_model_transport_failure()
        agent.interrupt_transaction("model_error")
        if agent.transaction_context is not None:
            task_state.transaction_state = agent.transaction_context.workspace.state
        error_text = agent.redact_text(str(exc))
        final = f"Model request failed: {error_text}"
        task_state.stop_model_error(final)
        agent.capture_run_outcome(task_state)
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(
            task_state, user_message, trigger="model_error"
        )
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "model_failed",
            {
                "error": error_text,
                "error_code": error_code,
                "completion_metadata": dict(agent.last_completion_metadata),
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.last_prompt_metadata = dict(prompt_metadata)
        agent.run_store.write_report(
            task_state, agent.redact_artifact(agent.build_report(task_state))
        )
        agent.progress_controller = None

    def _request_model(
        self,
        task_state,
        user_message,
        prompt,
        prompt_metadata,
        run_started_at,
        purpose,
        turn_policy,
    ):
        agent = self.agent
        agent.emit_trace(
            task_state,
            "model_requested",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                "purpose": purpose,
                "reasoning_effort": turn_policy.reasoning_effort,
                "max_output_tokens": turn_policy.max_output_tokens,
                "budget_decision": turn_policy.decision_reason,
            },
        )
        prompt_cache_key = None
        prompt_cache_retention = None
        if getattr(agent.model_client, "supports_prompt_cache", False):
            prompt_cache_key = prompt_metadata.get("prompt_cache_key")
            prompt_cache_retention = "in_memory"
        model_started_at = time.monotonic()
        try:
            raw = agent.model_client.complete(
                prompt,
                turn_policy.max_output_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
        except ProviderResponseError as exc:
            return self._handle_text_provider_error(
                task_state,
                user_message,
                exc,
                run_started_at,
                prompt_metadata,
                purpose,
                turn_policy,
                model_started_at,
            )
        except Exception as exc:
            self._record_unexpected_model_failure(
                task_state,
                user_message,
                exc,
                run_started_at,
                prompt_metadata,
                turn_policy,
                model_started_at,
            )
            raise
        completion_metadata, model_duration_ms = self._record_model_success(
            task_state, prompt_metadata, turn_policy, model_started_at
        )
        raw = agent.redact_text(raw)
        kind, payload = agent.parse(raw)
        agent.emit_trace(
            task_state,
            "model_parsed",
            {
                "kind": kind,
                "completion_metadata": completion_metadata,
                "duration_ms": model_duration_ms,
                "purpose": purpose,
                "budget_decision": turn_policy.decision_reason,
            },
        )
        return raw, kind, payload

    def _request_native_model(
        self,
        task_state,
        user_message,
        input_items,
        prompt_metadata,
        run_started_at,
        purpose,
        tools,
        turn_policy,
    ):
        agent = self.agent
        agent.emit_trace(
            task_state,
            "model_requested",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "purpose": purpose,
                "native_tools": True,
                "reasoning_effort": turn_policy.reasoning_effort,
                "max_output_tokens": turn_policy.max_output_tokens,
                "budget_decision": turn_policy.decision_reason,
            },
        )
        model_started_at = time.monotonic()
        try:
            safe_input_items = agent.redact_artifact(input_items)
            safe_instructions = agent.redact_text(
                ContextProjector(agent).instructions()
            )
            turn = agent.model_client.complete_turn(
                input_items=safe_input_items,
                tools=tools,
                max_new_tokens=turn_policy.max_output_tokens,
                instructions=safe_instructions,
                prompt_cache_key=agent.prefix_state.hash,
                prompt_cache_retention="in_memory",
                reasoning_effort=turn_policy.reasoning_effort,
            )
        except ProviderResponseError as exc:
            return self._handle_native_provider_error(
                task_state,
                user_message,
                exc,
                run_started_at,
                prompt_metadata,
                purpose,
                turn_policy,
                model_started_at,
            )
        except Exception as exc:
            self._record_unexpected_model_failure(
                task_state,
                user_message,
                exc,
                run_started_at,
                prompt_metadata,
                turn_policy,
                model_started_at,
            )
            raise
        completion_metadata, model_duration_ms = self._record_model_success(
            task_state, prompt_metadata, turn_policy, model_started_at
        )
        agent.emit_trace(
            task_state,
            "model_parsed",
            {
                "kind": turn.kind,
                "completion_metadata": completion_metadata,
                "duration_ms": model_duration_ms,
                "purpose": purpose,
                "native_tools": True,
                "response_status": turn.response_status,
                "incomplete_reason": turn.incomplete_reason,
                "protocol_error": turn.protocol_error,
                "budget_decision": turn_policy.decision_reason,
            },
        )
        return turn

    def _handle_text_provider_error(
        self,
        task_state,
        user_message,
        exc,
        run_started_at,
        prompt_metadata,
        purpose,
        turn_policy,
        model_started_at,
    ):
        agent = self.agent
        _, model_duration_ms = self._record_provider_failure(
            task_state, exc, prompt_metadata, turn_policy, model_started_at
        )
        if exc.code in {
            "provider_incomplete",
            "provider_empty_output",
            "provider_invalid_envelope",
        }:
            prompt_metadata["contract_failure_kind"] = (
                "incomplete" if exc.code == "provider_incomplete" else "invalid"
            )
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": "retry",
                    "provider_error_code": exc.code,
                    "duration_ms": model_duration_ms,
                    "purpose": purpose,
                },
            )
            return "", "retry", agent.redact_text(str(exc))
        self._persist_model_failure(
            task_state, user_message, exc, run_started_at, prompt_metadata
        )
        raise exc

    def _handle_native_provider_error(
        self,
        task_state,
        user_message,
        exc,
        run_started_at,
        prompt_metadata,
        purpose,
        turn_policy,
        model_started_at,
    ):
        agent = self.agent
        _, model_duration_ms = self._record_provider_failure(
            task_state, exc, prompt_metadata, turn_policy, model_started_at
        )
        if exc.code in {
            "provider_incomplete",
            "provider_empty_output",
            "provider_invalid_envelope",
        }:
            kind = "incomplete" if exc.code == "provider_incomplete" else "invalid"
            reason = agent.redact_text(str(exc))
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "provider_error_code": exc.code,
                    "duration_ms": model_duration_ms,
                    "purpose": purpose,
                    "native_tools": True,
                },
            )
            return ModelTurn(
                kind=kind,
                response_status="incomplete" if kind == "incomplete" else "",
                incomplete_reason=reason if kind == "incomplete" else "",
                protocol_error=reason if kind == "invalid" else "",
            )
        self._persist_model_failure(
            task_state, user_message, exc, run_started_at, prompt_metadata
        )
        raise exc

    def _record_provider_failure(
        self,
        task_state,
        exc,
        prompt_metadata,
        turn_policy,
        model_started_at,
    ):
        agent = self.agent
        model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
        completion_metadata = dict(
            getattr(agent.model_client, "last_completion_metadata", {}) or {}
        )
        completion_metadata["requested_output_tokens"] = turn_policy.max_output_tokens
        task_state.record_model_duration(
            model_duration_ms,
            completion_metadata.get(
                "transport_retries", getattr(exc, "attempts", 1) - 1
            ),
        )
        prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.model_execution_policy.observe(turn_policy, completion_metadata)
        agent.session["model_usage_samples"] = (
            agent.model_execution_policy.usage_snapshot()
        )
        agent.last_prompt_metadata = prompt_metadata
        return completion_metadata, model_duration_ms

    def _record_unexpected_model_failure(
        self,
        task_state,
        user_message,
        exc,
        run_started_at,
        prompt_metadata,
        turn_policy,
        model_started_at,
    ):
        agent = self.agent
        model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
        completion_metadata = dict(
            getattr(agent.model_client, "last_completion_metadata", {}) or {}
        )
        task_state.record_model_duration(
            model_duration_ms,
            completion_metadata.get(
                "transport_retries", getattr(exc, "attempts", 1) - 1
            ),
        )
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        self._persist_model_failure(
            task_state, user_message, exc, run_started_at, prompt_metadata
        )

    def _record_model_success(
        self, task_state, prompt_metadata, turn_policy, model_started_at
    ):
        agent = self.agent
        completion_metadata = dict(
            getattr(agent.model_client, "last_completion_metadata", {}) or {}
        )
        completion_metadata["requested_output_tokens"] = turn_policy.max_output_tokens
        prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.model_execution_policy.observe(turn_policy, completion_metadata)
        agent.session["model_usage_samples"] = (
            agent.model_execution_policy.usage_snapshot()
        )
        model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
        task_state.record_model_duration(
            model_duration_ms, completion_metadata.get("transport_retries", 0)
        )
        agent.last_prompt_metadata = prompt_metadata
        return completion_metadata, model_duration_ms
