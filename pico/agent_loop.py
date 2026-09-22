"""Agent control loop extracted from the runtime facade."""

import json
import time

from .checkpoint import (
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from .completion import CompletionAdmission
from .context_projection import ContextProjector
from .progress import ProgressController
from .providers.clients import ProviderResponseError
from .session_store import SessionConflictError
from .task_state import TaskState
from .tools import native_tool_definitions
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def _persist_model_failure(self, task_state, user_message, exc, run_started_at, prompt_metadata):
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
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="model_error")
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
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        agent.progress_controller = None

    def _request_model(self, task_state, user_message, prompt, prompt_metadata, run_started_at, purpose):
        agent = self.agent
        agent.emit_trace(
            task_state,
            "model_requested",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                "purpose": purpose,
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
                agent.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
        except ProviderResponseError as exc:
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
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
                        "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                        "purpose": purpose,
                    },
                )
                return "", "retry", agent.redact_text(str(exc))
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        except Exception as exc:
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        raw = agent.redact_text(raw)
        kind, payload = agent.parse(raw)
        agent.emit_trace(
            task_state,
            "model_parsed",
            {
                "kind": kind,
                "completion_metadata": completion_metadata,
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                "purpose": purpose,
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
            },
        )
        model_started_at = time.monotonic()
        try:
            safe_input_items = agent.redact_artifact(input_items)
            safe_instructions = agent.redact_text(ContextProjector(agent).instructions())
            turn = agent.model_client.complete_turn(
                input_items=safe_input_items,
                tools=tools,
                max_new_tokens=turn_policy.max_output_tokens,
                instructions=safe_instructions,
                prompt_cache_key=agent.prefix_state.hash,
                prompt_cache_retention="in_memory",
                reasoning_effort=turn_policy.reasoning_effort,
            )
        except Exception as exc:
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        agent.emit_trace(
            task_state,
            "model_parsed",
            {
                "kind": turn.kind,
                "completion_metadata": completion_metadata,
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                "purpose": purpose,
                "native_tools": True,
                "response_status": turn.response_status,
                "incomplete_reason": turn.incomplete_reason,
                "protocol_error": turn.protocol_error,
            },
        )
        return turn

    def _finish_success(self, task_state, user_message, final, run_started_at):
        agent = self.agent
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        outcome = agent.finalize_transaction()
        task_state.transaction_state = outcome["state"]
        if outcome["state"] == "COMMITTED":
            task_state.finish_success(final)
        elif outcome["state"] == "READY_FOR_REVIEW":
            task_state.stop("ready_for_review", final_answer=final)
        else:
            task_state.stop("workspace_conflict", final_answer=final)
        agent.promote_durable_memory(user_message, final)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": "run_finished",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        agent.record_model_events([], execution_ledger={})
        agent.progress_controller = None
        return final

    def _close_aborted_run(self, user_message, stop_reason, error_text=""):
        agent = self.agent
        task_state = agent.current_task_state
        if task_state is None or task_state.status != "running":
            return
        final = {
            "interrupted": "Run interrupted; the staged workspace was preserved for resume.",
            "session_revision_conflict": "Run stopped because another process updated this session.",
        }.get(stop_reason, "Run failed because the runtime raised an unexpected error.")
        if stop_reason == "interrupted":
            task_state.stop_interrupted(final)
        elif stop_reason == "session_revision_conflict":
            task_state.stop_session_conflict(final)
        else:
            task_state.stop_runtime_error(final)

        close_errors = []
        transaction = getattr(agent, "transaction_context", None)
        if transaction is not None:
            try:
                transaction.workspace.interrupt(stop_reason)
                task_state.transaction_state = transaction.workspace.state
            except Exception as exc:  # noqa: BLE001 - preserve the original failure while closing audit state
                close_errors.append(agent.redact_text(str(exc)))

        agent.run_store.write_task_state(task_state)
        checkpoint = None
        if stop_reason != "session_revision_conflict":
            try:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger=stop_reason)
                agent.run_store.write_task_state(task_state)
            except Exception as exc:  # noqa: BLE001 - task_state/trace must still be closed
                close_errors.append(agent.redact_text(str(exc)))
        event_name = {
            "interrupted": "run_interrupted",
            "session_revision_conflict": "session_revision_conflict",
        }.get(stop_reason, "runtime_failed")
        agent.emit_trace(
            task_state,
            event_name,
            {
                "error": error_text,
                "checkpoint_id": checkpoint["checkpoint_id"] if checkpoint else "",
                "close_errors": close_errors,
            },
        )
        started_at = getattr(agent, "current_run_started_at", None)
        duration_ms = int((time.monotonic() - started_at) * 1000) if started_at else 0
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": duration_ms,
            },
        )
        agent.run_store.write_report(
            task_state,
            agent.redact_artifact(agent.build_report(task_state)),
        )
        agent.progress_controller = None

    def run(self, user_message):
        agent = self.agent
        try:
            return self._run_active(user_message)
        except KeyboardInterrupt:
            self._close_aborted_run(user_message, "interrupted")
            raise
        except SessionConflictError as exc:
            self._close_aborted_run(
                user_message,
                "session_revision_conflict",
                agent.redact_text(str(exc)),
            )
            raise
        except Exception as exc:
            self._close_aborted_run(
                user_message,
                "runtime_error",
                agent.redact_text(str(exc)),
            )
            raise
        finally:
            lease = getattr(agent, "current_run_lease", None)
            if lease is not None:
                lease.release()
                agent.current_run_lease = None
            agent.current_run_started_at = None

    def _run_active(self, user_message):
        agent = self.agent
        user_message = agent.redact_text(user_message)
        run_started_at = time.monotonic()
        agent.current_run_started_at = run_started_at
        task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_run_lease = agent.run_store.acquire_run_lease(task_state, blocking=True)
        if agent.current_run_lease is None:
            raise RuntimeError("could not acquire run owner lease")
        agent.current_run_dir = agent.run_store.start_run(task_state)

        if agent.transaction_context is None:
            agent.begin_transaction()
        if agent.transaction_context is not None:
            task_state.transaction_id = agent.transaction_context.transaction_id
            task_state.transaction_state = agent.transaction_context.workspace.state
            agent.run_store.write_task_state(task_state)
        agent.memory.set_task_summary(user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})
        controller = ProgressController(
            max_steps=agent.max_steps,
            read_only=agent.read_only,
            soft_discovery_limit=agent.soft_discovery_limit,
            hard_discovery_limit=agent.hard_discovery_limit,
            ledger=agent.session.get("execution_ledger", {}),
        )
        agent.progress_controller = controller
        native_mode = bool(
            getattr(agent.model_client, "supports_native_tools", False)
            and hasattr(agent.model_client, "complete_turn")
        )
        model_events = list(agent.session.get("model_events", [])) if native_mode else []
        projector = ContextProjector(agent) if native_mode else None
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        attempts = 0
        budget_final = None
        stuck_final = None
        contract_failure_final = None
        contract_failures = 0
        model_notice = ""
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while task_state.tool_steps < agent.max_steps and attempts < max_attempts:
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            progress_notice = controller.consume_notice()
            combined_notice = "\n".join(
                notice for notice in (progress_notice, model_notice) if notice
            )
            model_notice = ""
            if native_mode:
                agent.refresh_prefix()
                input_items, prompt_metadata = projector.build(
                    user_message, model_events, controller, notice=combined_notice
                )
                prompt = ""
            else:
                prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            if combined_notice:
                if not native_mode:
                    prompt += "\n\n" + combined_notice
                prompt_metadata["progress_intervention"] = controller.state.intervention_level
                agent.emit_trace(
                    task_state,
                    "progress_intervention",
                    {
                        "level": controller.state.intervention_level,
                        **controller.metrics(),
                    },
                )
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            native_turn = None
            turn_policy = agent.model_execution_policy.for_turn(
                purpose="action",
                max_output_tokens=agent.max_new_tokens,
                read_only=agent.read_only,
                recovering=contract_failures > 0,
            )
            if native_mode:
                native_turn = self._request_native_model(
                    task_state,
                    user_message,
                    input_items,
                    prompt_metadata,
                    run_started_at,
                    purpose="action",
                    tools=native_tool_definitions(agent.tools),
                    turn_policy=turn_policy,
                )
                raw = agent.redact_text(native_turn.text)
                if native_turn.kind == "tool":
                    kind = "tool"
                    payload = {
                        "name": native_turn.tool_name,
                        "args": agent.redact_artifact(native_turn.tool_args),
                    }
                elif native_turn.kind == "final":
                    decision = CompletionAdmission.evaluate(native_turn)
                    if decision.accepted:
                        kind, payload = "final", decision.text
                    else:
                        kind, payload = "retry", decision.reason
                else:
                    reason = native_turn.incomplete_reason or native_turn.protocol_error or native_turn.kind
                    kind, payload = "retry", reason
            else:
                raw, kind, payload = self._request_model(
                    task_state,
                    user_message,
                    prompt,
                    prompt_metadata,
                    run_started_at,
                    purpose="action",
                )

            if kind == "tool":
                contract_failures = 0
                name = payload.get("name", "")
                args = payload.get("args", {})
                call_id = ""
                if native_turn is not None:
                    call_id = native_turn.call_id or f"call_{attempts}"
                    model_events.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False, sort_keys=True),
                        }
                    )
                tool_started_at = time.monotonic()
                tool_result = agent.execute_tool(name, args)
                result = tool_result.content
                if native_turn is not None:
                    model_events.append(
                        {"type": "function_call_output", "call_id": call_id, "output": result}
                    )
                    agent.record_model_events(model_events, execution_ledger=controller.ledger.to_dict())
                tool_metadata = dict(tool_result.metadata or {})
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
                        "created_at": now(),
                    }
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
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
                checkpoint_trigger = "tool_executed" if tool_metadata.get("executed") else "tool_rejected"
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger=checkpoint_trigger)
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": checkpoint_trigger,
                    },
                )
                if controller.state.stuck_detected:
                    stuck_final = (
                        "Stopped because the agent continued without observable progress after "
                        "the forced-decision intervention."
                    )
                    agent.emit_trace(
                        task_state,
                        "agent_stuck",
                        {"name": name, **controller.metrics()},
                    )
                    break
                continue

            if kind == "retry":
                contract_failures += 1
                task_state.record_model_contract_failure(
                    "incomplete"
                    if (
                        native_turn is not None
                        and native_turn.kind == "incomplete"
                        or prompt_metadata.get("contract_failure_kind") == "incomplete"
                    )
                    else "invalid"
                )
                agent.emit_trace(
                    task_state,
                    "model_contract_rejected",
                    {
                        "reason": str(payload),
                        "consecutive_failures": contract_failures,
                        "response_status": getattr(native_turn, "response_status", ""),
                    },
                )
                if contract_failures > turn_policy.max_contract_retries:
                    contract_failure_final = (
                        "Stopped because the model failed to produce a complete typed response "
                        f"after bounded recovery: {payload}"
                    )
                    break
                model_notice = (
                    "Runtime notice: the previous model response was not complete and was rejected "
                    f"({payload}). Produce exactly one complete function call or final answer."
                )
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                continue

            contract_failures = 0
            final = (payload or raw).strip()
            return self._finish_success(task_state, user_message, final, run_started_at)

        if task_state.tool_steps >= agent.max_steps:
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            if native_mode:
                input_items, prompt_metadata = projector.build(
                    user_message, model_events, controller, finalization=True
                )
                prompt = ""
            else:
                prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
                prompt += (
                    "\n\nRuntime notice: the tool budget is exhausted. Do not call another tool. "
                    "Use the evidence already present in the tool history and return exactly one "
                    "non-empty <final>...</final> answer."
                )
            prompt_metadata["finalization"] = True
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                    "purpose": "finalization",
                },
            )
            if native_mode:
                turn_policy = agent.model_execution_policy.for_turn(
                    purpose="finalization",
                    max_output_tokens=agent.max_new_tokens,
                    read_only=agent.read_only,
                    recovering=False,
                )
                native_turn = self._request_native_model(
                    task_state,
                    user_message,
                    input_items,
                    prompt_metadata,
                    run_started_at,
                    purpose="finalization",
                    tools=[],
                    turn_policy=turn_policy,
                )
                raw = agent.redact_text(native_turn.text)
                decision = CompletionAdmission.evaluate(native_turn)
                kind = "final" if decision.accepted else "retry"
                payload = decision.text if decision.accepted else decision.reason
            else:
                raw, kind, payload = self._request_model(
                    task_state,
                    user_message,
                    prompt,
                    prompt_metadata,
                    run_started_at,
                    purpose="finalization",
                )
            if kind == "final":
                final = (payload or raw).strip()
                transaction = getattr(agent, "transaction_context", None)
                if transaction is None or not transaction.workspace.diff():
                    return self._finish_success(task_state, user_message, final, run_started_at)
                budget_final = final

        if stuck_final is not None:
            final = stuck_final
            task_state.stop_stuck(final)
        elif contract_failure_final is not None:
            final = contract_failure_final
            task_state.stop_retry_limit(final)
        elif budget_final is not None:
            final = budget_final
            task_state.stop_step_limit(final)
        elif attempts >= max_attempts and task_state.tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.interrupt_transaction(task_state.stop_reason or "interrupted")
        if agent.transaction_context is not None:
            task_state.transaction_state = agent.transaction_context.workspace.state
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.promote_durable_memory(user_message, final)
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        agent.progress_controller = None
        return final
