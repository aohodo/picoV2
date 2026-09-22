"""Agent 运行时核心逻辑。

Pico 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from . import checkpoint as checkpointlib
from . import security as securitylib
from . import tools as toolkit
from .checkpoint import CHECKPOINT_NONE_STATUS
from .context_manager import ContextManager
from .execution import ExecutionLease, WorkspaceCommandRunner
from .execution_policy import ModelExecutionPolicy
from .features import memory as memorylib
from .interaction_policy import (
    PACKAGE_LAYOUTS,
    PreferenceError,
    WorkspacePreferenceStore,
    build_interaction_contract,
)
from .memory_admission import extract_explicit_memory
from .path_support import logical_path, native_path
from .prompt_prefix import build_prompt_prefix, tool_signature
from .run_store import RunStore
from .security import REDACTED_VALUE, SecretBoundary
from .session_store import SessionStore
from .state_root import WorkspaceState
from .task_state import TaskState
from .tool_context import ToolContext
from .tool_executor import ToolExecutor
from .transaction_context import TransactionContext
from .transactional_workspace import TransactionalWorkspace
from .workspace import IGNORED_PATH_NAMES, MAX_HISTORY, WorkspaceContext, clip, now

DEFAULT_SHELL_ENV_ALLOWLIST = (
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "SYSTEMROOT",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
)
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
}
SECRET_SHAPED_TEXT_PATTERN = re.compile(
    r"(?i)(\b(api[_ -]?key|token|secret|password)\b|sk-[A-Za-z0-9_-]{6,}|<redacted>)"
)

__all__ = ["Pico", "SessionStore"]


class Pico:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        depth=0,
        max_depth=1,
        read_only=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        allowed_tools=None,
        commit_policy=None,
        state_root=None,
        transaction_context=None,
        sandbox_image="pico-sandbox:1",
        soft_discovery_limit=None,
        hard_discovery_limit=None,
        model_execution_policy="adaptive",
        progress_sink=None,
        package_layout=None,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.source_root = Path(workspace.repo_root).resolve()
        self.root = self.source_root
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.soft_discovery_limit = soft_discovery_limit
        self.hard_discovery_limit = hard_discovery_limit
        self.model_execution_policy = ModelExecutionPolicy(model_execution_policy)
        self.progress_sink = progress_sink
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.secret_boundary = (
            transaction_context.secret_boundary
            if transaction_context is not None
            else SecretBoundary(secret_env_names=self.secret_env_names)
        )
        for attribute in ("api_key", "token", "auth_token"):
            self.secret_boundary.register_secret(getattr(model_client, attribute, ""))
        self.session_store.secret_boundary = self.secret_boundary
        self.commit_policy = str(commit_policy or ("auto" if approval_policy == "auto" else "review"))
        if self.commit_policy not in {"review", "auto"}:
            raise ValueError("commit_policy must be 'review' or 'auto'")
        self.sandbox_image = str(sandbox_image)
        self.workspace_state = None
        if state_root is not None:
            self.workspace_state = WorkspaceState(self.source_root, root=state_root).ensure()
            self.transactions_root = self.workspace_state.transactions
        else:
            self.transactions_root = Path(self.session_store.root).parent / "transactions"
            self.transactions_root.mkdir(parents=True, exist_ok=True)
        self.transaction_context = transaction_context
        if transaction_context is not None:
            self.root = Path(transaction_context.execution_root)
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.allowed_tools = self._normalize_allowed_tools(allowed_tools)
        fallback_state_root = Path(self.session_store.root).parent
        preference_root = self.workspace_state.root if self.workspace_state else fallback_state_root
        self.preference_store = WorkspacePreferenceStore(preference_root / "preferences.json")
        self.package_layout_override = str(package_layout).strip() if package_layout else ""
        if self.package_layout_override and self.package_layout_override not in PACKAGE_LAYOUTS:
            choices = ", ".join(sorted(PACKAGE_LAYOUTS))
            raise PreferenceError(f"package_layout must be one of: {choices}")
        self.current_interaction = {}
        self.run_store = run_store or RunStore(
            self.workspace_state.runs if self.workspace_state else fallback_state_root / "runs"
        )
        self.run_store.secret_boundary = self.secret_boundary
        self.session = session or {
            "id": datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self._ensure_session_shape()
        active_transaction_id = str(self.session.get("active_transaction_id", "")).strip()
        if self.transaction_context is None and active_transaction_id:
            transaction = TransactionalWorkspace.load(
                self.source_root,
                self.transactions_root,
                active_transaction_id,
                secret_boundary=self.secret_boundary,
            )
            if not transaction.execution_root.exists():
                raise RuntimeError("RECOVERY_UNAVAILABLE: transaction shadow workspace is missing")
            runner = WorkspaceCommandRunner(
                transaction.execution_root,
                self.secret_boundary,
                env_allowlist=self.shell_env_allowlist,
            )
            self.transaction_context = TransactionContext(
                transaction_id=transaction.transaction_id,
                workspace=transaction,
                secret_boundary=self.secret_boundary,
                execution_lease=ExecutionLease(runner),
                owns_context=True,
            )
            self.root = transaction.execution_root
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.root,
            durable_root=(self.workspace_state.memory if self.workspace_state else fallback_state_root / "memory"),
        )
        self.session["memory"] = self.memory.to_dict()
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self.tool_executor = ToolExecutor(self)
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)
        self.resume_state = self.evaluate_resume_state()
        self.current_task_state = None
        self.current_run_dir = None
        self.current_run_lease = None
        self.current_run_started_at = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.last_durable_promotions = []
        self.last_durable_rejections = []
        self.last_durable_superseded = []
        self._last_tool_result_metadata = {}
        self.last_shell_validation_succeeded = None
        self.progress_controller = None
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }
        self._recover_orphaned_runs()
        self.session_path = self.session_store.save(self.session)

    def effective_package_layout(self):
        if self.package_layout_override:
            return self.package_layout_override
        return self.preference_store.load()["package_layout"]

    def preferences_view(self):
        stored = self.preference_store.load()
        return {
            "package_layout": self.effective_package_layout(),
            "workspace_package_layout": stored["package_layout"],
            "source": "command_line" if self.package_layout_override else "workspace",
        }

    def set_workspace_package_layout(self, value):
        values = self.preference_store.set_package_layout(value)
        self.package_layout_override = ""
        return values

    def reset_workspace_package_layout(self):
        values = self.preference_store.reset_package_layout()
        self.package_layout_override = ""
        return values

    def interaction_contract(self, user_message):
        return build_interaction_contract(user_message, self.effective_package_layout())

    def _recover_orphaned_runs(self):
        for payload in self.run_store.claim_orphaned_runs():
            task_state = TaskState.from_dict(payload)
            transaction_error = ""
            transaction_id = str(task_state.transaction_id or "").strip()
            if transaction_id:
                try:
                    if (
                        self.transaction_context is not None
                        and self.transaction_context.transaction_id == transaction_id
                    ):
                        transaction = self.transaction_context.workspace
                    else:
                        transaction = TransactionalWorkspace.load(
                            self.source_root,
                            self.transactions_root,
                            transaction_id,
                            secret_boundary=self.secret_boundary,
                        )
                    transaction.interrupt("orphaned_run")
                    task_state.transaction_state = transaction.state
                except Exception as exc:  # noqa: BLE001 - orphan recovery must still close the run record
                    transaction_error = self.redact_text(str(exc))
            final = "Recovered a run left active without a live process owner."
            task_state.stop_orphaned(final)
            self.run_store.write_task_state(task_state)
            self.emit_trace(
                task_state,
                "orphan_recovered",
                {
                    "previous_status": "running",
                    "transaction_error": transaction_error,
                },
            )
            self.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                },
            )
            self.run_store.write_report(
                task_state,
                self.redact_artifact(self.build_report(task_state)),
            )

    def begin_transaction(self):
        if self.transaction_context is not None:
            return self.transaction_context
        transaction = TransactionalWorkspace(
            self.source_root,
            self.transactions_root,
            secret_boundary=self.secret_boundary,
        ).begin()
        runner = WorkspaceCommandRunner(
            transaction.execution_root,
            self.secret_boundary,
            env_allowlist=self.shell_env_allowlist,
        )
        self.transaction_context = TransactionContext(
            transaction_id=transaction.transaction_id,
            workspace=transaction,
            secret_boundary=self.secret_boundary,
            execution_lease=ExecutionLease(runner),
            owns_context=True,
        )
        self.root = transaction.execution_root
        self.workspace = WorkspaceContext.build(self.root, repo_root_override=self.root)
        self.session["active_transaction_id"] = transaction.transaction_id
        self.last_shell_validation_succeeded = None
        self.memory.workspace_root = self.root
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self._apply_prefix_state(self.build_prefix())
        self.session_path = self.session_store.save(self.session)
        return self.transaction_context

    def _restore_source_view(self, clear_context=True):
        context = self.transaction_context
        self.root = self.source_root
        self.workspace = WorkspaceContext.build(self.source_root, repo_root_override=self.source_root)
        self.memory.workspace_root = self.source_root
        if clear_context:
            if context and context.owns_context:
                context.execution_lease.stop()
            self.transaction_context = None
            self.session.pop("active_transaction_id", None)
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self._apply_prefix_state(self.build_prefix())
        self.session_path = self.session_store.save(self.session)

    def finalize_transaction(self):
        context = self.transaction_context
        if context is None or not context.owns_context:
            return {"state": "COMMITTED", "changes": [], "conflicts": []}
        transaction = context.workspace
        changes = transaction.stage()
        if self.last_shell_validation_succeeded is False:
            conflicts = transaction.block_validation("last_shell_command_failed")
            return {"state": transaction.state, "changes": changes, "conflicts": conflicts}
        conflicts = transaction.validate_commit()
        if conflicts:
            return {"state": transaction.state, "changes": changes, "conflicts": conflicts}
        if self.commit_policy == "auto" or not changes:
            context.execution_lease.stop()
            transaction.commit()
            result = {"state": transaction.state, "changes": changes, "conflicts": []}
            self._restore_source_view(clear_context=True)
            return result
        return {"state": transaction.state, "changes": changes, "conflicts": []}

    def apply_transaction(self):
        if self.transaction_context is None:
            raise RuntimeError("no active transaction")
        context = self.transaction_context
        transaction = context.workspace
        context.execution_lease.stop()
        committed_changes = transaction.commit()
        result = {"state": transaction.state, "changes": committed_changes, "conflicts": []}
        self._restore_source_view(clear_context=True)
        if self.current_task_state is not None:
            self.current_task_state.transaction_state = transaction.state
            self.current_task_state.finish_success(self.current_task_state.final_answer)
            self.run_store.write_task_state(self.current_task_state)
            self.run_store.write_report(
                self.current_task_state,
                self.redact_artifact(self.build_report(self.current_task_state)),
            )
        return result

    def discard_transaction(self):
        if self.transaction_context is None:
            raise RuntimeError("no active transaction")
        transaction = self.transaction_context.workspace
        transaction.discard()
        self._restore_source_view(clear_context=True)
        if self.current_task_state is not None:
            self.current_task_state.transaction_state = transaction.state
            self.current_task_state.stop("transaction_discarded", final_answer=self.current_task_state.final_answer)
            self.run_store.write_task_state(self.current_task_state)
            self.run_store.write_report(
                self.current_task_state,
                self.redact_artifact(self.build_report(self.current_task_state)),
            )
        return {"state": transaction.state}

    def interrupt_transaction(self, reason="interrupted"):
        if self.transaction_context is not None and self.transaction_context.owns_context:
            self.transaction_context.workspace.interrupt(reason)
            self.session_path = self.session_store.save(self.session)

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    def _ensure_session_shape(self):
        self.session.setdefault("history", [])
        self.session.setdefault("memory", memorylib.default_memory_state())
        model_events = self.session.setdefault("model_events", [])
        if not isinstance(model_events, list):
            self.session["model_events"] = []
        execution_ledger = self.session.setdefault("execution_ledger", {})
        if not isinstance(execution_ledger, dict):
            self.session["execution_ledger"] = {}
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}

    def current_runtime_identity(self):
        return checkpointlib.current_runtime_identity(self)

    def checkpoint_state(self):
        return checkpointlib.checkpoint_state(self)

    def current_checkpoint(self):
        return checkpointlib.current_checkpoint(self)

    def invalidate_stale_memory(self):
        invalidated = self.memory.invalidate_stale_file_summaries()
        self.session["memory"] = self.memory.to_dict()
        return invalidated

    def evaluate_resume_state(self):
        return checkpointlib.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        return checkpointlib.render_checkpoint_text(self)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        tools = toolkit.build_tool_registry(self.tool_context())
        if "run_shell" in tools:
            dialect = self.execution_profile_view().get("dialect", "unavailable")
            tools["run_shell"]["description"] = (
                f"Run a {dialect} command in the transaction workspace. "
                "Use `python -m pytest` for Python validation."
            )
        return tools

    @staticmethod
    def _normalize_allowed_tools(allowed_tools):
        if allowed_tools is None:
            return None
        normalized = tuple(str(name).strip() for name in allowed_tools)
        if not normalized or any(not name for name in normalized):
            raise ValueError("allowed_tools must be a non-empty sequence of tool names")
        return normalized

    def _apply_tool_allowlist(self, tools):
        if self.allowed_tools is None:
            return tools
        legal_names = toolkit.legal_tool_names()
        unknown = [name for name in self.allowed_tools if name not in legal_names]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        allowed = set(self.allowed_tools)
        return {
            name: tool
            for name, tool in tools.items()
            if name in allowed
        }

    def tool_signature(self):
        return tool_signature(self.tools)

    def build_prefix(self):
        return build_prompt_prefix(workspace=self.workspace, tools=self.tools)

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        return self.memory.render_memory_text()

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        self.session["history"].append(self.redact_artifact(item))
        self.session_path = self.session_store.save(self.session)

    def record_model_events(self, events, execution_ledger=None):
        self.session["model_events"] = self.redact_artifact(list(events)[-24:])
        if execution_ledger is not None:
            self.session["execution_ledger"] = self.redact_artifact(execution_ledger)
        self.session_path = self.session_store.save(self.session)

    @staticmethod
    def looks_sensitive_env_name(name):
        return securitylib.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return securitylib.is_secret_env_name(name, secret_env_names=self.secret_env_names)

    def configured_secret_env_items(self):
        return securitylib.configured_secret_env_items(secret_env_names=self.secret_env_names)

    def detected_secret_env_items(self):
        return securitylib.detected_secret_env_items(secret_env_names=self.secret_env_names)

    def secret_env_summary(self):
        return securitylib.secret_env_summary(secret_env_names=self.secret_env_names)

    def detected_secret_env_summary(self):
        return securitylib.detected_secret_env_summary(secret_env_names=self.secret_env_names)

    def redact_text(self, text):
        return self.secret_boundary.sanitize_text(text)

    def redact_artifact(self, value, key=None):
        return self.secret_boundary.sanitize_object(value, key=key)

    def shell_env(self):
        return self.secret_boundary.build_sandbox_env(
            allowlist=self.shell_env_allowlist,
            extra={"PWD": "/workspace"},
        )

    def execution_profile_view(self):
        if self.transaction_context is None:
            return {"dialect": "unavailable", "executable": ""}
        return self.transaction_context.execution_lease.runner.profile_view()

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把“这轮 prompt 是怎么拼出来的”连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return self.secret_boundary.sanitize_text(prompt), self.secret_boundary.sanitize_object(metadata)

    def emit_trace(self, task_state, event, payload=None):
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答“这一轮 agent 到底做了什么”。
        self.run_store.append_trace(task_state, payload)
        if self.progress_sink is not None:
            try:
                self.progress_sink(event, dict(payload), task_state)
            except (OSError, UnicodeError):
                pass
        return payload

    def capture_workspace_snapshot(self):
        snapshot = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if not path.is_file():
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                digest = None
            if digest is not None:
                snapshot[path.relative_to(self.root).as_posix()] = digest
        return snapshot

    @staticmethod
    def diff_workspace_snapshots(before, after):
        changed_paths = []
        summaries = []
        all_paths = sorted(set(before) | set(after))
        for path in all_paths:
            if before.get(path) == after.get(path):
                continue
            changed_paths.append(path)
            if path not in before:
                summaries.append(f"created:{path}")
            elif path not in after:
                summaries.append(f"deleted:{path}")
            else:
                summaries.append(f"modified:{path}")
        return changed_paths, summaries

    def create_checkpoint(self, task_state, user_message, trigger):
        return checkpointlib.create_checkpoint(self, task_state, user_message, trigger)

    def infer_next_step(self, task_state):
        return checkpointlib.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到 working memory。

        为什么存在：
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进了
        `history`，这里只挑少量“下一轮大概率还会用到”的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整历史，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        path = args.get("path")
        if not path:
            return

        canonical_path = self.memory.canonical_path(path)
        # 不是所有工具结果都进入工作记忆。
        # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
        if name in {"read_file", "write_file", "patch_file"}:
            self.memory.remember_file(canonical_path)
        if name == "read_file":
            summary = memorylib.summarize_read_result(result)
            self.memory.set_file_summary(canonical_path, summary)
            self.memory.append_note(summary, tags=(canonical_path,), source=canonical_path)
        elif name in {"write_file", "patch_file"}:
            self.memory.invalidate_file_summary(canonical_path)

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def record_process_note_for_tool(self, name, metadata):
        status = str(metadata.get("tool_status", "")).strip()
        if status not in {"partial_success", "error", "rejected"}:
            return
        affected_paths = [str(path).strip() for path in metadata.get("affected_paths", []) if str(path).strip()]
        path_text = ", ".join(affected_paths) or "workspace"
        if status == "partial_success":
            text = f"{name} partial_success on {path_text}; inspect diff before retry"
        elif status == "error":
            text = f"{name} error on {path_text}; check the failure before retry"
        else:
            text = f"{name} rejected; choose a different action before retry"
        tags = ["process", status, *affected_paths]
        self.memory.append_note(text, tags=tuple(tags), source=name, kind="process")
        self.session["memory"] = self.memory.to_dict()

    def reject_durable_reason(self, note_text):
        text = str(note_text or "").strip()
        lowered = text.lower()
        if not text:
            return "empty"
        if REDACTED_VALUE in text or SECRET_SHAPED_TEXT_PATTERN.search(text):
            return "secret_shaped"
        checkpoint_like_prefixes = (
            "current goal",
            "current blocker",
            "next step",
            "current phase",
            "key files",
            "freshness",
            "当前目标",
            "当前卡点",
            "下一步",
            "当前阶段",
            "关键文件",
            "已完成",
            "已排除",
        )
        if any(lowered.startswith(prefix) for prefix in checkpoint_like_prefixes):
            return "transient_task_state"
        if re.search(r"(?i)\b(stdout|stderr|traceback|exit_code)\b", text) or len(text) > 220:
            return "noisy_output"
        return ""

    def extract_durable_promotions(self, user_message, final_answer=None):
        # The model's final answer is deliberately not a memory source.  Only
        # explicit user-authored facts can cross the durable-memory boundary.
        del final_answer
        admission = extract_explicit_memory(user_message)
        promotions = []
        rejections = list(admission.rejections)
        for candidate in admission.candidates:
            reason = self.reject_durable_reason(candidate["text"])
            if reason:
                rejections.append(f"{candidate['topic']}:{reason}")
            else:
                promotions.append(dict(candidate))
        return promotions, rejections

    def promote_durable_memory(self, user_message, final_answer=None):
        promotions, rejections = self.extract_durable_promotions(user_message, final_answer)
        promoted, superseded = self.memory.promote_durable(promotions)
        self.session["memory"] = self.memory.to_dict()
        self.last_durable_promotions = promoted
        self.last_durable_rejections = rejections
        self.last_durable_superseded = superseded
        if promoted or rejections or superseded:
            self.session_path = self.session_store.save(self.session)
        return promoted, rejections, superseded

    def ask(self, user_message):
        from .agent_loop import AgentLoop

        if (
            self.transaction_context is not None
            and self.transaction_context.workspace.state not in {"ACTIVE", "INTERRUPTED"}
        ):
            state = self.transaction_context.workspace.state
            raise RuntimeError(f"active transaction is {state}; Apply, Discard, or recover it before another task")
        return AgentLoop(self).run(user_message)

    def execute_tool(self, name, args):
        result = self.tool_executor.execute(name, args)
        self._last_tool_result_metadata = dict(result.metadata)
        return result

    def run_tool(self, name, args):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是“模型会不会想调用工具”，而是
        “平台有没有在执行前把边界守住”。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的“模型决定要调用工具”之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        direct_transaction = self.transaction_context is None
        if direct_transaction:
            self.begin_transaction()
        result = self.execute_tool(name, args)
        if direct_transaction and self.commit_policy == "auto" and result.metadata.get("read_only") is False:
            if result.metadata.get("tool_status") == "ok":
                outcome = self.finalize_transaction()
                if outcome["state"] == "CONFLICTED":
                    return "error: workspace conflict: " + json.dumps(outcome["conflicts"], ensure_ascii=False)
            else:
                self.interrupt_transaction(result.metadata.get("tool_error_code") or "tool_failed")
        return result.content

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(self, task_state):
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        interaction = dict(self.current_interaction)
        if not interaction or task_state.run_id != getattr(self.current_task_state, "run_id", ""):
            interaction = {
                "mode": task_state.request_mode,
                "request_profile": task_state.request_profile,
                "package_layout": task_state.package_layout,
                "relative_adjustment": task_state.relative_adjustment,
            }
        return {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "checkpoint_id": task_state.checkpoint_id,
            "resume_status": task_state.resume_status,
            "task_state": task_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "durable_promotions": list(self.last_durable_promotions),
            "durable_rejections": list(self.last_durable_rejections),
            "durable_superseded": list(self.last_durable_superseded),
            "redacted_env": self.detected_secret_env_summary(),
            "transaction_id": task_state.transaction_id,
            "transaction_state": task_state.transaction_state,
            "blocked_repeats": task_state.blocked_repeats,
            "intervention_count": task_state.intervention_count,
            "steps_to_first_mutation": task_state.steps_to_first_mutation,
            "steps_to_first_shell": task_state.steps_to_first_shell,
            "max_discovery_streak": task_state.max_discovery_streak,
            "stuck_detected": task_state.stuck_detected,
            "model_incomplete_count": task_state.model_incomplete_count,
            "model_protocol_error_count": task_state.model_protocol_error_count,
            "model_transport_failure_count": task_state.model_transport_failure_count,
            "model_duration_ms": task_state.model_duration_ms,
            "tool_duration_ms": task_state.tool_duration_ms,
            "provider_retry_count": task_state.provider_retry_count,
            "model_execution_policy": self.model_execution_policy.mode,
            "execution_backend": "workspace-process",
            "execution_isolation": "deployment-boundary",
            "session_revision": int(self.session.get("revision", 0)),
            "interaction": interaction,
            "evidence": {
                "changed_paths": list(task_state.changed_paths),
                "validation_commands": list(task_state.validation_commands),
                "validation_status": task_state.validation_status,
            },
        }

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self.tool_context(), name, args)

    def tool_context(self):
        return ToolContext(
            root=self.root,
            path_resolver=self.path,
            shell_env_provider=self.shell_env,
            command_runner=(
                self.transaction_context.execution_lease.runner
                if self.transaction_context is not None
                else None
            ),
            depth=self.depth,
            max_depth=self.max_depth,
            spawn_delegate=self.spawn_delegate,
        )

    def spawn_delegate(self, args):
        task = str(args.get("task", "")).strip()
        if self.transaction_context is None:
            raise RuntimeError("delegate requires an active transaction")
        child = Pico(
            model_client=self.model_client,
            workspace=self.workspace,
            session_store=self.session_store,
            run_store=self.run_store,
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=self.max_new_tokens,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
            secret_env_names=self.secret_env_names,
            shell_env_allowlist=self.shell_env_allowlist,
            commit_policy=self.commit_policy,
            state_root=(self.workspace_state.global_root if self.workspace_state else None),
            transaction_context=self.transaction_context.borrow(),
            sandbox_image=self.sandbox_image,
            soft_discovery_limit=self.soft_discovery_limit,
            hard_discovery_limit=self.hard_discovery_limit,
            model_execution_policy=self.model_execution_policy.mode,
            progress_sink=self.progress_sink,
            package_layout=self.effective_package_layout(),
        )
        # 委派的目标是“调查”，不是“放权执行”。
        # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
        child.session["memory"]["task"] = task
        child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
        return "delegate_result:\n" + child.ask(task)

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self.tool_context(), args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self.tool_context(), args)

    def tool_read_files(self, args):
        return toolkit.tool_read_files(self.tool_context(), args)

    def tool_search(self, args):
        return toolkit.tool_search(self.tool_context(), args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self.tool_context(), args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self.tool_context(), args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self.tool_context(), args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self.tool_context(), args)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def parse(raw):
        """把模型原始输出解析成 runtime 可执行的动作或最终答案。

        为什么存在：
        模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
        “这是工具调用”还是“这是最终答案”。如果没有这层解析，后面的工具校验、
        审批和执行链路就没法可靠工作。

        输入 / 输出：
        - 输入：模型返回的原始文本 `raw`
        - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`final`、`retry`

        在 agent 链路里的位置：
        它位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
        进入平台控制流的第一道结构化关口。
        """
        raw = str(raw).strip()
        # 这里支持两种工具格式：
        # 1. <tool>...</tool> 里包 JSON，适合简短调用
        # 2. XML 风格属性/子标签，适合写文件这类多行内容
        json_tool = re.fullmatch(r"<tool>\s*(.*?)\s*</tool>", raw, re.DOTALL)
        if json_tool:
            body = json_tool.group(1)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return "retry", Pico.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", Pico.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", Pico.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", Pico.retry_notice()
            return "tool", payload
        if re.fullmatch(r"<tool(?:\s[^>]*)?>.*?</tool>", raw, re.DOTALL):
            payload = Pico.parse_xml_tool(raw)
            if payload is not None:
                return "tool", payload
            return "retry", Pico.retry_notice()
        final_match = re.fullmatch(r"<final>\s*(.*?)\s*</final>", raw, re.DOTALL)
        if final_match:
            final = final_match.group(1).strip()
            if final:
                return "final", final
            return "retry", Pico.retry_notice("model returned an empty <final> answer")
        if not raw:
            return "retry", Pico.retry_notice("model returned an empty response")
        return "retry", Pico.retry_notice("model returned an incomplete or untyped protocol response")

    @staticmethod
    def retry_notice(problem=None):
        prefix = "Runtime notice"
        if problem:
            prefix += f": {problem}"
        else:
            prefix += ": model returned malformed tool output"
        return (
            f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
            'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
        )

    @staticmethod
    def parse_xml_tool(raw):
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.DOTALL)
        if not match:
            return None
        attrs = Pico.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None

        body = match.group("body")
        args = dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
            if f"<{key}>" in body:
                args[key] = Pico.extract_raw(body, key)

        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "delegate" and "task" not in args and body_text:
            args["task"] = body_text.strip()
        return {"name": name, "args": args}

    @staticmethod
    def parse_attrs(text):
        attrs = {}
        for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    @staticmethod
    def extract(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:].strip()
        return text[start:end].strip()

    @staticmethod
    def extract_raw(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:]
        return text[start:end]

    def reset(self):
        if self.transaction_context is not None:
            raise RuntimeError("active transaction must be applied or discarded before session reset")
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(
            self.session["memory"],
            workspace_root=self.root,
            durable_root=(
                self.workspace_state.memory
                if self.workspace_state
                else Path(self.session_store.root).parent / "memory"
            ),
        )
        self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = logical_path(native_path(path).resolve())
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        relative = resolved.relative_to(self.root)
        if relative.parts and relative.parts[0] in {".git", ".pico"}:
            raise ValueError(f"path is internal to the runtime: {raw_path}")
        return resolved
