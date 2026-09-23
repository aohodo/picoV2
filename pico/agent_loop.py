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
from .model_contract import ModelTurn
from .progress import ProgressController
from .providers.clients import ProviderResponseError
from .session_store import SessionConflictError
from .task_state import TaskState
from .tool_executor import ToolExecutionResult
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
        agent.capture_run_outcome(task_state)
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
            model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            completion_metadata["requested_output_tokens"] = (
                turn_policy.max_output_tokens
            )
            task_state.record_model_duration(
                model_duration_ms,
                completion_metadata.get("transport_retries", getattr(exc, "attempts", 1) - 1),
            )
            prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.model_execution_policy.observe(turn_policy, completion_metadata)
            agent.session["model_usage_samples"] = (
                agent.model_execution_policy.usage_snapshot()
            )
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
                        "duration_ms": model_duration_ms,
                        "purpose": purpose,
                    },
                )
                return "", "retry", agent.redact_text(str(exc))
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        except Exception as exc:
            model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            task_state.record_model_duration(
                model_duration_ms,
                completion_metadata.get("transport_retries", getattr(exc, "attempts", 1) - 1),
            )
            if completion_metadata:
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        completion_metadata["requested_output_tokens"] = turn_policy.max_output_tokens
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.model_execution_policy.observe(turn_policy, completion_metadata)
        agent.session["model_usage_samples"] = (
            agent.model_execution_policy.usage_snapshot()
        )
        model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
        task_state.record_model_duration(model_duration_ms, completion_metadata.get("transport_retries", 0))
        agent.last_prompt_metadata = prompt_metadata
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
        except ProviderResponseError as exc:
            model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            completion_metadata["requested_output_tokens"] = turn_policy.max_output_tokens
            task_state.record_model_duration(
                model_duration_ms,
                completion_metadata.get("transport_retries", getattr(exc, "attempts", 1) - 1),
            )
            prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.model_execution_policy.observe(turn_policy, completion_metadata)
            agent.session["model_usage_samples"] = agent.model_execution_policy.usage_snapshot()
            agent.last_prompt_metadata = prompt_metadata
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
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        except Exception as exc:
            model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            task_state.record_model_duration(
                model_duration_ms,
                completion_metadata.get("transport_retries", getattr(exc, "attempts", 1) - 1),
            )
            prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        completion_metadata["requested_output_tokens"] = turn_policy.max_output_tokens
        prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.model_execution_policy.observe(turn_policy, completion_metadata)
        agent.session["model_usage_samples"] = (
            agent.model_execution_policy.usage_snapshot()
        )
        model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
        task_state.record_model_duration(model_duration_ms, completion_metadata.get("transport_retries", 0))
        agent.last_prompt_metadata = prompt_metadata
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
        """Execute and persist one auditable member of a model tool batch."""
        agent = self.agent
        if call_id:
            model_events.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(
                        args, ensure_ascii=False, sort_keys=True
                    ),
                }
            )
        agent.emit_trace(
            task_state,
            "tool_started",
            {
                "name": name,
                "args": agent.compact_tool_args(args),
                "next_step": task_state.tool_steps + 1,
            },
        )
        tool_started_at = time.monotonic()
        tool_result = (
            ToolExecutionResult(
                content=f"error: batch_call_deferred: {deferred_reason}",
                metadata={"executed": False, "tool_status": "rejected",
                          "tool_error_code": "batch_call_deferred"},
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
                    "_read_evidence": tool_result.metadata.get(
                        "read_evidence", []
                    ),
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

    def _finish_success(self, task_state, user_message, final, run_started_at):
        agent = self.agent
        if agent.progress_controller is not None:
            task_state.record_progress(agent.progress_controller.metrics())
        # Perform the session compare-and-swap before crossing the workspace
        # commit boundary. A stale concurrent session must not deliver code.
        agent.session_path = agent.session_store.save(agent.session)
        pending_read_only = bool(
            agent.transaction_context is not None
            and not (agent.current_interaction or {}).get("mutation_allowed", False)
            and agent.transaction_context.workspace.diff()
        )
        if pending_read_only:
            transaction = agent.transaction_context.workspace
            transaction.interrupt("read_only_followup")
            outcome = {
                "state": transaction.state,
                "changes": transaction.diff(),
                "conflicts": [],
            }
        else:
            outcome = agent.finalize_transaction()
        task_state.transaction_state = outcome["state"]
        paths = [change["path"] for change in outcome.get("changes", [])]
        conflicts = list(outcome.get("conflicts", []))
        if pending_read_only:
            task_state.finish_success(final)
            staged_paths, delivered_paths = paths, []
        elif outcome["state"] == "COMMITTED":
            task_state.finish_success(final)
            staged_paths = delivered_paths = paths
        elif outcome["state"] == "READY_FOR_REVIEW":
            task_state.stop("ready_for_review", final_answer=final)
            staged_paths, delivered_paths = paths, []
        else:
            validation_failed = any(
                item.get("reason")
                in {
                    "verification_failed",
                    "verification_stale",
                    "verification_required",
                    "required_test_artifact_missing",
                }
                for item in conflicts
            )
            scope_violation = any(
                item.get("reason") == "scope_constraint_violation" for item in conflicts
            )
            if validation_failed:
                reason = "validation_failed"
                final = (
                    "Run did not complete: validation failed because authoritative verification "
                    "was missing, failed, or stale; staged changes were not delivered."
                )
            elif scope_violation:
                reason = "scope_constraint_violation"
                final = "Run did not complete: staged changes violated an explicit no-modification constraint."
            else:
                reason = "workspace_conflict"
                final = "Run did not complete: the staged changes conflict with the source workspace."
            task_state.stop(reason, status="failed", final_answer=final)
            staged_paths, delivered_paths = paths, []
        agent.capture_run_outcome(
            task_state,
            staged_paths=staged_paths,
            delivered_paths=delivered_paths,
            conflicts=conflicts,
        )
        agent.record({"role": "assistant", "content": final, "created_at": now()})
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
        if outcome["state"] == "COMMITTED":
            agent.record_model_events([], execution_ledger={})
        agent.progress_controller = None
        return final

    def _close_aborted_run(self, user_message, stop_reason, error_text=""):
        agent = self.agent
        task_state = agent.current_task_state
        if task_state is None:
            return
        if task_state.status != "running":
            # A Session CAS can fail after the irreversible workspace commit
            # but before final history/checkpoint persistence. Delivery state
            # is already authoritative; close the audit record without
            # rewriting the successful task as failed or pretending the
            # Session update succeeded.
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "post_completion_persistence_failed",
                {"stop_reason": stop_reason, "error": error_text},
            )
            agent.run_store.write_report(
                task_state,
                agent.redact_artifact(agent.build_report(task_state)),
            )
            agent.progress_controller = None
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
        staged_paths = []
        delivered_paths = []
        if transaction is not None:
            try:
                transaction.workspace.interrupt(stop_reason)
                task_state.transaction_state = transaction.workspace.state
                if transaction.workspace.state == "COMMITTED":
                    delivered_paths = [
                        change["path"]
                        for change in transaction.workspace.committed_changes()
                    ]
                    staged_paths = list(delivered_paths)
                else:
                    staged_paths = [change["path"] for change in transaction.workspace.diff()]
            except Exception as exc:  # noqa: BLE001 - preserve the original failure while closing audit state
                close_errors.append(agent.redact_text(str(exc)))

        agent.capture_run_outcome(
            task_state,
            staged_paths=staged_paths,
            delivered_paths=delivered_paths,
        )
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
        # These fields describe one run, not the Session or the active Shadow
        # transaction. Reset them at the run boundary so an early failure or a
        # read-only follow-up cannot inherit evidence/report metadata from the
        # preceding request.
        agent.last_prompt_metadata = {}
        agent.last_completion_metadata = {}
        agent.last_durable_promotions = []
        agent.last_durable_rejections = []
        agent.last_durable_superseded = []
        agent.last_run_outcome = None
        agent.last_repository_evidence = {}
        agent._last_tool_result_metadata = {}
        task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        interaction = agent.interaction_contract(user_message)
        agent.current_interaction = interaction
        task_state.set_interaction(interaction)
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_run_lease = agent.run_store.acquire_run_lease(task_state, blocking=True)
        if agent.current_run_lease is None:
            raise RuntimeError("could not acquire run owner lease")
        agent.current_run_dir = agent.run_store.start_run(task_state)

        if agent.transaction_context is None:
            agent.begin_transaction()
        elif interaction.get("mutation_allowed"):
            agent.transaction_context.workspace.resume_editing()
        if interaction.get("mutation_allowed") and not agent.read_only:
            evidence = agent.repository_evidence(user_message)
            if evidence:
                interaction["repository_evidence"] = evidence
        if agent.transaction_context is not None:
            task_state.transaction_id = agent.transaction_context.transaction_id
            task_state.transaction_state = agent.transaction_context.workspace.state
            agent.run_store.write_task_state(task_state)
        agent.memory.set_task_summary(user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})
        controller = ProgressController(
            max_steps=agent.max_steps,
            read_only=agent.read_only or not interaction.get("mutation_allowed", False),
            soft_discovery_limit=agent.soft_discovery_limit,
            hard_discovery_limit=agent.hard_discovery_limit,
            ledger=agent.session.get("execution_ledger", {}),
            repository_evidence=agent.last_repository_evidence,
            delivery_requirements=interaction,
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
                "interaction": interaction,
            },
        )
        promoted, rejected, superseded = agent.promote_durable_memory(user_message)
        if promoted or rejected or superseded:
            agent.emit_trace(
                task_state,
                "durable_memory_admitted",
                {
                    "promoted": promoted,
                    "rejected": rejected,
                    "superseded": superseded,
                },
            )

        attempts = 0
        stuck_final = None
        contract_failure_final = None
        contract_failures = 0
        contract_failure_reason = ""
        completion_failure_reason = ""
        model_notice = ""
        request_profile = interaction["request_profile"]
        prompt_metadata_base = {
            "request_profile": request_profile,
            "request_mode": interaction["mode"],
            "package_layout": interaction["package_layout"],
        }
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while task_state.tool_steps < agent.max_steps and attempts < max_attempts:
            controller.set_remaining_steps(agent.max_steps - task_state.tool_steps)
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            progress_notice = controller.consume_notice()
            combined_notice = "\n".join(
                notice for notice in (progress_notice, model_notice) if notice
            )
            model_notice = ""
            turn_policy = agent.model_execution_policy.for_turn(
                purpose="action",
                max_output_tokens=agent.max_new_tokens,
                read_only=controller.read_only,
                recovering=contract_failures > 0,
                request_profile=request_profile,
                recovery_reason=contract_failure_reason,
                completion_metadata=agent.last_completion_metadata,
                recovery_attempt=contract_failures,
                capabilities=getattr(agent.model_client, "capabilities", None),
            )
            if native_mode:
                agent.refresh_prefix()
                input_items, prompt_metadata = projector.build(
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
            prompt_metadata.update(prompt_metadata_base)
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
            if native_mode:
                native_turn = self._request_native_model(
                    task_state,
                    user_message,
                    input_items,
                    prompt_metadata,
                    run_started_at,
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
                    kind = native_turn.kind
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
                    turn_policy=turn_policy,
                )

            if kind in {"tool", "tool_batch"}:
                if contract_failures:
                    task_state.record_model_recovery()
                    agent.emit_trace(
                        task_state,
                        "model_recovered",
                        {
                            "previous_failure": contract_failure_reason,
                            "recovery_attempts": contract_failures,
                            "kind": kind,
                        },
                    )
                contract_failures = 0
                contract_failure_reason = ""
                if native_turn is not None and raw.strip():
                    # Preserve the assistant's public plan/preamble alongside
                    # its actions. Never replay opaque provider reasoning items.
                    model_events.append({"role": "assistant", "content": raw})
                if native_turn is not None and native_turn.tool_calls:
                    calls = [
                        (
                            call.name,
                            agent.redact_artifact(call.args),
                            call.call_id or f"call_{attempts}_{index}",
                        )
                        for index, call in enumerate(native_turn.tool_calls, 1)
                    ]
                else:
                    calls = [
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
                deferred_reason = ""
                for name, args, call_id in calls:
                    if task_state.tool_steps >= agent.max_steps:
                        deferred_reason = "the configured tool budget was exhausted"
                    controller.set_remaining_steps(
                        agent.max_steps - task_state.tool_steps
                    )
                    metadata = self._execute_tool_call(
                        task_state,
                        user_message,
                        controller,
                        model_events,
                        name,
                        args,
                        call_id=call_id,
                        deferred_reason=deferred_reason,
                    )
                    if metadata.get("tool_status") != "ok":
                        deferred_reason = (
                            "an earlier call failed; inspect its result before requesting further actions"
                        )
                if controller.state.stuck_detected:
                    stuck_final = (
                        "Stopped because the agent continued without observable progress after "
                        "the forced-decision intervention."
                    )
                    agent.emit_trace(
                        task_state,
                        "agent_stuck",
                        {"name": calls[-1][0], **controller.metrics()},
                    )
                    break
                continue

            if kind == "retry":
                contract_failures += 1
                contract_failure_reason = str(payload)
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
                failure_kind = (
                    "incomplete"
                    if (
                        native_turn is not None
                        and native_turn.kind == "incomplete"
                        or prompt_metadata.get("contract_failure_kind")
                        == "incomplete"
                    )
                    else "invalid"
                )
                if failure_kind == "incomplete":
                    next_policy = agent.model_execution_policy.for_turn(
                        purpose="action",
                        max_output_tokens=agent.max_new_tokens,
                        read_only=controller.read_only,
                        recovering=True,
                        request_profile=request_profile,
                        recovery_reason=contract_failure_reason,
                        completion_metadata=agent.last_completion_metadata,
                        recovery_attempt=contract_failures,
                        capabilities=getattr(
                            agent.model_client, "capabilities", None
                        ),
                    )
                    retry_permitted = next_policy.recovery_permitted
                else:
                    # Malformed protocol does not improve with additional
                    # output space. One regeneration is enough to distinguish
                    # a transient formatting error from a repeating failure.
                    retry_permitted = contract_failures == 1
                if not retry_permitted:
                    contract_failure_final = (
                        "Stopped because the model failed to produce a complete typed response "
                        f"after bounded recovery: {payload}"
                    )
                    break
                model_notice = (
                    "Runtime notice: the previous model response was not complete and was rejected "
                    f"({payload}). Return complete function calls with valid arguments, or a final answer."
                )
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                continue

            contract_failures = 0
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
            contract_failure_reason = ""
            final = (payload or raw).strip()
            if interaction.get("mutation_allowed") and not agent.read_only:
                completion_failure_reason = agent.verification_failure_reason()
                if completion_failure_reason:
                    # A final message is a proposal, not a workspace commit.
                    # Give the model the same authoritative failure the commit
                    # boundary would return, while there is still room to act.
                    model_notice = (
                        "Runtime verification feedback: completion was not accepted "
                        f"({completion_failure_reason}). Staged changes are preserved. "
                        "Inspect the test results, repair failures if needed, and run "
                        "the required verification on the current code before finishing."
                    )
                    agent.emit_trace(
                        task_state,
                        "completion_deferred",
                        {"reason": completion_failure_reason},
                    )
                    agent.run_store.write_task_state(task_state)
                    continue
            return self._finish_success(task_state, user_message, final, run_started_at)

        if task_state.tool_steps >= agent.max_steps:
            finalization_failures = 0
            finalization_reason = ""
            while attempts < max_attempts:
                attempts += 1
                task_state.record_attempt()
                agent.run_store.write_task_state(task_state)
                prompt_started_at = time.monotonic()
                recovery_notice = ""
                if finalization_failures:
                    recovery_notice = (
                        "Runtime notice: finalization was incomplete and rejected "
                        f"({finalization_reason}). Regenerate one complete final answer "
                        "from existing evidence. Do not call or describe another tool."
                    )
                turn_policy = agent.model_execution_policy.for_turn(
                    purpose="finalization",
                    max_output_tokens=agent.max_new_tokens,
                    read_only=controller.read_only,
                    recovering=finalization_failures > 0,
                    request_profile=request_profile,
                    recovery_reason=finalization_reason,
                    completion_metadata=agent.last_completion_metadata,
                    recovery_attempt=finalization_failures,
                    capabilities=getattr(agent.model_client, "capabilities", None),
                )
                if native_mode:
                    input_items, prompt_metadata = projector.build(
                        user_message,
                        model_events,
                        controller,
                        notice=recovery_notice,
                        finalization=True,
                        input_token_budget=turn_policy.max_input_tokens,
                        chars_per_token=agent.model_execution_policy.chars_per_input_token(),
                    )
                    prompt = ""
                else:
                    prompt, prompt_metadata = agent._build_prompt_and_metadata(
                        user_message
                    )
                    prompt += (
                        "\n\nRuntime notice: finalization mode is active. Do not call "
                        "another tool. Use existing evidence and return exactly one "
                        "non-empty <final>...</final> answer."
                    )
                    if recovery_notice:
                        prompt += "\n" + recovery_notice
                prompt_metadata["finalization"] = True
                agent.emit_trace(
                    task_state,
                    "prompt_built",
                    {
                        "prompt_metadata": prompt_metadata,
                        "duration_ms": int(
                            (time.monotonic() - prompt_started_at) * 1000
                        ),
                        "purpose": "finalization",
                    },
                )
                native_turn = None
                if native_mode:
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
                    payload = (
                        decision.text
                        if decision.accepted
                        else (
                            native_turn.incomplete_reason
                            or native_turn.protocol_error
                            or decision.reason
                        )
                    )
                else:
                    raw, kind, payload = self._request_model(
                        task_state,
                        user_message,
                        prompt,
                        prompt_metadata,
                        run_started_at,
                        purpose="finalization",
                        turn_policy=turn_policy,
                    )
                if kind == "final":
                    if finalization_failures:
                        task_state.record_model_recovery()
                        agent.emit_trace(
                            task_state,
                            "model_recovered",
                            {
                                "previous_failure": finalization_reason,
                                "recovery_attempts": finalization_failures,
                                "kind": "final",
                                "purpose": "finalization",
                            },
                        )
                    final = (payload or raw).strip()
                    return self._finish_success(
                        task_state, user_message, final, run_started_at
                    )

                finalization_failures += 1
                finalization_reason = str(payload)
                failure_kind = (
                    "incomplete"
                    if (
                        native_turn is not None
                        and native_turn.kind == "incomplete"
                        or prompt_metadata.get("contract_failure_kind")
                        == "incomplete"
                    )
                    else "invalid"
                )
                task_state.record_model_contract_failure(failure_kind)
                agent.emit_trace(
                    task_state,
                    "model_contract_rejected",
                    {
                        "reason": finalization_reason,
                        "consecutive_failures": finalization_failures,
                        "purpose": "finalization",
                        "response_status": getattr(
                            native_turn, "response_status", ""
                        ),
                    },
                )
                if failure_kind == "incomplete":
                    next_policy = agent.model_execution_policy.for_turn(
                        purpose="finalization",
                        max_output_tokens=agent.max_new_tokens,
                        read_only=controller.read_only,
                        recovering=True,
                        request_profile=request_profile,
                        recovery_reason=finalization_reason,
                        completion_metadata=agent.last_completion_metadata,
                        recovery_attempt=finalization_failures,
                        capabilities=getattr(
                            agent.model_client, "capabilities", None
                        ),
                    )
                    retry_permitted = next_policy.recovery_permitted
                else:
                    retry_permitted = finalization_failures == 1
                if not retry_permitted:
                    contract_failure_final = (
                        "Stopped because finalization did not produce a complete "
                        f"typed answer after bounded recovery: {payload}"
                    )
                    break

            if finalization_failures and contract_failure_final is None:
                contract_failure_final = (
                    "Stopped because finalization exhausted the remaining request "
                    f"budget: {finalization_reason}"
                )

        if stuck_final is not None:
            final = stuck_final
            task_state.stop_stuck(final)
        elif contract_failure_final is not None:
            final = contract_failure_final
            task_state.stop_retry_limit(final)
        elif completion_failure_reason:
            final = (
                "Run did not complete: authoritative verification remained "
                f"unsatisfied ({completion_failure_reason}); staged changes were not delivered."
            )
            task_state.stop("validation_failed", status="failed", final_answer=final)
        elif attempts >= max_attempts and task_state.tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        task_state.record_progress(controller.metrics())
        agent.interrupt_transaction(task_state.stop_reason or "interrupted")
        if agent.transaction_context is not None:
            task_state.transaction_state = agent.transaction_context.workspace.state
            staged_paths = [
                change["path"] for change in agent.transaction_context.workspace.diff()
            ]
        else:
            staged_paths = []
        agent.capture_run_outcome(task_state, staged_paths=staged_paths)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
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
