"""Execution ledger and deterministic progress control."""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

NEW_EVIDENCE = "NEW_EVIDENCE"
MATERIAL_PROGRESS = "MATERIAL_PROGRESS"
NO_PROGRESS = "NO_PROGRESS"
INTERVENTION_NORMAL = "NORMAL"
INTERVENTION_SOFT = "SOFT_INTERVENTION"
INTERVENTION_FORCED = "FORCED_DECISION"
STABLE_READ_TOOLS = frozenset({"list_files", "read_file", "read_files", "search"})
DISCOVERY_TOOLS = frozenset({"list_files", "read_file", "read_files", "search", "delegate"})
VALIDATION_COMMAND = re.compile(
    r"(?i)(^|[;&|]\s*|\s)(pytest|python\s+-m\s+pytest|mvn(?:\s+[^;&|]+)?\s+test|"
    r"gradle\w*\s+test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
    r"cargo\s+test|go\s+test|make\s+test)(\s|$)"
)


def is_validation_command(command):
    return bool(VALIDATION_COMMAND.search(str(command or "")))


@dataclass(frozen=True)
class ActionSignature:
    tool_name: str
    canonical_args: str
    revision: str

    @classmethod
    def create(cls, tool_name, args, revision):
        canonical = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(str(tool_name), canonical, str(revision))

    @property
    def key(self):
        return f"{self.tool_name}:{self.canonical_args}@{self.revision}"


@dataclass(frozen=True)
class ProgressEvidence:
    kind: str
    observation_hash: str
    reason: str


@dataclass
class ExecutionLedger:
    path_revisions: dict = field(default_factory=dict)
    observed_files: dict = field(default_factory=dict)
    observed_directories: set = field(default_factory=set)
    searches: set = field(default_factory=set)
    mutations: list = field(default_factory=list)
    validations: list = field(default_factory=list)
    unresolved_failures: list = field(default_factory=list)
    unverified_changes: set = field(default_factory=set)

    def path_revision(self, path):
        return int(self.path_revisions.get(str(path), 0))

    def mark_mutation(self, paths):
        for path in paths:
            key = str(path)
            self.path_revisions[key] = self.path_revision(key) + 1
            self.mutations.append(key)
            self.unverified_changes.add(key)

    def record_read(self, tool_name, args):
        if tool_name == "read_file":
            items = [args]
        elif tool_name == "read_files":
            items = args.get("files", [])
        else:
            items = []
        for item in items:
            path = str(item.get("path", ""))
            coverage = (int(item.get("start", 1)), int(item.get("end", 0)))
            ranges = self.observed_files.setdefault(path, [])
            if coverage not in ranges:
                ranges.append(coverage)
        if tool_name == "list_files":
            self.observed_directories.add(str(args.get("path", ".")))
        elif tool_name == "search":
            self.searches.add((str(args.get("pattern", "")), str(args.get("path", "."))))

    def view(self):
        return {
            "observed_files": {path: [list(r) for r in ranges] for path, ranges in sorted(self.observed_files.items())},
            "observed_directories": sorted(self.observed_directories),
            "searches": [list(item) for item in sorted(self.searches)],
            "mutations": list(self.mutations[-12:]),
            "validations": list(self.validations[-8:]),
            "unresolved_failures": list(self.unresolved_failures[-8:]),
            "unverified_changes": sorted(self.unverified_changes),
        }

    def to_dict(self):
        return {"path_revisions": dict(self.path_revisions), **self.view()}

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        ledger = cls()
        ledger.path_revisions = {str(k): int(v) for k, v in data.get("path_revisions", {}).items()}
        ledger.observed_files = {
            str(path): [tuple(item) for item in ranges]
            for path, ranges in data.get("observed_files", {}).items()
        }
        ledger.observed_directories = set(data.get("observed_directories", []))
        ledger.searches = {tuple(item) for item in data.get("searches", [])}
        ledger.mutations = list(data.get("mutations", []))
        ledger.validations = list(data.get("validations", []))
        ledger.unresolved_failures = list(data.get("unresolved_failures", []))
        ledger.unverified_changes = set(data.get("unverified_changes", []))
        return ledger


@dataclass
class ProgressState:
    workspace_revision: int = 0
    discovery_streak: int = 0
    steps_since_material_progress: int = 0
    no_progress_streak: int = 0
    blocked_repeats: int = 0
    intervention_level: str = INTERVENTION_NORMAL
    intervention_count: int = 0
    max_discovery_streak: int = 0
    mutation_count: int = 0
    shell_count: int = 0
    stuck_detected: bool = False
    post_hard_no_progress: int = 0
    successful_reads: set = field(default_factory=set)
    observations: set = field(default_factory=set)
    pending_notices: list = field(default_factory=list)
    rejected_actions: set = field(default_factory=set)


class ProgressController:
    def __init__(self, max_steps, read_only=False, soft_discovery_limit=None, hard_discovery_limit=None, ledger=None):
        self.max_steps = max(1, int(max_steps))
        self.read_only = bool(read_only)
        self.soft_discovery_limit = int(soft_discovery_limit or max(4, math.ceil(self.max_steps * 0.33)))
        self.hard_discovery_limit = int(hard_discovery_limit or max(6, math.ceil(self.max_steps * 0.50)))
        if self.soft_discovery_limit < 1:
            raise ValueError("soft_discovery_limit must be positive")
        if self.hard_discovery_limit < self.soft_discovery_limit:
            raise ValueError("hard_discovery_limit must be >= soft_discovery_limit")
        self.state = ProgressState()
        self.ledger = ExecutionLedger.from_dict(ledger)

    def _revision_for(self, tool_name, args):
        if tool_name == "read_file":
            path = str(args.get("path", ""))
            return f"{path}:{self.ledger.path_revision(path)}"
        if tool_name == "read_files":
            return "|".join(
                f"{item.get('path', '')}:{self.ledger.path_revision(item.get('path', ''))}"
                for item in args.get("files", [])
            )
        return str(self.state.workspace_revision)

    def signature(self, tool_name, args):
        return ActionSignature.create(tool_name, args, self._revision_for(tool_name, args))

    def preflight(self, tool_name, args):
        signature = self.signature(tool_name, args)
        if tool_name not in STABLE_READ_TOOLS or signature.key not in self.state.successful_reads:
            return {"allowed": True, "signature": signature}
        self.state.blocked_repeats += 1
        self.state.no_progress_streak += 1
        self.state.steps_since_material_progress += 1
        if self.state.intervention_level == INTERVENTION_FORCED:
            self.state.post_hard_no_progress += 1
            self._detect_stuck()
        self._maybe_intervene()
        return {"allowed": False, "signature": signature, "evidence": ProgressEvidence(NO_PROGRESS, "", "repeated_no_progress")}

    @staticmethod
    def action_key(tool_name, args):
        canonical = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{tool_name}:{canonical}"

    def preflight_known_error(self, tool_name, args):
        key = self.action_key(tool_name, args)
        if key not in self.state.rejected_actions:
            return True
        self.state.blocked_repeats += 1
        self.state.no_progress_streak += 1
        self._maybe_intervene()
        return False

    def record_rejected_action(self, tool_name, args):
        self.state.rejected_actions.add(self.action_key(tool_name, args))

    def observe(self, tool_name, args, content, metadata):
        signature = self.signature(tool_name, args)
        observation_hash = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        identity = f"{signature.key}:{observation_hash}"
        status = str(metadata.get("tool_status", ""))
        executed = bool(metadata.get("executed", False))
        changed = bool(metadata.get("workspace_changed", False))

        if changed:
            affected_paths = list(metadata.get("affected_paths", []))
            if not affected_paths and args.get("path"):
                affected_paths = [str(args["path"])]
            self.ledger.mark_mutation(affected_paths)
            evidence = ProgressEvidence(MATERIAL_PROGRESS, observation_hash, "workspace_changed")
            self.state.workspace_revision += 1
            self.state.mutation_count += 1
            self.state.discovery_streak = self.state.steps_since_material_progress = 0
            self.state.no_progress_streak = self.state.post_hard_no_progress = 0
            self.state.intervention_level = INTERVENTION_NORMAL
            self.state.pending_notices.clear()
            self.state.stuck_detected = False
        else:
            if executed and identity not in self.state.observations and status in {"ok", "partial_success", "error"}:
                evidence = ProgressEvidence(NEW_EVIDENCE, observation_hash, "new_observation")
                self.state.no_progress_streak = 0
            else:
                evidence = ProgressEvidence(NO_PROGRESS, observation_hash, "repeated_or_rejected")
                self.state.no_progress_streak += 1
            self.state.steps_since_material_progress += 1
            if tool_name in DISCOVERY_TOOLS:
                self.state.discovery_streak += 1
                self.state.max_discovery_streak = max(self.state.max_discovery_streak, self.state.discovery_streak)
            elif tool_name == "run_shell" and executed:
                if is_validation_command(args.get("command", "")):
                    self.state.shell_count += 1
                self.state.discovery_streak = 0
            else:
                self.state.discovery_streak = 0
            if self.state.intervention_level == INTERVENTION_FORCED:
                self.state.post_hard_no_progress = self.state.post_hard_no_progress + 1 if evidence.kind == NO_PROGRESS else 0
                self._detect_stuck()

        if executed:
            self.state.observations.add(identity)
            if tool_name in STABLE_READ_TOOLS and status == "ok":
                self.state.successful_reads.add(signature.key)
                self.ledger.record_read(tool_name, args)
            if tool_name == "run_shell":
                validation = is_validation_command(args.get("command", ""))
                record = {
                    "command": str(args.get("command", "")),
                    "status": status,
                    "kind": "validation" if validation else "execution",
                }
                self.ledger.validations.append(record)
                if validation and status == "ok":
                    self.ledger.unverified_changes.clear()
                elif validation:
                    self.ledger.unresolved_failures.append(record)
        self._maybe_intervene()
        return evidence

    def _maybe_intervene(self):
        no_effects = self.state.mutation_count == 0 and self.state.shell_count == 0
        if no_effects and self.state.discovery_streak >= self.hard_discovery_limit and self.state.intervention_level != INTERVENTION_FORCED:
            self.state.intervention_level = INTERVENTION_FORCED
            self.state.intervention_count += 1
            self.state.post_hard_no_progress = 0
            self.state.pending_notices.append(self._forced_notice())
        elif self.state.intervention_level == INTERVENTION_NORMAL and (self.state.discovery_streak >= self.soft_discovery_limit or self.state.no_progress_streak >= 2):
            self.state.intervention_level = INTERVENTION_SOFT
            self.state.intervention_count += 1
            self.state.pending_notices.append(self._soft_notice())

    def _detect_stuck(self):
        if self.state.post_hard_no_progress >= 2:
            self.state.stuck_detected = True

    def consume_notice(self):
        return self.state.pending_notices.pop(0) if self.state.pending_notices else ""

    def runtime_state_view(self):
        return {"progress": self.metrics(), "ledger": self.ledger.view()}

    def _soft_notice(self):
        if self.read_only:
            action = "choose one genuinely new target, finalize, or identify a blocker."
        else:
            action = "choose one genuinely new target, modify, verify, finalize, or identify a blocker."
        return "Runtime notice: Recent actions are not advancing the task. Reuse prior observations and " + action

    def _forced_notice(self):
        if self.read_only:
            action = "Produce new evidence, finalize, or identify a blocker."
        else:
            action = "Modify, verify, finalize, or identify a blocker."
        return "Runtime notice: the exploration budget is exhausted; broad exploration must stop. " + action

    def metrics(self):
        return {
            "workspace_revision": self.state.workspace_revision,
            "blocked_repeats": self.state.blocked_repeats,
            "intervention_count": self.state.intervention_count,
            "max_discovery_streak": self.state.max_discovery_streak,
            "stuck_detected": self.state.stuck_detected,
            "discovery_streak": self.state.discovery_streak,
            "no_progress_streak": self.state.no_progress_streak,
            "intervention_level": self.state.intervention_level,
        }
