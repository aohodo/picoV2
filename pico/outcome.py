"""Authoritative result of one Pico run.

Model prose is advisory. This object is derived from runtime, transaction,
and verification state so callers can tell whether code was delivered.
"""

from dataclasses import dataclass, field

EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_STOPPED = 3


@dataclass(frozen=True)
class RunOutcome:
    status: str
    stop_reason: str
    transaction_state: str
    validation_status: str
    final_answer: str
    staged_paths: tuple[str, ...] = field(default_factory=tuple)
    delivered_paths: tuple[str, ...] = field(default_factory=tuple)
    conflicts: tuple[dict, ...] = field(default_factory=tuple)
    exit_code: int = EXIT_STOPPED

    @property
    def successful(self):
        return self.status == "committed" and self.exit_code == EXIT_SUCCESS

    def to_dict(self):
        return {
            "status": self.status,
            "stop_reason": self.stop_reason,
            "transaction_state": self.transaction_state,
            "validation_status": self.validation_status,
            "final_answer": self.final_answer,
            "staged_paths": list(self.staged_paths),
            "delivered_paths": list(self.delivered_paths),
            "conflicts": [dict(item) for item in self.conflicts],
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_task_state(cls, task_state, staged_paths=(), delivered_paths=(), conflicts=()):
        transaction_state = str(task_state.transaction_state or "")
        stop_reason = str(task_state.stop_reason or "")
        if task_state.status == "completed" and transaction_state == "COMMITTED":
            status, exit_code = "committed", EXIT_SUCCESS
        elif transaction_state == "COMMITTED":
            # The commit journal can prove that source delivery completed even
            # when the process died before TaskState/final-answer persistence.
            # Do not mislabel that crash window as either a clean completion or
            # an ordinary stopped run.
            status, exit_code = "committed_unconfirmed", EXIT_STOPPED
        elif transaction_state == "READY_FOR_REVIEW" or stop_reason == "ready_for_review":
            status, exit_code = "ready_for_review", EXIT_STOPPED
        elif stop_reason == "validation_failed":
            status, exit_code = "validation_failed", EXIT_FAILED
        elif stop_reason == "workspace_conflict":
            status, exit_code = "workspace_conflict", EXIT_FAILED
        elif task_state.status == "failed":
            status, exit_code = "failed", EXIT_FAILED
        else:
            status, exit_code = "stopped", EXIT_STOPPED
        return cls(
            status=status,
            stop_reason=stop_reason,
            transaction_state=transaction_state,
            validation_status=str(task_state.validation_status or "not_run"),
            final_answer=str(task_state.final_answer or ""),
            staged_paths=tuple(str(path) for path in staged_paths),
            delivered_paths=tuple(str(path) for path in delivered_paths),
            conflicts=tuple(dict(item) for item in conflicts),
            exit_code=exit_code,
        )
