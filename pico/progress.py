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
STABLE_READ_TOOLS = frozenset({"list_files", "read_file", "read_files", "search", "inspect_repository"})
DISCOVERY_TOOLS = frozenset({"list_files", "read_file", "read_files", "search", "inspect_repository", "delegate"})
VALIDATION_COMMAND = re.compile(
    r"(?i)(^|[;&|]\s*|\s)(pytest|python\s+-m\s+pytest|mvn(?:\s+[^;&|]+)?\s+test|"
    r"gradle\w*\s+test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
    r"cargo\s+test|go\s+test|make\s+test)(\s|$)"
)
SHELL_SEGMENT = re.compile(r"\s*(?:&&|\|\||[;|])\s*")
REPOSITORY_READ_COMMANDS = frozenset(
    {
        "cat",
        "cd",
        "dir",
        "echo",
        "find",
        "findstr",
        "gc",
        "get-childitem",
        "get-content",
        "grep",
        "head",
        "ls",
        "more",
        "popd",
        "pushd",
        "rg",
        "set-location",
        "tail",
        "type",
    }
)
MAX_LEDGER_OBSERVED_FILES = 48
MAX_LEDGER_RANGES_PER_FILE = 8
MAX_LEDGER_DIRECTORIES = 24
MAX_LEDGER_SEARCHES = 24
MAX_LEDGER_MUTATIONS = 24
MAX_LEDGER_VALIDATIONS = 12
MAX_LEDGER_FAILURES = 12
MAX_LEDGER_UNVERIFIED_CHANGES = 48
MAX_LEDGER_GROUNDING_PATHS = 24
MAX_LEDGER_PATH_REVISIONS = 256


def is_validation_command(command):
    return bool(VALIDATION_COMMAND.search(str(command or "")))


def is_repository_read_command(command):
    """Recognize shell pipelines that only duplicate typed repository reads."""
    text = str(command or "").strip()
    if not text or is_validation_command(text):
        return False
    segments = [item.strip() for item in SHELL_SEGMENT.split(text) if item.strip()]
    if not segments:
        return False
    commands = []
    for segment in segments:
        match = re.match(r"(?i)^(?:&\s*)?([\w.-]+)", segment)
        if match is None:
            return False
        commands.append(match.group(1).casefold())
    return bool(commands) and all(command in REPOSITORY_READ_COMMANDS for command in commands)


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
    grounding_paths: set = field(default_factory=set)
    grounding_confidence: str = "none"
    semantic_backend: str = "none"
    semantic_status: str = "not_requested"
    broad_exploration_count: int = 0
    targeted_read_count: int = 0
    file_read_count: int = 0
    observed_file_count: int = 0
    observed_directory_count: int = 0
    search_count: int = 0
    mutation_path_count: int = 0
    validation_count: int = 0
    unresolved_failure_count: int = 0
    unverified_change_count: int = 0
    grounding_path_count: int = 0

    def path_revision(self, path):
        return int(self.path_revisions.get(str(path), 0))

    def mark_mutation(self, paths):
        for path in paths:
            key = str(path)
            # Reinsert updated revisions so the persisted bounded map retains
            # the most recently touched paths across resume cycles.
            previous_revision = self.path_revision(key)
            self.path_revisions.pop(key, None)
            self.path_revisions[key] = previous_revision + 1
            self.mutations.append(key)
            self.mutation_path_count += 1
            if len(self.mutations) > MAX_LEDGER_MUTATIONS:
                del self.mutations[:-MAX_LEDGER_MUTATIONS]
            if key not in self.unverified_changes:
                self.unverified_change_count += 1
            self.unverified_changes.add(key)
            if len(self.unverified_changes) > MAX_LEDGER_UNVERIFIED_CHANGES:
                self.unverified_changes.discard(min(self.unverified_changes))

    def record_read(self, tool_name, args):
        if tool_name == "read_file":
            items = [args]
        elif tool_name == "read_files":
            items = args.get("files", [])
        else:
            items = []
        for item in items:
            path = str(item.get("path", ""))
            self.file_read_count += 1
            if path in self.grounding_paths:
                self.targeted_read_count += 1
            coverage = (int(item.get("start", 1)), int(item.get("end", 0)))
            if path not in self.observed_files:
                self.observed_file_count += 1
            ranges = self.observed_files.setdefault(path, [])
            if coverage not in ranges:
                ranges.append(coverage)
                del ranges[:-MAX_LEDGER_RANGES_PER_FILE]
            while len(self.observed_files) > MAX_LEDGER_OBSERVED_FILES:
                del self.observed_files[next(iter(self.observed_files))]
        if tool_name == "list_files":
            path = str(args.get("path", "."))
            if path not in self.observed_directories:
                self.observed_directory_count += 1
            self.observed_directories.add(path)
            while len(self.observed_directories) > MAX_LEDGER_DIRECTORIES:
                self.observed_directories.discard(min(self.observed_directories))
        elif tool_name == "search":
            search = (str(args.get("pattern", "")), str(args.get("path", ".")))
            if search not in self.searches:
                self.search_count += 1
            self.searches.add(search)
            while len(self.searches) > MAX_LEDGER_SEARCHES:
                self.searches.discard(min(self.searches))
        elif tool_name == "inspect_repository":
            search = (str(args.get("query", "")), "<repository-graph>")
            if search not in self.searches:
                self.search_count += 1
            self.searches.add(search)
            while len(self.searches) > MAX_LEDGER_SEARCHES:
                self.searches.discard(min(self.searches))

    def seed_repository_evidence(self, evidence):
        evidence = evidence or {}
        paths = {
            str(path) for path in evidence.get("paths", []) if str(path).strip()
        }
        self.grounding_path_count = len(paths)
        self.grounding_paths = set(sorted(paths)[-MAX_LEDGER_GROUNDING_PATHS:])
        self.grounding_confidence = str(evidence.get("confidence", "none"))
        self.semantic_backend = str(evidence.get("semantic_backend", "none"))
        self.semantic_status = str(evidence.get("semantic_status", "not_requested"))

    def view(self):
        observed_items = list(self.observed_files.items())[-MAX_LEDGER_OBSERVED_FILES:]
        return {
            "observed_files": {
                path: [list(item) for item in ranges[-MAX_LEDGER_RANGES_PER_FILE:]]
                for path, ranges in sorted(observed_items)
            },
            "observed_file_count": max(self.observed_file_count, len(self.observed_files)),
            "observed_directories": sorted(self.observed_directories)[-MAX_LEDGER_DIRECTORIES:],
            "observed_directory_count": max(
                self.observed_directory_count, len(self.observed_directories)
            ),
            "searches": [
                list(item) for item in sorted(self.searches)[-MAX_LEDGER_SEARCHES:]
            ],
            "search_count": max(self.search_count, len(self.searches)),
            "mutations": list(self.mutations[-MAX_LEDGER_MUTATIONS:]),
            "mutation_path_count": max(self.mutation_path_count, len(self.mutations)),
            "validations": list(self.validations[-MAX_LEDGER_VALIDATIONS:]),
            "validation_count": max(self.validation_count, len(self.validations)),
            "unresolved_failures": list(
                self.unresolved_failures[-MAX_LEDGER_FAILURES:]
            ),
            "unresolved_failure_count": max(
                self.unresolved_failure_count, len(self.unresolved_failures)
            ),
            "unverified_changes": sorted(self.unverified_changes)[
                -MAX_LEDGER_UNVERIFIED_CHANGES:
            ],
            "unverified_change_count": max(
                self.unverified_change_count, len(self.unverified_changes)
            ),
            "grounding_paths": sorted(self.grounding_paths)[-MAX_LEDGER_GROUNDING_PATHS:],
            "grounding_path_count": max(self.grounding_path_count, len(self.grounding_paths)),
            "grounding_confidence": self.grounding_confidence,
            "semantic_backend": self.semantic_backend,
            "semantic_status": self.semantic_status,
            "broad_exploration_count": self.broad_exploration_count,
            "targeted_read_count": self.targeted_read_count,
            "file_read_count": self.file_read_count,
        }

    def to_dict(self):
        recent_revisions = list(self.path_revisions.items())[-MAX_LEDGER_PATH_REVISIONS:]
        return {"path_revisions": dict(recent_revisions), **self.view()}

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        ledger = cls()
        ledger.path_revisions = {
            str(k): int(v)
            for k, v in list(data.get("path_revisions", {}).items())[
                -MAX_LEDGER_PATH_REVISIONS:
            ]
        }
        ledger.observed_files = {
            str(path): [tuple(item) for item in ranges[-MAX_LEDGER_RANGES_PER_FILE:]]
            for path, ranges in list(data.get("observed_files", {}).items())[
                -MAX_LEDGER_OBSERVED_FILES:
            ]
        }
        ledger.observed_directories = set(
            data.get("observed_directories", [])[-MAX_LEDGER_DIRECTORIES:]
        )
        ledger.searches = {
            tuple(item) for item in data.get("searches", [])[-MAX_LEDGER_SEARCHES:]
        }
        ledger.mutations = list(data.get("mutations", [])[-MAX_LEDGER_MUTATIONS:])
        ledger.validations = list(data.get("validations", [])[-MAX_LEDGER_VALIDATIONS:])
        ledger.unresolved_failures = list(
            data.get("unresolved_failures", [])[-MAX_LEDGER_FAILURES:]
        )
        ledger.unverified_changes = set(
            data.get("unverified_changes", [])[-MAX_LEDGER_UNVERIFIED_CHANGES:]
        )
        ledger.grounding_paths = set(
            data.get("grounding_paths", [])[-MAX_LEDGER_GROUNDING_PATHS:]
        )
        ledger.grounding_confidence = str(data.get("grounding_confidence", "none"))
        ledger.semantic_backend = str(data.get("semantic_backend", "none"))
        ledger.semantic_status = str(data.get("semantic_status", "not_requested"))
        ledger.broad_exploration_count = int(data.get("broad_exploration_count", 0))
        ledger.targeted_read_count = int(data.get("targeted_read_count", 0))
        ledger.file_read_count = int(data.get("file_read_count", 0))
        ledger.observed_file_count = max(
            len(ledger.observed_files), int(data.get("observed_file_count", 0))
        )
        ledger.observed_directory_count = max(
            len(ledger.observed_directories),
            int(data.get("observed_directory_count", 0)),
        )
        ledger.search_count = max(len(ledger.searches), int(data.get("search_count", 0)))
        ledger.mutation_path_count = max(
            len(ledger.mutations), int(data.get("mutation_path_count", 0))
        )
        ledger.validation_count = max(
            len(ledger.validations), int(data.get("validation_count", 0))
        )
        ledger.unresolved_failure_count = max(
            len(ledger.unresolved_failures),
            int(data.get("unresolved_failure_count", 0)),
        )
        ledger.unverified_change_count = max(
            len(ledger.unverified_changes),
            int(data.get("unverified_change_count", 0)),
        )
        ledger.grounding_path_count = max(
            len(ledger.grounding_paths), int(data.get("grounding_path_count", 0))
        )
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
    def __init__(
        self,
        max_steps,
        read_only=False,
        soft_discovery_limit=None,
        hard_discovery_limit=None,
        ledger=None,
        repository_evidence=None,
    ):
        self.max_steps = max(1, int(max_steps))
        self.read_only = bool(read_only)
        default_hard = max(6, math.ceil(self.max_steps * 0.50))
        if not self.read_only:
            # Leave an execution reserve for at least one edit, one
            # verification, and one correction. Discovery must not consume
            # the complete tool budget before FORCED_DECISION can take effect.
            default_hard = min(default_hard, max(2, self.max_steps - 3))
        default_soft = min(
            max(4, math.ceil(self.max_steps * 0.33)),
            max(1, default_hard - 1),
        )
        evidence_confidence = str((repository_evidence or {}).get("confidence", "none"))
        if evidence_confidence == "high" and not self.read_only:
            default_soft = min(default_soft, 4)
            default_hard = min(default_hard, 6)
        self.soft_discovery_limit = int(soft_discovery_limit or default_soft)
        self.hard_discovery_limit = int(hard_discovery_limit or default_hard)
        if self.soft_discovery_limit < 1:
            raise ValueError("soft_discovery_limit must be positive")
        if self.hard_discovery_limit < self.soft_discovery_limit:
            raise ValueError("hard_discovery_limit must be >= soft_discovery_limit")
        self.state = ProgressState()
        self.ledger = ExecutionLedger.from_dict(ledger)
        if repository_evidence:
            self.ledger.seed_repository_evidence(repository_evidence)

    @staticmethod
    def _is_broad_exploration(tool_name, args):
        return tool_name in {"list_files", "search"} and str(args.get("path", ".")) in {"", "."}

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
        if tool_name == "run_shell" and is_repository_read_command(args.get("command", "")):
            self.state.blocked_repeats += 1
            self.state.no_progress_streak += 1
            self._maybe_intervene()
            return {
                "allowed": False,
                "signature": signature,
                "evidence": ProgressEvidence(
                    NO_PROGRESS, "", "typed_repository_read_required"
                ),
            }
        if self.requires_material_action() and tool_name not in self.admissible_tools(
            {tool_name}
        ):
            self.state.blocked_repeats += 1
            self.state.no_progress_streak += 1
            self.state.post_hard_no_progress += 1
            self.state.pending_notices.append(self._forced_notice())
            self._detect_stuck()
            return {
                "allowed": False,
                "signature": signature,
                "evidence": ProgressEvidence(
                    NO_PROGRESS, "", "material_action_required"
                ),
            }
        if (
            self._is_broad_exploration(tool_name, args)
            and self.ledger.grounding_confidence == "high"
            and self.ledger.grounding_paths
            and self.ledger.targeted_read_count == 0
            and self.ledger.broad_exploration_count >= 2
        ):
            self.state.blocked_repeats += 1
            self.state.no_progress_streak += 1
            self.state.pending_notices.append(self._grounding_notice())
            self._maybe_intervene()
            return {
                "allowed": False,
                "signature": signature,
                "evidence": ProgressEvidence(NO_PROGRESS, "", "broad_exploration_after_grounding"),
            }
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

    def requires_material_action(self):
        return self.state.intervention_level == INTERVENTION_FORCED

    def admissible_tools(self, tool_names):
        names = set(tool_names)
        if not self.requires_material_action():
            return names
        if self.read_only:
            return set()
        return names & {"write_file", "patch_file"}

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
            # Argument validation failures belong to the workspace revision in
            # which they occurred. A successful mutation may make the exact
            # same operation valid, so failures must not poison later phases.
            self.state.rejected_actions.clear()
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
            elif tool_name in {"run_shell", "run_verification"} and executed:
                validation = tool_name == "run_verification" or is_validation_command(
                    args.get("command", "")
                )
                if validation:
                    self.state.shell_count += 1
                    self.state.discovery_streak = 0
                else:
                    # A shell command with no workspace effect and no
                    # verification is still exploration. Treating it as
                    # progress lets `cat`/`rg` bypass the discovery budget.
                    self.state.discovery_streak += 1
                    self.state.max_discovery_streak = max(
                        self.state.max_discovery_streak,
                        self.state.discovery_streak,
                    )
            # Rejected actions and non-mutating write attempts are not phase
            # progress. Preserve the exploration streak instead of letting an
            # invalid write reset the forced-decision budget.
            if self.state.intervention_level == INTERVENTION_FORCED:
                self.state.post_hard_no_progress = self.state.post_hard_no_progress + 1 if evidence.kind == NO_PROGRESS else 0
                self._detect_stuck()

        if executed:
            self.state.observations.add(identity)
            if tool_name in STABLE_READ_TOOLS and status == "ok":
                self.state.successful_reads.add(signature.key)
                self.ledger.record_read(tool_name, args)
                if self._is_broad_exploration(tool_name, args):
                    self.ledger.broad_exploration_count += 1
                    if (
                        self.ledger.grounding_confidence == "high"
                        and self.ledger.grounding_paths
                        and self.ledger.targeted_read_count == 0
                    ):
                        self.state.pending_notices.append(self._grounding_notice())
            if tool_name in {"run_shell", "run_verification"}:
                validation = tool_name == "run_verification"
                command = (
                    " ".join(str(item) for item in args.get("argv", []))
                    if validation
                    else str(args.get("command", ""))
                )
                record = {
                    "command": command,
                    "status": status,
                    "kind": "validation" if validation else "shell_execution",
                }
                self.ledger.validations.append(record)
                self.ledger.validation_count += 1
                del self.ledger.validations[:-MAX_LEDGER_VALIDATIONS]
                if validation and status == "ok":
                    self.ledger.unverified_changes.clear()
                    self.ledger.unverified_change_count = 0
                elif validation:
                    self.ledger.unresolved_failures.append(record)
                    self.ledger.unresolved_failure_count += 1
                    del self.ledger.unresolved_failures[:-MAX_LEDGER_FAILURES]
        self._maybe_intervene()
        return evidence

    def _maybe_intervene(self):
        if (
            self.state.discovery_streak >= self.hard_discovery_limit
            and self.state.intervention_level != INTERVENTION_FORCED
        ):
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
            action = "Finalize from existing evidence or identify a blocker."
        else:
            action = "Modify with write_file/patch_file, finalize, or identify a blocker."
        return (
            "Runtime notice: the exploration budget is exhausted; further reads, searches, "
            "delegation, and shell exploration are unavailable until a workspace mutation. "
            + action
        )

    def _grounding_notice(self):
        candidates = ", ".join(sorted(self.ledger.grounding_paths)[:6])
        return (
            "Runtime notice: repository evidence already identified high-confidence candidate files "
            f"({candidates}). Read those targets before another repository-wide listing or search."
        )

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
            "initial_evidence_count": len(self.ledger.grounding_paths),
            "grounding_confidence": self.ledger.grounding_confidence,
            "semantic_backend": self.ledger.semantic_backend,
            "semantic_status": self.ledger.semantic_status,
            "broad_exploration_count": self.ledger.broad_exploration_count,
            "targeted_read_count": self.ledger.targeted_read_count,
            "evidence_hit_rate": (
                self.ledger.targeted_read_count / self.ledger.file_read_count
                if self.ledger.file_read_count
                else 0.0
            ),
        }
