"""一次 ask() 运行过程中的状态机快照。

它回答的是：这次用户请求当前进行到哪了、调了多少次工具、最后为什么停下。
这个对象会被不断写入 task_state.json，供运行中观察和运行后复盘。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"

STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"
STOP_REASON_STEP_LIMIT_REACHED = "step_limit_reached"
STOP_REASON_RETRY_LIMIT_REACHED = "retry_limit_reached"
STOP_REASON_MODEL_ERROR = "model_error"
STOP_REASON_TOOL_TIMEOUT = "tool_timeout"
STOP_REASON_APPROVAL_DENIED = "approval_denied"
STOP_REASON_DELEGATE_FAILED = "delegate_failed"
STOP_REASON_PERSISTENCE_ERROR = "persistence_error"
STOP_REASON_RESUME_LOAD_ERROR = "resume_load_error"
STOP_REASON_STUCK_NO_PROGRESS = "stuck_no_progress"
STOP_REASON_INTERRUPTED = "interrupted"
STOP_REASON_RUNTIME_ERROR = "runtime_error"
STOP_REASON_ORPHANED = "orphaned"
STOP_REASON_SESSION_CONFLICT = "session_revision_conflict"


@dataclass
class TaskState:
    run_id: str
    task_id: str
    user_request: str
    status: str = STATUS_RUNNING
    tool_steps: int = 0
    attempts: int = 0
    last_tool: str = ""
    stop_reason: str = ""
    final_answer: str = ""
    checkpoint_id: str = ""
    resume_status: str = ""
    transaction_id: str = ""
    transaction_state: str = ""
    blocked_repeats: int = 0
    intervention_count: int = 0
    steps_to_first_mutation: int | None = None
    steps_to_first_shell: int | None = None
    max_discovery_streak: int = 0
    stuck_detected: bool = False
    model_incomplete_count: int = 0
    model_protocol_error_count: int = 0
    model_transport_failure_count: int = 0

    @classmethod
    def create(cls, task_id, user_request, run_id=""):
        if not run_id:
            run_id = "run_" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        return cls(run_id=run_id, task_id=task_id, user_request=user_request)

    @classmethod
    def from_dict(cls, data):
        return cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            user_request=str(data.get("user_request", "")),
            status=str(data.get("status", STATUS_RUNNING)),
            tool_steps=int(data.get("tool_steps", 0)),
            attempts=int(data.get("attempts", 0)),
            last_tool=str(data.get("last_tool", "")),
            stop_reason=str(data.get("stop_reason", "")),
            final_answer=str(data.get("final_answer", "")),
            checkpoint_id=str(data.get("checkpoint_id", "")),
            resume_status=str(data.get("resume_status", "")),
            transaction_id=str(data.get("transaction_id", "")),
            transaction_state=str(data.get("transaction_state", "")),
            blocked_repeats=int(data.get("blocked_repeats", 0)),
            intervention_count=int(data.get("intervention_count", 0)),
            steps_to_first_mutation=data.get("steps_to_first_mutation"),
            steps_to_first_shell=data.get("steps_to_first_shell"),
            max_discovery_streak=int(data.get("max_discovery_streak", 0)),
            stuck_detected=bool(data.get("stuck_detected", False)),
            model_incomplete_count=int(data.get("model_incomplete_count", 0)),
            model_protocol_error_count=int(data.get("model_protocol_error_count", 0)),
            model_transport_failure_count=int(data.get("model_transport_failure_count", 0)),
        )

    def record_attempt(self):
        # attempt 统计的是“模型被调用了几轮”，不等于 tool_steps。
        self.attempts += 1
        return self

    def record_tool(self, name, workspace_changed=False):
        # tool_steps 只统计真正进入执行阶段的工具调用次数。
        self.tool_steps += 1
        self.last_tool = str(name or "")
        if self.last_tool == "run_shell" and self.steps_to_first_shell is None:
            self.steps_to_first_shell = self.tool_steps
        if workspace_changed and self.steps_to_first_mutation is None:
            self.steps_to_first_mutation = self.tool_steps
        return self

    def record_progress(self, metrics):
        self.blocked_repeats = int(metrics.get("blocked_repeats", self.blocked_repeats))
        self.intervention_count = int(metrics.get("intervention_count", self.intervention_count))
        self.max_discovery_streak = int(metrics.get("max_discovery_streak", self.max_discovery_streak))
        self.stuck_detected = bool(metrics.get("stuck_detected", self.stuck_detected))
        return self

    def record_model_contract_failure(self, kind):
        if str(kind) == "incomplete":
            self.model_incomplete_count += 1
        else:
            self.model_protocol_error_count += 1
        return self

    def record_model_transport_failure(self):
        self.model_transport_failure_count += 1
        return self

    def stop(self, stop_reason, status=STATUS_STOPPED, final_answer=""):
        # stop_reason 和 status 分开存，是为了区分“怎么停的”和“停下时是什么状态”。
        self.status = status
        self.stop_reason = stop_reason
        if final_answer != "":
            self.final_answer = final_answer
        return self

    def stop_step_limit(self, final_answer=""):
        return self.stop(STOP_REASON_STEP_LIMIT_REACHED, final_answer=final_answer)

    def stop_retry_limit(self, final_answer=""):
        return self.stop(STOP_REASON_RETRY_LIMIT_REACHED, final_answer=final_answer)

    def stop_model_error(self, final_answer=""):
        return self.stop(STOP_REASON_MODEL_ERROR, status=STATUS_FAILED, final_answer=final_answer)

    def stop_stuck(self, final_answer=""):
        self.stuck_detected = True
        return self.stop(STOP_REASON_STUCK_NO_PROGRESS, final_answer=final_answer)

    def stop_interrupted(self, final_answer=""):
        return self.stop(STOP_REASON_INTERRUPTED, final_answer=final_answer)

    def stop_runtime_error(self, final_answer=""):
        return self.stop(STOP_REASON_RUNTIME_ERROR, status=STATUS_FAILED, final_answer=final_answer)

    def stop_orphaned(self, final_answer=""):
        return self.stop(STOP_REASON_ORPHANED, final_answer=final_answer)

    def stop_session_conflict(self, final_answer=""):
        return self.stop(STOP_REASON_SESSION_CONFLICT, status=STATUS_FAILED, final_answer=final_answer)

    def finish_success(self, final_answer):
        self.status = STATUS_COMPLETED
        self.stop_reason = STOP_REASON_FINAL_ANSWER_RETURNED
        self.final_answer = str(final_answer)
        return self

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "user_request": self.user_request,
            "status": self.status,
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "checkpoint_id": self.checkpoint_id,
            "resume_status": self.resume_status,
            "transaction_id": self.transaction_id,
            "transaction_state": self.transaction_state,
            "blocked_repeats": self.blocked_repeats,
            "intervention_count": self.intervention_count,
            "steps_to_first_mutation": self.steps_to_first_mutation,
            "steps_to_first_shell": self.steps_to_first_shell,
            "max_discovery_streak": self.max_discovery_streak,
            "stuck_detected": self.stuck_detected,
            "model_incomplete_count": self.model_incomplete_count,
            "model_protocol_error_count": self.model_protocol_error_count,
            "model_transport_failure_count": self.model_transport_failure_count,
        }
