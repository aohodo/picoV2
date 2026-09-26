"""Run termination and transactional delivery lifecycle."""

import time

from ..persistence import SessionConflictError
from ..workspace import now


class DeliveryRuntime:
    """Own successful delivery and controlled run shutdown."""

    def _finish_success(self, task_state, user_message, final, run_started_at):
        agent = self.agent
        if agent.progress_controller is not None:
            task_state.record_progress(agent.progress_controller.metrics())
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
                    "Run did not complete: validation failed because authoritative "
                    "verification was missing, failed, or stale; staged changes "
                    "were not delivered."
                )
            elif scope_violation:
                reason = "scope_constraint_violation"
                final = (
                    "Run did not complete: staged changes violated an explicit "
                    "no-modification constraint."
                )
            else:
                reason = "workspace_conflict"
                final = (
                    "Run did not complete: the staged changes conflict with the "
                    "source workspace."
                )
            task_state.stop(reason, status="failed", final_answer=final)
            staged_paths, delivered_paths = paths, []
        agent.capture_run_outcome(
            task_state,
            staged_paths=staged_paths,
            delivered_paths=delivered_paths,
            conflicts=conflicts,
        )
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        checkpoint = agent.create_checkpoint(
            task_state, user_message, trigger="run_finished"
        )
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
        agent.run_store.write_report(
            task_state, agent.redact_artifact(agent.build_report(task_state))
        )
        if outcome["state"] == "COMMITTED":
            agent.record_model_events([], execution_ledger={})
        agent.progress_controller = None
        return final

    def _finish_verified_without_model_final(
        self, task_state, user_message, run_started_at, failure_reason
    ):
        agent = self.agent
        transaction = agent.transaction_context
        interaction = agent.current_interaction or {}
        if (
            transaction is None
            or not interaction.get("mutation_allowed", False)
            or agent.read_only
            or not transaction.workspace.diff()
            or agent.verification_failure_reason()
        ):
            return None
        if agent.progress_controller is not None:
            agent.progress_controller.complete_delivery_review()
        agent.emit_trace(
            task_state,
            "runtime_finalization_fallback",
            {"reason": str(failure_reason)},
        )
        final = (
            "Implementation was verified and delivered. The model did not produce "
            "a usable final summary; inspect the run report for the validated "
            "change set."
        )
        return self._finish_success(task_state, user_message, final, run_started_at)

    def _close_aborted_run(self, user_message, stop_reason, error_text=""):
        agent = self.agent
        task_state = agent.current_task_state
        if task_state is None:
            return
        if task_state.status != "running":
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
            "interrupted": (
                "Run interrupted; the staged workspace was preserved for resume."
            ),
            "session_revision_conflict": (
                "Run stopped because another process updated this session."
            ),
        }.get(
            stop_reason,
            "Run failed because the runtime raised an unexpected error.",
        )
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
                    staged_paths = [
                        change["path"] for change in transaction.workspace.diff()
                    ]
            except Exception as exc:  # noqa: BLE001
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
                checkpoint = agent.create_checkpoint(
                    task_state, user_message, trigger=stop_reason
                )
                agent.run_store.write_task_state(task_state)
            except Exception as exc:  # noqa: BLE001
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
                "checkpoint_id": (checkpoint["checkpoint_id"] if checkpoint else ""),
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

    def _graph_deliver(self, state):
        final = self._finish_success(
            state["task_state"],
            state["user_message"],
            state["final_answer"],
            state["run_started_at"],
        )
        return {"final_answer": final}

    def _graph_stop(self, state):
        agent = self.agent
        task_state = state["task_state"]
        controller = state["controller"]
        contract_failure_final = state.get("contract_failure_final")
        if contract_failure_final is not None:
            delivered = self._finish_verified_without_model_final(
                task_state,
                state["user_message"],
                state["run_started_at"],
                contract_failure_final,
            )
            if delivered is not None:
                return {"final_answer": delivered}

        final = self._stop_task_state(state, task_state)
        task_state.record_progress(controller.metrics())
        agent.interrupt_transaction(task_state.stop_reason or "interrupted")
        staged_paths = self._staged_transaction_paths(task_state)
        agent.capture_run_outcome(task_state, staged_paths=staged_paths)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(
            task_state,
            state["user_message"],
            trigger=task_state.stop_reason or "run_stopped",
        )
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
                "run_duration_ms": int(
                    (time.monotonic() - state["run_started_at"]) * 1000
                ),
            },
        )
        agent.run_store.write_report(
            task_state, agent.redact_artifact(agent.build_report(task_state))
        )
        agent.progress_controller = None
        return {"final_answer": final}

    def _stop_task_state(self, state, task_state):
        agent = self.agent
        contract_failure_final = state.get("contract_failure_final")
        if state.get("stuck_final") is not None:
            final = state["stuck_final"]
            task_state.stop_stuck(final)
        elif contract_failure_final is not None:
            final = contract_failure_final
            task_state.stop_retry_limit(final)
        elif state.get("completion_failure_reason"):
            final = (
                "Run did not complete: authoritative verification remained "
                "unsatisfied "
                f"({state['completion_failure_reason']}); staged changes were not "
                "delivered."
            )
            task_state.stop("validation_failed", status="failed", final_answer=final)
        elif (
            state["attempts"] >= state["max_attempts"]
            and task_state.tool_steps < agent.max_steps
        ):
            final = (
                "Stopped after too many malformed model responses without a valid "
                "tool call or final answer."
            )
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        return final

    def _staged_transaction_paths(self, task_state):
        transaction = self.agent.transaction_context
        if transaction is None:
            return []
        task_state.transaction_state = transaction.workspace.state
        return [change["path"] for change in transaction.workspace.diff()]
