"""Bootstrap node for a LangGraph-managed Pico run."""

import time

from ..context.context_projection import ContextProjector
from ..context.working_set import build_initial_working_set
from ..domain.task_state import TaskState
from ..persistence.checkpoint_store import CHECKPOINT_NONE_STATUS
from ..progress import ProgressController
from ..workspace import clip, now


class BootstrapRuntime:
    """Create run-scoped state and seed repository evidence."""

    def _graph_bootstrap(self, state):
        agent = self.agent
        user_message = state["user_message"]
        run_started_at = time.monotonic()
        agent.current_run_started_at = run_started_at
        self._reset_run_fields()
        task_state, interaction = self._create_run_task(user_message)
        self._prepare_transaction(task_state, interaction, user_message)
        controller, native_mode, model_events, projector = self._prepare_context(
            task_state, interaction, user_message
        )
        self._emit_bootstrap_evidence(task_state, interaction, user_message)
        request_profile = interaction["request_profile"]
        return self._initial_graph_state(
            run_started_at,
            task_state,
            interaction,
            controller,
            native_mode,
            model_events,
            projector,
            request_profile,
        )

    def _reset_run_fields(self):
        agent = self.agent
        agent.last_prompt_metadata = {}
        agent.last_completion_metadata = {}
        agent.last_durable_promotions = []
        agent.last_durable_rejections = []
        agent.last_durable_superseded = []
        agent.last_run_outcome = None
        agent.last_repository_evidence = {}
        agent.initial_working_set = ""
        agent.initial_working_set_coverage = []
        agent._last_tool_result_metadata = {}

    def _create_run_task(self, user_message):
        agent = self.agent
        task_state = TaskState.create(
            run_id=agent.new_run_id(),
            task_id=agent.new_task_id(),
            user_request=user_message,
        )
        interaction = agent.interaction_contract(user_message)
        agent.current_interaction = interaction
        task_state.set_interaction(interaction)
        task_state.resume_status = agent.resume_state.get(
            "status", CHECKPOINT_NONE_STATUS
        )
        agent.current_task_state = task_state
        agent.current_run_lease = agent.run_store.acquire_run_lease(
            task_state, blocking=True
        )
        if agent.current_run_lease is None:
            raise RuntimeError("could not acquire run owner lease")
        agent.current_run_dir = agent.run_store.start_run(task_state)
        return task_state, interaction

    def _prepare_transaction(self, task_state, interaction, user_message):
        agent = self.agent
        if agent.transaction_context is None:
            agent.begin_transaction()
        elif interaction.get("mutation_allowed"):
            agent.transaction_context.workspace.resume_editing()
        if interaction.get("mutation_allowed") and not agent.read_only:
            evidence = agent.repository_evidence(user_message)
            if evidence:
                interaction["repository_evidence"] = evidence
        self._ground_referenced_paths(interaction)
        task_state.set_interaction(interaction)
        self._remember_transaction_paths(interaction)
        if agent.transaction_context is not None:
            task_state.transaction_id = agent.transaction_context.transaction_id
            task_state.transaction_state = agent.transaction_context.workspace.state
            agent.run_store.write_task_state(task_state)

    def _prepare_context(self, task_state, interaction, user_message):
        agent = self.agent
        agent.memory.set_task_summary(user_message)
        agent.memory.set_work_scope(
            agent.transaction_context.transaction_id
            if agent.transaction_context is not None
            else task_state.task_id
        )
        agent.record({"role": "user", "content": user_message, "created_at": now()})
        controller = self._build_progress_controller(interaction)
        initial_working_set = build_initial_working_set(
            agent.path,
            user_message,
            (
                interaction.get("requested_existing_paths", ())
                if interaction.get("mutation_allowed") and not agent.read_only
                else ()
            ),
        )
        agent.initial_working_set = initial_working_set.text
        agent.initial_working_set_coverage = controller.seed_working_set(
            initial_working_set.coverage
        )
        agent.progress_controller = controller
        native_mode = bool(
            getattr(agent.model_client, "supports_native_tools", False)
            and hasattr(agent.model_client, "complete_turn")
        )
        model_events = (
            list(agent.session.get("model_events", [])) if native_mode else []
        )
        projector = ContextProjector(agent) if native_mode else None
        self._initial_working_set = initial_working_set
        return controller, native_mode, model_events, projector

    def _emit_bootstrap_evidence(self, task_state, interaction, user_message):
        agent = self.agent
        initial_working_set = self._initial_working_set
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
                "interaction": interaction,
                "orchestrator": "langgraph",
            },
        )
        if initial_working_set.coverage:
            agent.emit_trace(
                task_state,
                "working_set_seeded",
                {
                    "chars": len(initial_working_set.text),
                    "coverage": list(initial_working_set.coverage),
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

    def _initial_graph_state(
        self,
        run_started_at,
        task_state,
        interaction,
        controller,
        native_mode,
        model_events,
        projector,
        request_profile,
    ):
        agent = self.agent
        recovery_budget = agent.model_execution_policy.max_recoveries
        return {
            "next_node": "action_turn_runtime",
            "run_started_at": run_started_at,
            "task_state": task_state,
            "interaction": interaction,
            "controller": controller,
            "native_mode": native_mode,
            "model_events": model_events,
            "projector": projector,
            "attempts": 0,
            "max_attempts": agent.max_steps + recovery_budget + 1,
            "finalization_requested": False,
            "stuck_final": None,
            "contract_failure_final": None,
            "contract_failures": 0,
            "contract_failure_reason": "",
            "completion_failure_reason": "",
            "model_notice": "",
            "request_profile": request_profile,
            "prompt_metadata_base": {
                "request_profile": request_profile,
                "request_mode": interaction["mode"],
                "package_layout": interaction["package_layout"],
                "orchestrator": "langgraph",
            },
            "finalization_failures": 0,
            "finalization_reason": "",
            "finalization_attempts": 0,
            "max_finalization_attempts": 1 + recovery_budget,
        }

    def _remember_transaction_paths(self, interaction):
        if not (
            interaction.get("mutation_allowed") and interaction.get("referenced_paths")
        ):
            return
        path_context = self.agent.session.setdefault("transaction_requirements", {})
        for key in (
            "referenced_paths",
            "requested_existing_paths",
            "requested_missing_paths",
            "unresolved_path_mentions",
        ):
            path_context[key] = list(interaction.get(key, ()))

    def _build_progress_controller(self, interaction):
        agent = self.agent
        return ProgressController(
            max_steps=agent.max_steps,
            read_only=agent.read_only or not interaction.get("mutation_allowed", False),
            soft_discovery_limit=agent.soft_discovery_limit,
            hard_discovery_limit=agent.hard_discovery_limit,
            ledger=agent.session.get("execution_ledger", {}),
            repository_evidence=agent.last_repository_evidence,
            delivery_requirements=interaction,
        )
